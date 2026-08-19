"""Structured ActionIntent policy for agent-originated operations.

Natural-language objectives are not authoritative here. Decisions use an
exact operation ID, structured targets, workspace scope, and rollback plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_ACTION_PERMISSIONS = (
    Path(__file__).resolve().parents[2] / "config" / "action_permissions.yaml"
)


@dataclass(frozen=True)
class ActionDecision:
    route: str  # auto | hermes | user
    risk: str   # read | write | critical | unknown
    reason: str


class ActionPolicy:
    def __init__(self, *, workspace: Path,
                 permissions_path: Path | None = None):
        path = permissions_path or DEFAULT_ACTION_PERMISSIONS
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.workspace = workspace.expanduser().resolve()
        self.auto = frozenset(cfg.get("auto") or [])
        self.hermes_approve = frozenset(cfg.get("hermes_approve") or [])
        self.require_user = frozenset(cfg.get("require_user") or [])

        duplicates = ((self.auto & self.hermes_approve)
                      | (self.auto & self.require_user)
                      | (self.hermes_approve & self.require_user))
        if duplicates:
            raise ValueError(
                f"action operation appears in multiple policy groups: "
                f"{sorted(duplicates)}")

    @staticmethod
    def _target_strings(targets: list | dict) -> list[str]:
        if isinstance(targets, list):
            return [item for item in targets if isinstance(item, str)]
        if not isinstance(targets, dict):
            return []
        out: list[str] = []
        for key in ("path", "repo", "workspace"):
            value = targets.get(key)
            if isinstance(value, str):
                out.append(value)
        values = targets.get("paths")
        if isinstance(values, list):
            out.extend(item for item in values if isinstance(item, str))
        return out

    def _resolve_target(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def _within_workspace(self, targets: list | dict) -> tuple[bool, str]:
        strings = self._target_strings(targets)
        if not strings:
            return False, "修改操作缺少可验证的 path/repo target"
        for value in strings:
            path = self._resolve_target(value)
            try:
                path.relative_to(self.workspace)
            except ValueError:
                return False, f"目标超出工作区: {path}"
        return True, "目标均位于工作区"

    @staticmethod
    def _profile_value(profile: Any, key: str, default=None):
        if profile is None:
            return default
        if isinstance(profile, dict):
            return profile.get(key, default)
        try:
            return profile[key]
        except (KeyError, IndexError, TypeError):
            return default

    @classmethod
    def _profile_list(cls, profile: Any, key: str) -> list[str]:
        value = cls._profile_value(profile, key)
        if value is None:
            value = cls._profile_value(profile, f"{key}_json", [])
        if isinstance(value, str):
            try:
                import json

                value = json.loads(value)
            except (TypeError, ValueError):
                return []
        return [item for item in (value or []) if isinstance(item, str)]

    def _profile_scope(self, profile: Any,
                       targets: list | dict) -> tuple[bool, str]:
        roots = self._profile_list(profile, "workspace_roots")
        strings = self._target_strings(targets)
        if not roots or not strings:
            return True, "Profile 未追加路径限制"
        resolved_roots = [self._resolve_target(root) for root in roots]
        for value in strings:
            path = self._resolve_target(value)
            if not any(
                path == root or root in path.parents for root in resolved_roots
            ):
                return False, f"目标超出 Agent Profile 工作区: {path}"
        return True, "目标位于 Agent Profile 工作区"

    def _apply_profile(self, decision: ActionDecision, *, operation: str,
                       targets: list | dict, profile: Any) -> ActionDecision:
        if profile is None:
            return decision
        if self._profile_value(profile, "status", "active") != "active":
            return ActionDecision("user", "critical", "Agent Profile 未启用")
        denied = self._profile_list(profile, "denied_operations")
        if operation in denied:
            return ActionDecision(
                "user", "critical", f"Agent Profile 禁止操作: {operation}")
        allowed = self._profile_list(profile, "allowed_operations")
        if allowed and operation not in allowed:
            return ActionDecision(
                "user", "critical", f"操作不在 Agent Profile 白名单: {operation}")
        if (self._profile_value(profile, "execution_mode", "read_only")
                == "read_only" and decision.risk != "read"):
            return ActionDecision(
                "user", "critical", "Agent Profile 为只读模式")
        in_scope, reason = self._profile_scope(profile, targets)
        if not in_scope:
            return ActionDecision("user", "critical", reason)
        if (decision.route == "hermes"
                and self._profile_value(profile, "approval_level") == "user"):
            return ActionDecision(
                "user", "critical", "Agent Profile 要求用户批准修改操作")
        return decision

    def evaluate(self, *, operation: str, targets: list | dict,
                 rollback_plan: str | None,
                 profile: Any = None) -> ActionDecision:
        if operation in self.auto:
            decision = ActionDecision(
                "auto", "read", f"明确只读操作: {operation}")
            if self._target_strings(targets):
                in_scope, reason = self._within_workspace(targets)
                if not in_scope:
                    decision = ActionDecision("user", "critical", reason)
        elif operation in self.require_user:
            decision = ActionDecision(
                "user", "critical", f"策略要求用户批准: {operation}")
        elif operation not in self.hermes_approve:
            decision = ActionDecision(
                "user", "unknown", f"未知操作 fail-closed: {operation}")
        else:
            in_scope, reason = self._within_workspace(targets)
            if not in_scope:
                decision = ActionDecision("user", "critical", reason)
            elif not rollback_plan or not rollback_plan.strip():
                decision = ActionDecision(
                    "user", "critical", "修改操作没有明确回滚方案")
            else:
                decision = ActionDecision(
                    "hermes", "write", f"工作区内可回滚修改: {operation}")
        return self._apply_profile(
            decision, operation=operation, targets=targets, profile=profile)
