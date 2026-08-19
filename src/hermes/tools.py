"""Hermes 工具集 — brain 的工具调用循环分发到这里（Evolution v3 §6.1）。

每个工具 = JSON schema（给 LLM）+ 异步实现（操作 TaskManager / policy）。
审批语义：
  delegate_task 先做策略判定——auto/granted 直接委派；
  ask 则不动任务状态，返回 needs_approval，由 hermes 在对话里询问用户。
  用户批准后由 approve_and_delegate 显式放行（对话即审批记录）。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import yaml

from hermes.policy import ApprovalPolicy
from orchestrator.task_manager import TaskManager

DEFAULT_AGENTS = Path(__file__).resolve().parents[2] / "config" / "agents.yaml"


def load_agents(path: Path | None = None) -> dict:
    p = path or DEFAULT_AGENTS
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("agents", {})


TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "create_task",
        "description": "创建任务（可选父子/依赖关系）。返回 task_id。",
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string", "description": "任务目标"},
            "project": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "string"},
                           "description": "依赖的 task_id 列表"},
        }, "required": ["objective"]}}},
    {"type": "function", "function": {
        "name": "delegate_task",
        "description": "把任务委派给 Registry 中已注册的指定 agent。"
                       "写操作会先经过审批策略；返回 needs_approval 时"
                       "必须先询问用户，不得重复调用本工具。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "agent_id": {"type": "string"},
        }, "required": ["task_id", "agent_id"]}}},
    {"type": "function", "function": {
        "name": "approve_and_delegate",
        "description": "用户已在对话中批准后调用：记录批准并委派任务。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "agent_id": {"type": "string"},
            "note": {"type": "string", "description": "用户批准备注"},
        }, "required": ["task_id", "agent_id"]}}},
    {"type": "function", "function": {
        "name": "wait_task",
        "description": "等待任务到达终态（completed/failed/cancelled）。"
                       "长任务安全：轮询状态库，不占连接。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "timeout_seconds": {"type": "integer", "default": 600},
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "review_task",
        "description": "复审已完成任务：approved=true 验收（自动解锁依赖任务），"
                       "false 返工。验收前必须先用 get_task_artifacts 核对产物；"
                       "目标声明创建文件但产物清单无对应文件时，服务端会强制"
                       "驳回（veto），不得绕过。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "approved": {"type": "boolean"},
            "notes": {"type": "string"},
        }, "required": ["task_id", "approved"]}}},
    {"type": "function", "function": {
        "name": "get_task_artifacts",
        "description": "列出任务的实际产物清单（名称/类型/大小）。"
                       "复审前必查：worker 汇报声称创建的文件必须在此清单中。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "列出任务（可按状态过滤）。",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string"},
        }}}},
    {"type": "function", "function": {
        "name": "list_agents",
        "description": "列出可用 worker 及其技能。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "grant_operation",
        "description": "用户说『以后某类操作自动批准』时调用：创建常驻授权。",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string",
                        "description": "操作关键词，如 重启 / 部署"},
            "note": {"type": "string"},
        }, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "revoke_grant",
        "description": "撤销常驻授权。",
        "parameters": {"type": "object", "properties": {
            "grant_id": {"type": "integer"},
        }, "required": ["grant_id"]}}},
]


class HermesTools:
    def __init__(self, tm: TaskManager, policy: ApprovalPolicy,
                 agents_path: Path | None = None,
                 collaboration_id: str | None = None):
        self.tm = tm
        self.policy = policy
        self.agents = load_agents(agents_path)
        self.collaboration_id = collaboration_id

    def _resolve_agents(self) -> dict:
        """发现视图（v3 M2）：静态 agents.yaml 为种子，DB 中心跳注册的
        agent 覆盖/补充；带 online 标记（lease 未过期）。

        语义：worker 由用户自装、经心跳自注册；DB 中 lease 过期 = offline，
        未注册的 agent 仅在静态 yaml 显式配置时可用（static）。
        """
        import json as _json
        from datetime import datetime

        from state.db import CST

        now = datetime.now(CST).isoformat(timespec="seconds")
        merged = {k: {**v, "online": None} for k, v in self.agents.items()}
        for r in self.tm.conn.execute(
                "SELECT id, endpoint, skills_json, lease_expires_at,"
                " template_id, profile_id"
                " FROM agents;").fetchall():
            online = bool(r["lease_expires_at"] and r["lease_expires_at"] > now)
            entry = merged.setdefault(r["id"], {"endpoint": "", "skills": []})
            entry["online"] = online
            entry["template_id"] = r["template_id"]
            entry["profile_id"] = r["profile_id"]
            if online:
                if r["endpoint"]:
                    entry["endpoint"] = r["endpoint"]
                if r["skills_json"]:
                    try:
                        entry["skills"] = _json.loads(r["skills_json"])
                    except (ValueError, TypeError):
                        pass
        return merged

    def _agent_or_error(self, agent_id: str) -> dict:
        """解析可委派的 agent；离线/未知返回 error dict。"""
        agents = self._resolve_agents()
        info = agents.get(agent_id)
        if info is None:
            return {"error": f"unknown agent: {agent_id}",
                    "known": sorted(agents)}
        if info["online"] is False:
            return {"error": f"agent offline: {agent_id}（心跳租约已过期）"}
        if not info.get("endpoint"):
            return {"error": f"agent {agent_id} 无可用 endpoint（未注册）"}
        return info

    async def dispatch(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return await handler(**args)
        except Exception as e:  # 工具失败回报给 LLM 而非中断对话
            return {"error": f"{type(e).__name__}: {e}"}

    # ---------- 任务 ----------

    async def _tool_create_task(self, objective: str,
                                project: str | None = None,
                                depends_on: list[str] | None = None) -> dict:
        task_id = self.tm.create_task(objective, project=project,
                                      collaboration_id=self.collaboration_id,
                                      depends_on=depends_on)
        return {"task_id": task_id, "status": "created",
                "risk": self.policy.classify(objective)}

    async def _tool_delegate_task(self, task_id: str, agent_id: str) -> dict:
        row = self._task_or_error(task_id)
        if "error" in row:
            return row
        agent = self._agent_or_error(agent_id)
        if "error" in agent:
            return agent
        decision = self.policy.decide(self.tm.conn, row["objective"])
        if decision.action == "ask":
            return {"status": "needs_approval", "task_id": task_id,
                    "risk": decision.risk, "reason": decision.reason,
                    "hint": "请在对话中询问用户批准；用户同意后调用 "
                            "approve_and_delegate。"}
        if decision.action == "granted":
            self._record_auto_approval(task_id, decision)
        await self.tm.delegate_task(task_id, agent["endpoint"], agent_id)
        return {"status": "delegated", "task_id": task_id,
                "agent": agent_id, "approval": decision.action}

    async def _tool_approve_and_delegate(self, task_id: str, agent_id: str,
                                         note: str = "") -> dict:
        row = self._task_or_error(task_id)
        if "error" in row:
            return row
        agent = self._agent_or_error(agent_id)
        if "error" in agent:
            return agent
        # 对话即审批：记录批准事件后委派
        from orchestrator import state_store
        state_store.record_event(self.tm.conn, {
            "event_id": f"approval-{task_id}-{uuid4().hex[:8]}",
            "event_type": "task.approved", "task_id": task_id,
            "payload": {"by": "user", "note": note},
        })
        await self.tm.delegate_task(task_id, agent["endpoint"], agent_id)
        return {"status": "delegated", "task_id": task_id,
                "agent": agent_id, "approval": "user"}

    async def _tool_wait_task(self, task_id: str,
                              timeout_seconds: int = 600) -> dict:
        status = await self.tm.wait_task(task_id, timeout=timeout_seconds)
        row = self._task_or_error(task_id)
        return {"task_id": task_id, "status": status,
                "objective": row.get("objective"),
                "artifacts": self._artifacts_summary(task_id)}

    async def _tool_get_task_artifacts(self, task_id: str) -> dict:
        row = self._task_or_error(task_id)
        if "error" in row:
            return row
        return {"task_id": task_id,
                "artifacts": self._artifacts_summary(task_id)}

    async def _tool_review_task(self, task_id: str, approved: bool,
                                notes: str = "") -> dict:
        # 产物核验（防谎报，2026-08-17 T-20260817-0020 事故）：
        # 目标声明创建文件但产物清单无任何产出文件时，服务端强制驳回，
        # 不信任 LLM/worker 的文本汇报。
        if approved:
            row = self._task_or_error(task_id)
            if "error" in row:
                return row
            veto = self._artifact_veto_reason(row["objective"], task_id)
            if veto:
                new_status = self.tm.review_result(
                    task_id, approved=False, notes=veto)
                return {"task_id": task_id, "status": new_status,
                        "veto": veto,
                        "hint": "产物核验未通过，已按返工处理；请核实执行情况"
                                "后重新委派。"}
        new_status = self.tm.review_result(task_id, approved=approved,
                                           notes=notes)
        return {"task_id": task_id, "status": new_status}

    async def _tool_list_tasks(self, status: str | None = None) -> dict:
        from orchestrator.state_store import list_tasks
        rows = list_tasks(self.tm.conn, status=status)
        return {"tasks": [
            {"id": r["id"], "status": r["status"],
             "assigned_to": r["assigned_to"], "objective": r["objective"][:80]}
            for r in rows]}

    async def _tool_list_agents(self) -> dict:
        out = []
        for k, v in self._resolve_agents().items():
            status = {True: "online", False: "offline", None: "static"}[v["online"]]
            out.append({"id": k, "endpoint": v.get("endpoint", ""),
                        "skills": v.get("skills", []), "status": status,
                        "template_id": v.get("template_id"),
                        "profile_id": v.get("profile_id")})
        return {"agents": out}

    # ---------- 常驻授权 ----------

    async def _tool_grant_operation(self, pattern: str, note: str = "") -> dict:
        if any(k in pattern for k in self.policy.never_grant):
            return {"error": f"'{pattern}' 属高危类（never_grant），"
                             "不允许常驻授权"}
        gid = self.policy.grant(self.tm.conn, pattern, note=note)
        return {"grant_id": gid, "pattern": pattern, "status": "granted"}

    async def _tool_revoke_grant(self, grant_id: int) -> dict:
        ok = self.policy.revoke(self.tm.conn, grant_id)
        return {"grant_id": grant_id, "revoked": ok}

    # ---------- 内部 ----------

    # 运行日志/最终汇报不算"产出文件"
    _NON_PRODUCT_ARTIFACTS = {
        "codex.log", "codex.jsonl", "codex-app-server.jsonl",
        "kimi.jsonl", "kimi-acp.jsonl", "kimi-stderr.log",
        "dsh-history.json",
        "last-message.md",
    }

    def _artifacts_summary(self, task_id: str) -> list[dict]:
        from orchestrator import state_store
        return [{"name": a["name"], "type": a["type"]}
                for a in state_store.list_artifacts(self.tm.conn, task_id)]

    def _artifact_veto_reason(self, objective: str, task_id: str) -> str | None:
        """目标声明创建文件但无实际产出时返回驳回理由，否则 None。"""
        import re
        claims_file = (
            re.search(r"(创建|新建|写入|生成|保存|产出).{0,24}?"
                      r"(文件|\.md|\.txt|\.py|\.json|\.ya?ml|\.toml|\.csv)",
                      objective)
            or re.search(r"(创建|新建|写入|生成|保存)\s*[~\w./-]+\.\w{1,6}",
                         objective)
        )
        if not claims_file:
            return None
        produced = [a for a in self._artifacts_summary(task_id)
                    if a["name"] not in self._NON_PRODUCT_ARTIFACTS]
        if produced:
            return None
        return ("产物核验驳回：任务目标声明创建文件，但产物清单中没有"
                "任何产出文件（仅有运行日志/汇报）——疑似执行失败或"
                "worker 谎报完成。")

    def _task_or_error(self, task_id: str) -> dict:
        from orchestrator import state_store
        row = state_store.get_task(self.tm.conn, task_id)
        if row is None:
            return {"error": f"task not found: {task_id}"}
        return dict(row)

    def _record_auto_approval(self, task_id: str, decision) -> None:
        from orchestrator import state_store
        state_store.record_event(self.tm.conn, {
            "event_id": f"auto-approval-{task_id}-{decision.grant_id}",
            "event_type": "task.auto_approved", "task_id": task_id,
            "payload": {"grant_id": decision.grant_id,
                        "reason": decision.reason},
        })
