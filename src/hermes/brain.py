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
- 分析用户需求，拆解为任务（create_task），派给合适的 Worker（delegate_task）。
- Worker：codex（编码/测试/运维操作），kimi（调研/长上下文分析）。
- 任务完成后 wait_task 等结果，review_task 复审；不合格就返工（review approved=false 后重新委派）。
- 最终用简洁中文向用户汇报结果。

审批纪律（必须遵守）：
- delegate_task 返回 needs_approval 时，停下来用一句话向用户说明风险并询问是否批准。
- 用户说"批准/可以/做吧"→ 调 approve_and_delegate。
- 用户说"以后 X 类你自己批"→ 先调 grant_operation 记录常驻授权，再调 approve_and_delegate 完成本次。
- 用户拒绝 → 不要再委派，说明已取消。
- 一次只问一个审批问题。
"""

MAX_TOOL_ROUNDS = 12


class Hermes:
    def __init__(self, tm: TaskManager, llm: HermesLLM | None = None,
                 policy: ApprovalPolicy | None = None,
                 agents_path: Path | None = None):
        self.tm = tm
        self.llm = llm or HermesLLM()
        self.tools = HermesTools(tm, policy or ApprovalPolicy(), agents_path)
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT}]

    async def chat(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await self.llm.chat(self.messages, tools=TOOL_SCHEMAS)
            self.messages.append(reply.raw_message
                                 or {"role": "assistant",
                                     "content": reply.content})
            if not reply.tool_calls:
                return reply.content
            for call in reply.tool_calls:
                log.info("tool call: %s(%s)", call.name, call.arguments)
                result = await self.tools.dispatch(call.name, call.arguments)
                self.messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return "（工具调用轮次超限，请缩小任务范围或分步进行）"
