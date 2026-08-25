"""Hermes 工具集 — brain 的工具调用循环分发到这里（Evolution v3 §6.1）。

每个工具 = JSON schema（给 LLM）+ 异步实现（操作 TaskManager / policy）。
审批语义：
  delegate_task 先做策略判定——auto/granted 直接委派；
  ask 则不动任务状态，返回 needs_approval，由 hermes 在对话里询问用户。
  用户批准后由 approve_and_delegate 显式放行（对话即审批记录）。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import yaml

from hermes.policy import ApprovalPolicy
from orchestrator.task_manager import TaskManager, require_structured_workspace

DEFAULT_AGENTS = Path(__file__).resolve().parents[2] / "config" / "agents.yaml"


def load_agents(path: Path | None = None) -> dict:
    p = path or DEFAULT_AGENTS
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("agents", {})


TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "create_task_plan",
        "description": "创建并激活结构化多步骤 Task Plan；每步绑定 Agent/Profile、"
                       "依赖、预期操作、产物和验收条件。多 Agent/多步骤任务必须先调用。",
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string"},
            "project": {"type": "string"},
            "steps": {"type": "array", "minItems": 1, "maxItems": 50,
                      "items": {"type": "object", "properties": {
                          "key": {"type": "string"},
                          "objective": {"type": "string"},
                          "title": {"type": "string", "maxLength": 100},
                          "summary": {"type": "string", "maxLength": 500},
                          "agent_id": {"type": "string"},
                          "model": {"type": "string",
                                    "description": "必须来自 Agent Profile "
                                                   "allowed_models"},
                          "reasoning_effort": {"type": "string",
                                               "description": "必须来自 "
                                                              "Agent Profile "
                                                              "allowed_reasoning_efforts"},
                          "workspace": {"type": "string",
                                        "description": "现有代码仓库任务必须填写的绝对"
                                                       "工作区路径；不得只写在 objective；"
                                                       "写操作仍需 ActionIntent 审批"},
                          "depends_on": {"type": "array",
                                         "items": {"type": "string"}},
                          "expected_operations": {"type": "array",
                                                  "description": "精确 operation ID；"
                                                                 "必须来自 list_agents"
                                                                 " 的 Profile allowlist",
                                                  "items": {"type": "string"}},
                          "expected_artifacts": {"type": "array",
                                                 "items": {"type": "string"}},
                          "acceptance_criteria": {"type": "array",
                                                  "items": {"type": "string"}},
                      }, "required": ["key", "objective", "agent_id",
                                      "expected_operations",
                                      "acceptance_criteria"]}},
        }, "required": ["objective", "steps"]}}},
    {"type": "function", "function": {
        "name": "create_task",
        "description": "兼容路径：仅创建单 Agent 单步骤小任务。多 Agent/多步骤"
                       "必须使用 create_task_plan。返回 task_id。",
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string", "description": "任务目标"},
            "title": {"type": "string", "maxLength": 100,
                      "description": "面向 WebUI 的简洁任务标题"},
            "summary": {"type": "string", "maxLength": 500,
                        "description": "面向 WebUI 的简要说明"},
            "project": {"type": "string"},
            "agent_id": {"type": "string",
                         "description": "指定 model 或 reasoning_effort 时必填"},
            "model": {"type": "string"},
            "reasoning_effort": {"type": "string"},
            "workspace": {"type": "string",
                          "description": "现有代码仓库任务必须填写的绝对工作区路径；"
                                         "不得只写在 objective，否则原生工具请求无法批准"},
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
        "name": "respond_agent_interaction",
        "description": "回复 wait_task 返回的原生 Agent 交互。仅可批准 "
                       "inspectable=true 且 action_intent_status=awaiting_hermes "
                       "的请求；awaiting_user 必须由用户在 WebUI 决定。服务端会再次"
                       "校验权限并签发一次性 ActionIntent receipt。",
        "parameters": {"type": "object", "properties": {
            "interaction_id": {"type": "string"},
            "outcome": {"type": "string",
                        "enum": ["allowed-once", "rejected"]},
            "note": {"type": "string",
                     "maxLength": 2000,
                     "description": "核对目标、影响和回滚后的决策依据"},
        }, "required": ["interaction_id", "outcome"]}}},
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
        agent 覆盖/补充；带 online 标记（lease 未过期）。静态启停策略是
        desired state，优先级高于 worker 心跳；停用 Agent 保留可见状态
        但不参与委派。

        语义：worker 由用户自装、经心跳自注册；DB 中 lease 过期 = offline，
        未注册的 agent 仅在静态 yaml 显式配置时可用（static）。
        Registry-only 的动态 Agent 只在 lease 有效时出现；过期的测试或
        已卸载 worker 不应永久污染 Hermes 发现列表。
        """
        import json as _json
        from datetime import datetime

        from state.db import CST

        now = datetime.now(CST).isoformat(timespec="seconds")
        from orchestrator import agent_control_store

        merged = {}
        for agent_id, spec in self.agents.items():
            enabled = agent_control_store.desired_enabled(
                self.tm.conn, agent_id, spec.get("enabled", True))
            merged[agent_id] = {**spec, "enabled": enabled, "online": None}
        for r in self.tm.conn.execute(
                "SELECT id, endpoint, skills_json, lease_expires_at,"
                " template_id, profile_id"
                " FROM agents;").fetchall():
            online = bool(r["lease_expires_at"] and r["lease_expires_at"] > now)
            entry = merged.get(r["id"])
            if entry is None:
                if not online:
                    continue
                entry = {
                    "endpoint": "", "skills": [],
                    "enabled": agent_control_store.desired_enabled(
                        self.tm.conn, r["id"], True),
                }
                merged[r["id"]] = entry
            if entry.get("enabled") is False:
                entry["online"] = None
                continue
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
        if info.get("enabled") is False:
            return {
                "error": f"agent disabled: {agent_id}（生产安全门禁）",
                "status": "needs_confirmation",
                "reason": "agent_disabled",
                "agent_id": agent_id,
                "hint": (
                    f"{agent_id} 已停用；不要创建、委派或重试任务。"
                    "请询问用户是否先启用并重新探测，或改派其他已启用 Agent。"
                ),
            }
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

    async def _tool_create_task_plan(self, objective: str, steps: list[dict],
                                     project: str | None = None) -> dict:
        from orchestrator import agent_profile_store, task_plan_store

        if not self.collaboration_id:
            raise ValueError("Task Plan 必须属于持久 collaboration")
        task_plan_store.validate_steps(steps)
        prepared: list[dict] = []
        for step in steps:
            require_structured_workspace(
                step["objective"], step.get("workspace"))
            for key, value, maximum in (
                    ("title", step.get("title"), 100),
                    ("summary", step.get("summary"), 500)):
                if value is not None and (
                        not isinstance(value, str) or not value.strip()
                        or len(value.strip()) > maximum):
                    raise ValueError(
                        f"步骤 {step['key']} 的 {key} 必须是 "
                        f"1-{maximum} 字符的非空字符串")
            agent = self._agent_or_error(step["agent_id"])
            if "error" in agent:
                if agent.get("reason") == "agent_disabled":
                    return agent
                raise ValueError(agent["error"])
            profile_id = agent.get("profile_id")
            if not profile_id:
                raise ValueError(
                    f"agent {step['agent_id']} 尚未绑定 Agent Profile")
            profile = agent_profile_store.profile_policy(
                self.tm.conn, profile_id)
            if profile["status"] != "active":
                raise ValueError(f"Agent Profile 未启用: {profile_id}")
            operations = step["expected_operations"]
            allowed = set(profile.get("allowed_operations") or [])
            denied = set(profile.get("denied_operations") or [])
            disallowed = set(operations) & denied
            outside = set(operations) - allowed if allowed else set()
            if disallowed or outside:
                raise PermissionError(
                    f"步骤 {step['key']} 的操作超出 Profile: "
                    f"{sorted(disallowed | outside)}")
            from orchestrator.runtime_config import (
                normalize_runtime_config,
                resolve_runtime_config,
            )
            requested_runtime = normalize_runtime_config(
                step.get("model"), step.get("reasoning_effort"))
            runtime_config = resolve_runtime_config(
                self.tm.conn, agent_id=step["agent_id"],
                requested=requested_runtime)
            prepared.append({
                **step,
                "profile_id": profile_id,
                "profile_version": profile["version"],
                "profile_name": profile["name"],
                "role_prompt": profile.get("role_prompt"),
                "responsibilities": profile.get("responsibilities") or [],
                "timeout_seconds": profile["timeout_seconds"],
                "runtime_config": runtime_config,
            })

        resolved: list[dict] = []
        task_ids: dict[str, str] = {}
        for step in prepared:
            dependency_ids = [task_ids[key]
                              for key in step.get("depends_on") or []]
            context = {
                "plan_objective": objective,
                "step_key": step["key"],
                "agent_id": step["agent_id"],
                "profile_id": step["profile_id"],
                "profile_version": step["profile_version"],
                "profile_name": step["profile_name"],
                "role_prompt": step["role_prompt"],
                "responsibilities": step["responsibilities"],
                "expected_operations": step["expected_operations"],
                "expected_artifacts": step.get("expected_artifacts") or [],
                "acceptance_criteria": step["acceptance_criteria"],
                **({"display_title": step["title"].strip()}
                   if step.get("title") else {}),
                **({"objective_summary": step["summary"].strip()}
                   if step.get("summary") else {}),
                **({"execution_workspace": step["workspace"]}
                   if step.get("workspace") else {}),
                **({"runtime_config": step["runtime_config"]}
                   if step.get("runtime_config") else {}),
            }
            task_id = self.tm.create_task(
                step["objective"], project=project,
                collaboration_id=self.collaboration_id,
                depends_on=dependency_ids,
                timeout_seconds=step["timeout_seconds"],
                context=context,
                execution_workspace=step.get("workspace"),
            )
            task_ids[step["key"]] = task_id
            resolved.append({
                **step,
                "task_id": task_id,
                "profile_id": step["profile_id"],
                "profile_version": step["profile_version"],
            })
        plan = task_plan_store.create_plan(
            self.tm.conn, collaboration_id=self.collaboration_id,
            objective=objective, project=project, steps=resolved)
        return {
            "plan_id": plan["id"], "revision": plan["revision"],
            "status": plan["status"],
            "steps": [{"key": step["key"], "task_id": step["task_id"],
                       "agent_id": step["agent_id"],
                       "profile_id": step["profile_id"]}
                      for step in resolved],
        }

    async def _tool_create_task(self, objective: str,
                                project: str | None = None,
                                depends_on: list[str] | None = None,
                                workspace: str | None = None,
                                title: str | None = None,
                                summary: str | None = None,
                                agent_id: str | None = None,
                                model: str | None = None,
                                reasoning_effort: str | None = None) -> dict:
        require_structured_workspace(objective, workspace)
        for key, value, maximum in (
                ("title", title, 100), ("summary", summary, 500)):
            if value is not None and (
                    not isinstance(value, str) or not value.strip()
                    or len(value.strip()) > maximum):
                raise ValueError(
                    f"{key} 必须是 1-{maximum} 字符的非空字符串")
        from orchestrator.runtime_config import (
            normalize_runtime_config,
            resolve_runtime_config,
        )
        requested_runtime = normalize_runtime_config(model, reasoning_effort)
        if requested_runtime and not agent_id:
            raise ValueError(
                "agent_id is required with model or reasoning_effort")
        if requested_runtime and agent_id:
            agent = self._agent_or_error(agent_id)
            if "error" in agent:
                return agent
        runtime_config = (
            resolve_runtime_config(
                self.tm.conn, agent_id=agent_id,
                requested=requested_runtime)
            if requested_runtime and agent_id else None
        )
        task_id = self.tm.create_task(objective, project=project,
                                      collaboration_id=self.collaboration_id,
                                      depends_on=depends_on,
                                      context={
                                          **({"display_title": title.strip()}
                                             if title else {}),
                                          **({"objective_summary": summary.strip()}
                                             if summary else {}),
                                          **({"agent_id": agent_id}
                                             if runtime_config else {}),
                                          **({"runtime_config": runtime_config}
                                             if runtime_config else {}),
                                      } or None,
                                      execution_workspace=workspace)
        return {"task_id": task_id, "status": "created",
                "risk": self.policy.classify(objective)}

    async def _tool_delegate_task(self, task_id: str, agent_id: str) -> dict:
        row = self._task_or_error(task_id)
        if "error" in row:
            return row
        from orchestrator import task_plan_store

        task_plan_store.validate_delegation(
            self.tm.conn, task_id=task_id, agent_id=agent_id)
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
        from orchestrator import task_plan_store

        task_plan_store.validate_delegation(
            self.tm.conn, task_id=task_id, agent_id=agent_id)
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
                "artifacts": self._artifacts_summary(task_id),
                "pending_interactions": self._pending_interactions(task_id)}

    async def _tool_respond_agent_interaction(
            self, interaction_id: str, outcome: str,
            note: str = "") -> dict:
        if outcome not in {"allowed-once", "rejected"}:
            raise ValueError(
                "outcome must be allowed-once or rejected")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note 必须是至多 2000 字符的字符串")
        native_result = await self.tm.respond_agent_interaction(
            interaction_id,
            response={"outcome": outcome, "note": note},
            requested_by="hermes",
        )
        return {
            "status": "responded", "interaction_id": interaction_id,
            "outcome": outcome, "native_result": native_result,
        }

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
        from orchestrator import agent_profile_store

        out = []
        for k, v in self._resolve_agents().items():
            status = (
                "disabled" if v.get("enabled") is False else
                {True: "online", False: "offline", None: "static"}[
                    v["online"]]
            )
            profile = None
            if v.get("profile_id"):
                profile = agent_profile_store.profile_policy(
                    self.tm.conn, v["profile_id"])
            out.append({"id": k, "endpoint": v.get("endpoint", ""),
                        "skills": v.get("skills", []), "status": status,
                        "template_id": v.get("template_id"),
                        "profile_id": v.get("profile_id"),
                        "profile": ({
                            "version": profile["version"],
                            "name": profile["name"],
                            "execution_mode": profile["execution_mode"],
                            "responsibilities": profile["responsibilities"],
                            "allowed_operations": profile["allowed_operations"],
                            "denied_operations": profile["denied_operations"],
                            "model": profile.get("model"),
                            "allowed_models": profile.get("allowed_models") or [],
                            "reasoning_effort": profile.get(
                                "reasoning_effort"),
                            "allowed_reasoning_efforts": profile.get(
                                "allowed_reasoning_efforts") or [],
                            "status": profile["status"],
                        } if profile else None)})
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

    def _pending_interactions(self, task_id: str) -> list[dict]:
        from orchestrator import collaboration_store

        return collaboration_store.pending_interaction_views(
            self.tm.conn, task_id)

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
