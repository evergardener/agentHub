"""Hermes Brain — 总控对话循环（Evolution v3 §6.1）。

用户唯一入口：chat()。LLM 决策 + 工具调用循环；
审批通过对话完成（needs_approval → 询问用户 → approve_and_delegate）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hermes.llm import HermesLLM
from hermes.policy import ApprovalPolicy
from hermes.tools import TOOL_SCHEMAS, HermesTools
from orchestrator.task_manager import TaskManager

log = logging.getLogger("hermes")

SYSTEM_PROMPT = """你是 Hermes，本地多 Agent 系统的总控（规划/编排/监督）。

职责：
- 分析用户需求；多 Agent/多步骤工作必须先用 create_task_plan 形成结构化计划，
  明确每步 Agent/Profile、依赖、预期操作/产物和验收条件，再 delegate_task。
- 规划前先调用 list_agents，使用返回的 Profile version 与 allowed_operations，
  不得猜测 Agent 能力或 operation ID。
- 只有明确的单 Agent 单步骤小任务可使用 legacy create_task。
- 任务涉及读取或修改现有代码仓库时，create_task/create_task_plan 必须把该仓库的
  绝对路径放入结构化 workspace 字段；不得只把路径写进 objective。workspace 是
  原生工具审查和批准的安全边界，缺失会导致 Codex/DSH 请求保持不可批准。
- 创建任务时同时提供简洁 title（目标）和 summary（简要说明），完整约束继续放在
  objective；title 不得直接复制冗长对话或包含 commit SHA、完整路径和证据清单。
- 用户指定模型或推理强度时，只能使用 list_agents 返回的 Agent Profile
  allowed_models / allowed_reasoning_efforts；把 model 与 reasoning_effort 放入任务工具
  的结构化字段，不能写进 objective 代替。未指定时保持该 Agent 的默认运行配置。
- Worker：codex（编码/测试/运维操作），kimi（调研/长上下文分析），
  dsh（持久开发会话/复审/原生子 agent 协作）。
- 任务完成后 wait_task 等结果，review_task 复审；不合格就返工（review approved=false 后重新委派）。
- 最终用简洁中文向用户汇报结果。

复审纪律（必须遵守）：
- review_task 验收前，先调 get_task_artifacts 核对产物清单（wait_task 返回里也附带了）。
- worker 汇报声称创建/写入了某文件，但产物清单中没有该文件 → 一律 approved=false 返工，
  不得凭汇报文本验收（worker 可能谎报完成）。
- 服务端对"声明创建文件但无产物"有强制驳回（veto）；被 veto 后先核实原因再重新委派。

审批纪律（必须遵守）：
- list_agents 或任务工具返回 reason=agent_disabled / needs_confirmation 时，
  不得创建、委派、批准或重试该 Agent 的任务；必须先询问用户是启用后重新探测，
  还是改派其他已启用 Agent。不得静默改派。
- delegate_task 返回 needs_approval 时，停下来用一句话向用户说明风险并询问是否批准。
- 用户说"批准/可以/做吧"→ 调 approve_and_delegate。
- 用户说"以后 X 类你自己批"→ 先调 grant_operation 记录常驻授权，再调 approve_and_delegate 完成本次。
- 用户拒绝 → 不要再委派，说明已取消。
- 一次只问一个审批问题。
- wait_task 返回 blocked 时检查 pending_interactions：只有 inspectable=true 且
  action_intent_status=awaiting_hermes 的请求，才可在核对目标、影响和回滚方案后调用
  respond_agent_interaction；对 risk=read 的 command.read，rollback_plan=null 表示无变更、
  回滚不适用，但仍只能 allowed-once。awaiting_user 必须请用户在 WebUI
  处理，Hermes 不得越权。
"""

MAX_TOOL_ROUNDS = 12


class Hermes:
    def __init__(self, tm: TaskManager, llm: HermesLLM | None = None,
                 policy: ApprovalPolicy | None = None,
                 agents_path: Path | None = None,
                 conversation_id: str | None = None,
                 collaboration_id: str | None = None):
        self.tm = tm
        self.llm = llm or HermesLLM()
        self.conversation_id = conversation_id
        self.collaboration_id = collaboration_id
        self.tools = HermesTools(
            tm, policy or ApprovalPolicy(), agents_path,
            collaboration_id=collaboration_id)
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT}]
        self._restored_sequence = 0
        self._restore_context()

    def _restore_context(self) -> None:
        """Validate IDs and restore persisted LLM-visible messages."""
        from orchestrator import collaboration_store

        if self.collaboration_id:
            collaboration = collaboration_store.get_collaboration(
                self.tm.conn, self.collaboration_id)
            if collaboration is None:
                raise KeyError(
                    f"collaboration not found: {self.collaboration_id}")
            actual_conversation = collaboration["conversation_id"]
            if (self.conversation_id
                    and self.conversation_id != actual_conversation):
                raise ValueError(
                    "collaboration does not belong to conversation")
            self.conversation_id = actual_conversation
            rows = collaboration_store.list_collaboration_messages(
                self.tm.conn, self.collaboration_id)
            for row in rows:
                self._restore_visible_row(row)
                self._restored_sequence = max(
                    self._restored_sequence, row["sequence"])
            return
        if self.conversation_id and collaboration_store.get_conversation(
                self.tm.conn, self.conversation_id) is None:
            raise KeyError(f"conversation not found: {self.conversation_id}")

    def _restore_visible_row(self, row) -> None:
        payload = json.loads(row["content_json"])
        if row["message_type"].startswith("llm."):
            if isinstance(payload, dict) and payload.get("role"):
                self.messages.append(payload)
            return
        if not row["message_type"].startswith("user."):
            return
        text = payload.get("text") if isinstance(payload, dict) else payload
        if not isinstance(text, str):
            text = json.dumps(payload, ensure_ascii=False)
        mode = row["message_type"].removeprefix("user.")
        self.messages.append({
            "role": "user",
            "content": f"[用户直接介入子 Agent：{mode}] {text}",
        })

    def _sync_user_interventions(self) -> None:
        """Make WebUI-issued session corrections visible to live Hermes."""
        if not self.collaboration_id:
            return
        from orchestrator import collaboration_store

        rows = collaboration_store.list_collaboration_messages(
            self.tm.conn, self.collaboration_id,
            after=self._restored_sequence)
        for row in rows:
            self._restore_visible_row(row)
            self._restored_sequence = max(
                self._restored_sequence, row["sequence"])

    def _ensure_collaboration(self, user_text: str) -> None:
        from orchestrator import collaboration_store

        if not self.conversation_id:
            self.conversation_id = collaboration_store.create_conversation(
                self.tm.conn, title=user_text[:80], created_by="user")
        if not self.collaboration_id:
            self.collaboration_id = collaboration_store.create_collaboration(
                self.tm.conn, conversation_id=self.conversation_id,
                objective=user_text)
            self.tools.collaboration_id = self.collaboration_id

    def _persist_message(self, payload: dict, *, sender_type: str,
                         sender_id: str, message_type: str) -> None:
        from orchestrator import collaboration_store

        collaboration = collaboration_store.get_collaboration(
            self.tm.conn, self.collaboration_id)
        row = collaboration_store.append_message(
            self.tm.conn, conversation_id=self.conversation_id,
            collaboration_id=self.collaboration_id,
            sender_type=sender_type, sender_id=sender_id,
            content=payload, message_type=message_type,
            based_on_revision=collaboration["context_revision"])
        self._restored_sequence = max(
            self._restored_sequence, row["sequence"])

    async def chat(self, user_text: str) -> str:
        from common import tracing

        tracing.init_tracing("hermes-brain")
        tracer = tracing.get_tracer("hermes")
        with tracer.start_as_current_span(
                "hermes.chat",
                attributes={"chat.len": len(user_text)}):
            return await self._chat_loop(user_text)

    async def _chat_loop(self, user_text: str) -> str:
        self._ensure_collaboration(user_text)
        self._sync_user_interventions()
        user_message = {"role": "user", "content": user_text}
        self._persist_message(
            user_message, sender_type="user", sender_id="user",
            message_type="llm.user")
        self.messages.append(user_message)
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await self.llm.chat(self.messages, tools=TOOL_SCHEMAS)
            assistant_message = (reply.raw_message
                                 or {"role": "assistant",
                                     "content": reply.content})
            self._persist_message(
                assistant_message, sender_type="hermes", sender_id="hermes",
                message_type="llm.assistant")
            self.messages.append(assistant_message)
            if not reply.tool_calls:
                return reply.content
            for call in reply.tool_calls:
                log.info("tool call: %s(%s)", call.name, call.arguments)
                result = await self.tools.dispatch(call.name, call.arguments)
                tool_message = {
                    "role": "tool", "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                self._persist_message(
                    tool_message, sender_type="system", sender_id="tool",
                    message_type="llm.tool")
                self.messages.append(tool_message)
        return "（工具调用轮次超限，请缩小任务范围或分步进行）"
