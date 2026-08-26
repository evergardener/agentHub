"""Persistent collaboration/session/message store (ADR-0004).

Messages are assigned a per-conversation monotonic sequence before delivery.
User interventions advance context_revision so stale write intents are denied.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from common.models import CollaborationPhase, InterventionMode
from state.db import now_iso


class ContextConflict(RuntimeError):
    def __init__(self, collaboration_id: str, expected: int, actual: int):
        super().__init__(
            f"stale collaboration context {collaboration_id}: "
            f"based_on_revision={expected}, current={actual}"
        )
        self.collaboration_id = collaboration_id
        self.expected = expected
        self.actual = actual


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# A2A callers need enough detail to audit a native approval, but must not get
# an unbounded adapter payload or accidentally receive credentials.  These
# limits apply to the public interaction views only; the durable payload keeps
# the adapter's bounded record for internal investigation.
_A2A_DETAIL_TEXT_LIMIT = 4096
_A2A_COMMAND_LIMIT = 256
_A2A_ARG_LIMIT = 2048
_A2A_MAX_ARGS = 64
_A2A_MAX_PATHS = 100
_A2A_TOOL_VIEW_LIMIT = 16 * 1024
_A2A_SENSITIVE_KEY = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key)",
    re.I,
)
_A2A_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:])"
    r"[^\s,;]+"
)


def _safe_a2a_text(value: Any, *, limit: int) -> tuple[str | None, bool]:
    """Return one bounded, non-secret string for an external A2A view."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None, False
    if "\x00" in value or _A2A_SECRET_TEXT.search(value):
        return None, False
    return value, True


def _safe_a2a_paths(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list) or len(value) > _A2A_MAX_PATHS:
        return [], False
    result: list[str] = []
    for item in value:
        safe, valid = _safe_a2a_text(item, limit=_A2A_DETAIL_TEXT_LIMIT)
        if not valid:
            return [], False
        result.append(safe)
    return result, True


def _safe_a2a_args(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list) or len(value) > _A2A_MAX_ARGS:
        return [], False
    result: list[str] = []
    for item in value:
        safe, valid = _safe_a2a_text(item, limit=_A2A_ARG_LIMIT)
        if not valid:
            return [], False
        result.append(safe)
    return result, True


def _contains_sensitive_key(value: Any) -> bool:
    """Fail closed when an adapter view contains credential-like fields."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 8:
            return True
        if isinstance(item, dict):
            for key, child in item.items():
                if _A2A_SENSITIVE_KEY.search(str(key)):
                    return True
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item[:100])
    return False


def _safe_tool_view(value: Any) -> tuple[dict | None, bool]:
    """Keep only bounded presentation fields; ignore opaque adapter extras."""
    if not isinstance(value, dict):
        return None, False
    if _contains_sensitive_key(value):
        return None, False
    try:
        if len(_json(value)) > _A2A_TOOL_VIEW_LIMIT:
            return None, False
    except (TypeError, ValueError):
        return None, False

    safe: dict[str, Any] = {}
    for key in ("card", "kind", "title", "command", "cwd", "paths"):
        if key not in value:
            continue
        item = value[key]
        if key == "paths":
            paths, valid = _safe_a2a_paths(item)
            if not valid:
                return None, False
            safe[key] = paths
            continue
        text, valid = _safe_a2a_text(
            item,
            limit=_A2A_COMMAND_LIMIT if key == "command" else
            _A2A_DETAIL_TEXT_LIMIT,
        )
        if not valid:
            return None, False
        safe[key] = text
    return safe, True


def _safe_interaction_details(
    *, payload: dict, targets: Any, operation: str | None,
    rollback_plan: Any, inspectable: bool,
) -> dict[str, Any]:
    """Build the bounded public fields shared by tasks/get and interactions/get."""
    raw_targets = targets if isinstance(targets, dict) else {}
    safe_targets: dict[str, Any] = {}
    details_valid = isinstance(targets, dict)

    raw_workspace = raw_targets.get("workspace")
    workspace, workspace_valid = (
        (None, True) if raw_workspace is None else _safe_a2a_text(
            raw_workspace, limit=_A2A_DETAIL_TEXT_LIMIT)
    )
    if workspace is not None:
        safe_targets["workspace"] = workspace
    details_valid = details_valid and workspace_valid

    raw_paths = raw_targets.get("paths")
    paths, paths_valid = (
        ([], True) if raw_paths is None else _safe_a2a_paths(raw_paths)
    )
    safe_targets["paths"] = paths
    details_valid = details_valid and paths_valid

    raw_cwd = raw_targets.get("cwd")
    cwd, cwd_valid = (
        (None, True) if raw_cwd is None else _safe_a2a_text(
            raw_cwd, limit=_A2A_DETAIL_TEXT_LIMIT)
    )
    if cwd is not None:
        safe_targets["cwd"] = cwd
    details_valid = details_valid and cwd_valid

    raw_command = raw_targets.get("command")
    command, command_valid = (
        (None, True) if raw_command is None else _safe_a2a_text(
            raw_command, limit=_A2A_COMMAND_LIMIT)
    )
    raw_args = raw_targets.get("args")
    args, args_valid = (
        ([], True) if raw_args is None else _safe_a2a_args(raw_args)
    )
    if command is not None:
        safe_targets["command"] = command
    if raw_args is not None or operation == "command.read":
        safe_targets["args"] = args
    details_valid = details_valid and command_valid and args_valid

    safe_rollback, rollback_valid = _safe_a2a_text(
        rollback_plan, limit=_A2A_DETAIL_TEXT_LIMIT)
    if rollback_plan is None:
        rollback_valid = True
    details_valid = details_valid and rollback_valid

    raw_tool = payload.get("toolView")
    safe_tool, tool_valid = (
        (None, True) if raw_tool is None else _safe_tool_view(raw_tool)
    )
    # command.read is only allowed when its canonical argv and workspace
    # fields are complete and safe.  Other interaction kinds retain the
    # adapter's inspectable bit, while malformed command detail always fails
    # closed.
    command_read = operation == "command.read"
    if command_read:
        details_valid = (
            details_valid and command is not None and bool(args) and bool(paths)
            and cwd is not None and workspace is not None
            and tool_valid
        )
    inspectable_value = bool(inspectable and details_valid)

    return {
        "inspectable": inspectable_value,
        "tool_view": safe_tool if tool_valid else None,
        "targets": safe_targets,
        "command": command if command_read else None,
        "args": args if command_read else [],
        "cwd": cwd if command_read else None,
        "workspace": workspace,
        "rollback_plan": safe_rollback,
        "details_valid": details_valid,
    }


def _audit(conn, event_type: str, *, task_id: str | None,
           source: str, payload: dict) -> None:
    from orchestrator import state_store

    state_store.record_event(conn, {
        "event_id": _id("E"),
        "event_type": event_type,
        "task_id": task_id,
        "source": source,
        "payload": payload,
    }, commit=False)


def create_conversation(conn, *, title: str | None = None,
                        project: str | None = None,
                        created_by: str = "user",
                        conversation_id: str | None = None) -> str:
    conversation_id = conversation_id or _id("C")
    ts = now_iso()
    conn.execute(
        "INSERT INTO conversations (id, title, project, status, created_by,"
        " next_message_seq, created_at, updated_at)"
        " VALUES (?,?,?,'active',?,0,?,?);",
        (conversation_id, title, project, created_by, ts, ts),
    )
    conn.commit()
    return conversation_id


def create_collaboration(conn, *, conversation_id: str, objective: str,
                         controller: str = "hermes",
                         collaboration_id: str | None = None) -> str:
    if get_conversation(conn, conversation_id) is None:
        raise KeyError(f"conversation not found: {conversation_id}")
    collaboration_id = collaboration_id or _id("COL")
    ts = now_iso()
    conn.execute(
        "INSERT INTO collaborations (id, conversation_id, objective, status,"
        " phase, controller, context_revision, created_at, updated_at)"
        " VALUES (?,?,?,'active',?,?,1,?,?);",
        (collaboration_id, conversation_id, objective,
         CollaborationPhase.PLANNING.value, controller, ts, ts),
    )
    conn.commit()
    return collaboration_id


def a2a_context_ids(*, peer: str, context_id: str) -> dict[str, str]:
    """Validate one peer context and return its deterministic internal IDs."""
    peer = peer.strip()
    context_id = context_id.strip()
    if not peer or len(peer) > 128:
        raise ValueError("A2A peer must contain 1..128 characters")
    if not context_id or len(context_id) > 512:
        raise ValueError("A2A contextId must contain 1..512 characters")
    digest = hashlib.sha256(
        f"{peer}\0{context_id}".encode("utf-8")
    ).hexdigest()[:32]
    conversation_id = f"C-A2A-{digest}"
    collaboration_id = f"COL-A2A-{digest}"
    return {
        "conversation_id": conversation_id,
        "collaboration_id": collaboration_id,
        "peer": peer,
        "context_id": context_id,
    }


def require_a2a_task(conn, *, task_id: str, peer: str,
                     context_id: str):
    """Return a task only when it belongs to this authenticated A2A context."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id is required")
    mapping = a2a_context_ids(peer=peer, context_id=context_id)
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ?;", (task_id.strip(),)
    ).fetchone()
    if task is None:
        raise KeyError(f"task not found: {task_id}")
    if task["collaboration_id"] != mapping["collaboration_id"]:
        raise PermissionError("task does not belong to this peer/context")
    collaboration = get_collaboration(conn, task["collaboration_id"])
    conversation = (
        get_conversation(conn, collaboration["conversation_id"])
        if collaboration is not None else None
    )
    if (
        collaboration is None
        or collaboration["conversation_id"] != mapping["conversation_id"]
        or conversation is None
        or conversation["created_by"] != f"a2a:{mapping['peer']}"
    ):
        raise PermissionError("task does not belong to this peer/context")
    return task


def require_a2a_interaction(conn, *, interaction_id: str, peer: str,
                            context_id: str):
    """Return one interaction after enforcing peer/context/task ownership."""
    if not isinstance(interaction_id, str) or not interaction_id.strip():
        raise ValueError("interaction_id is required")
    interaction = get_session_interaction(conn, interaction_id.strip())
    if interaction is None:
        raise KeyError(f"interaction not found: {interaction_id}")
    require_a2a_task(
        conn, task_id=interaction["task_id"], peer=peer, context_id=context_id)
    return interaction


def ensure_a2a_collaboration(
    conn,
    *,
    peer: str,
    context_id: str,
    objective: str,
    project: str | None = None,
    commit: bool = True,
) -> dict[str, str]:
    """Atomically map one authenticated A2A peer context to a collaboration.

    A2A ``contextId`` is scoped by peer identity.  Stable hashed IDs provide a
    migration-free, race-safe mapping while avoiding raw caller-controlled
    context values in primary keys.  A collision with unrelated pre-existing
    rows fails closed instead of silently joining two conversations.
    """
    mapping = a2a_context_ids(peer=peer, context_id=context_id)
    peer = mapping["peer"]
    context_id = mapping["context_id"]
    conversation_id = mapping["conversation_id"]
    collaboration_id = mapping["collaboration_id"]
    created_by = f"a2a:{peer}"
    ts = now_iso()
    title = objective[:80] or f"A2A session {context_id[:32]}"
    try:
        conn.execute(
            "INSERT INTO conversations (id, title, project, status, created_by,"
            " next_message_seq, created_at, updated_at)"
            " VALUES (?,?,?,'active',?,0,?,?)"
            " ON CONFLICT(id) DO NOTHING;",
            (conversation_id, title, project, created_by, ts, ts),
        )
        conn.execute(
            "INSERT INTO collaborations (id, conversation_id, objective, status,"
            " phase, controller, context_revision, created_at, updated_at)"
            " VALUES (?,?,?,'active',?,?,1,?,?)"
            " ON CONFLICT(id) DO NOTHING;",
            (collaboration_id, conversation_id, objective,
             CollaborationPhase.PLANNING.value, "hermes", ts, ts),
        )
        conversation = get_conversation(conn, conversation_id)
        collaboration = get_collaboration(conn, collaboration_id)
        if (conversation is None
                or conversation["created_by"] != created_by
                or collaboration is None
                or collaboration["conversation_id"] != conversation_id):
            raise RuntimeError("A2A context mapping collision")
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return mapping


def get_conversation(conn, conversation_id: str):
    return conn.execute(
        "SELECT * FROM conversations WHERE id = ?;", (conversation_id,)
    ).fetchone()


def get_collaboration(conn, collaboration_id: str):
    return conn.execute(
        "SELECT * FROM collaborations WHERE id = ?;", (collaboration_id,)
    ).fetchone()


def set_phase(conn, collaboration_id: str, phase: CollaborationPhase,
              *, controller: str | None = None) -> None:
    extra = ", controller = ?" if controller else ""
    params: list[Any] = [phase.value, now_iso()]
    if controller:
        params.append(controller)
    params.append(collaboration_id)
    cur = conn.execute(
        f"UPDATE collaborations SET phase = ?, updated_at = ?{extra}"
        " WHERE id = ?;", params,
    )
    conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"collaboration not found: {collaboration_id}")


def _current_revision(conn, collaboration_id: str) -> tuple[str, int]:
    row = get_collaboration(conn, collaboration_id)
    if row is None:
        raise KeyError(f"collaboration not found: {collaboration_id}")
    return row["conversation_id"], row["context_revision"]


def _next_message_sequence(conn, conversation_id: str) -> int:
    row = conn.execute(
        "UPDATE conversations SET next_message_seq = next_message_seq + 1,"
        " updated_at = ? WHERE id = ? RETURNING next_message_seq;",
        (now_iso(), conversation_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"conversation not found: {conversation_id}")
    return row[0]


def _insert_message(conn, *, conversation_id: str,
                    collaboration_id: str | None, sender_type: str,
                    sender_id: str, content: dict | list | str,
                    sequence: int, message_id: str,
                    task_id: str | None = None,
                    agent_id: str | None = None,
                    recipient_type: str | None = None,
                    recipient_id: str | None = None,
                    message_type: str = "message",
                    parent_message_id: str | None = None,
                    based_on_revision: int | None = None,
                    idempotency_key: str | None = None,
                    visibility: str = "participants") -> None:
    conn.execute(
        "INSERT INTO conversation_messages (id, conversation_id,"
        " collaboration_id, task_id, agent_id, sender_type, sender_id,"
        " recipient_type, recipient_id, message_type, content_json,"
        " parent_message_id, based_on_revision, sequence, delivery_status,"
        " visibility, redaction_status, idempotency_key, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'persisted',?,'none',?,?);",
        (message_id, conversation_id, collaboration_id, task_id, agent_id,
         sender_type, sender_id, recipient_type, recipient_id, message_type,
         _json(content), parent_message_id, based_on_revision, sequence,
         visibility, idempotency_key, now_iso()),
    )


def append_message(conn, *, conversation_id: str,
                   sender_type: str, sender_id: str,
                   content: dict | list | str,
                   collaboration_id: str | None = None,
                   task_id: str | None = None,
                   agent_id: str | None = None,
                   recipient_type: str | None = None,
                   recipient_id: str | None = None,
                   message_type: str = "message",
                   parent_message_id: str | None = None,
                   based_on_revision: int | None = None,
                   idempotency_key: str | None = None,
                   visibility: str = "participants",
                   commit: bool = True):
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM conversation_messages WHERE idempotency_key = ?;",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return existing

    if collaboration_id:
        actual_conversation, current = _current_revision(conn, collaboration_id)
        if actual_conversation != conversation_id:
            raise ValueError("collaboration does not belong to conversation")
        if based_on_revision is not None and based_on_revision != current:
            raise ContextConflict(collaboration_id, based_on_revision, current)

    message_id = _id("M")
    try:
        seq = _next_message_sequence(conn, conversation_id)
        _insert_message(
            conn, conversation_id=conversation_id,
            collaboration_id=collaboration_id, task_id=task_id,
            agent_id=agent_id, sender_type=sender_type, sender_id=sender_id,
            recipient_type=recipient_type, recipient_id=recipient_id,
            content=content, sequence=seq, message_id=message_id,
            message_type=message_type, parent_message_id=parent_message_id,
            based_on_revision=based_on_revision,
            idempotency_key=idempotency_key, visibility=visibility,
        )
        _audit(
            conn, "conversation.message.created", task_id=task_id,
            source=sender_id,
            payload={
                "message_id": message_id,
                "conversation_id": conversation_id,
                "collaboration_id": collaboration_id,
                "sequence": seq,
                "message_type": message_type,
                "based_on_revision": based_on_revision,
                "visibility": visibility,
            })
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM conversation_messages"
                " WHERE idempotency_key = ?;", (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return existing
        raise
    return conn.execute(
        "SELECT * FROM conversation_messages WHERE id = ?;", (message_id,)
    ).fetchone()


def record_user_intervention(conn, *, collaboration_id: str,
                             user_id: str, mode: str,
                             content: dict | list | str,
                             task_id: str | None = None,
                             agent_id: str | None = None,
                             idempotency_key: str | None = None):
    """Persist an intervention and atomically advance context revision."""
    try:
        intervention_mode = InterventionMode(mode)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in InterventionMode)
        raise ValueError(
            f"unsupported intervention mode: {mode}; allowed: {allowed}"
        ) from exc
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM conversation_messages WHERE idempotency_key = ?;",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return existing

    conversation_id, _ = _current_revision(conn, collaboration_id)
    phase = (CollaborationPhase.PAUSED.value
             if intervention_mode in {
                 InterventionMode.PAUSE,
                 InterventionMode.INTERRUPT,
                 InterventionMode.CANCEL,
             }
             else CollaborationPhase.NEEDS_REPLAN.value)
    message_id = _id("M")
    try:
        rev_row = conn.execute(
            "UPDATE collaborations SET context_revision = context_revision + 1,"
            " phase = ?, controller = 'user', updated_at = ? WHERE id = ?"
            " RETURNING context_revision;",
            (phase, now_iso(), collaboration_id),
        ).fetchone()
        if rev_row is None:
            raise KeyError(f"collaboration not found: {collaboration_id}")
        revision = rev_row[0]
        seq = _next_message_sequence(conn, conversation_id)
        recipient_is_hermes = intervention_mode in {
            InterventionMode.COMMENT,
            InterventionMode.TAKEOVER,
            InterventionMode.RETURN_TO_HERMES,
        }
        _insert_message(
            conn, conversation_id=conversation_id,
            collaboration_id=collaboration_id, task_id=task_id,
            agent_id=agent_id, sender_type="user", sender_id=user_id,
            recipient_type=("hermes" if recipient_is_hermes or not agent_id
                            else "session"),
            recipient_id=("hermes" if recipient_is_hermes or not agent_id
                          else agent_id),
            content=content,
            sequence=seq, message_id=message_id,
            message_type=f"user.{intervention_mode.value}",
            based_on_revision=revision,
            idempotency_key=idempotency_key,
        )
        _audit(
            conn, "conversation.message.created", task_id=task_id,
            source=user_id,
            payload={
                "message_id": message_id,
                "conversation_id": conversation_id,
                "collaboration_id": collaboration_id,
                "sequence": seq,
                "message_type": f"user.{intervention_mode.value}",
                "based_on_revision": revision,
                "visibility": "participants",
            })
        _audit(
            conn, "user.intervened", task_id=task_id, source=user_id,
            payload={
                "message_id": message_id,
                "collaboration_id": collaboration_id,
                "agent_id": agent_id,
                "mode": intervention_mode.value,
                "context_revision": revision,
                "phase": phase,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute(
        "SELECT * FROM conversation_messages WHERE id = ?;", (message_id,)
    ).fetchone()


def list_messages(conn, conversation_id: str, *, after: int = 0,
                  limit: int = 200):
    return conn.execute(
        "SELECT * FROM conversation_messages WHERE conversation_id = ?"
        " AND sequence > ? ORDER BY sequence LIMIT ?;",
        (conversation_id, after, limit),
    ).fetchall()


def list_collaboration_messages(conn, collaboration_id: str, *,
                                after: int = 0, limit: int = 1000):
    return conn.execute(
        "SELECT * FROM conversation_messages WHERE collaboration_id = ?"
        " AND sequence > ? ORDER BY sequence LIMIT ?;",
        (collaboration_id, after, limit),
    ).fetchall()


def bind_agent_session(conn, *, collaboration_id: str, task_id: str,
                       agent_id: str, native_session_id: str | None = None,
                       adapter_session_id: str | None = None,
                       adapter_instance_id: str | None = None,
                       resume_capability: str = "unknown",
                       capabilities: dict | None = None,
                       recovery_state: str = "none",
                       replacement_of_id: str | None = None,
                       context_snapshot: dict | None = None,
                       commit: bool = True):
    """Create the current binding, retaining replaced bindings for audit."""
    _, context_revision = _current_revision(conn, collaboration_id)
    binding_id = _id("S")
    ts = now_iso()
    try:
        conn.execute(
            "UPDATE agent_session_bindings SET is_current = 0, status ="
            " 'replaced', last_active_at = ? WHERE task_id = ? AND agent_id = ?"
            " AND is_current = 1;", (ts, task_id, agent_id),
        )
        conn.execute(
            "INSERT INTO agent_session_bindings (id, collaboration_id, task_id,"
            " agent_id, native_session_id, adapter_session_id,"
            " adapter_instance_id, status,"
            " resume_capability, context_revision, last_message_seq,"
            " context_snapshot_json, capabilities_json, recovery_state,"
            " replacement_of_id,"
            " is_current, created_at, last_active_at)"
            " VALUES (?,?,?,?,?,?,?,'active',?,?,0,?,?,?,?,1,?,?);",
            (binding_id, collaboration_id, task_id, agent_id,
             native_session_id, adapter_session_id, adapter_instance_id,
             resume_capability,
             context_revision,
             _json(context_snapshot) if context_snapshot is not None else None,
             _json(capabilities or {}), recovery_state, replacement_of_id,
             ts, ts),
        )
        _audit(
            conn, "agent.session.bound", task_id=task_id, source="hermes",
            payload={
                "binding_id": binding_id,
                "collaboration_id": collaboration_id,
                "agent_id": agent_id,
                "adapter_session_id": adapter_session_id,
                "native_session_id": native_session_id,
                "resume_capability": resume_capability,
                "recovery_state": recovery_state,
                "replacement_of_id": replacement_of_id,
            })
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_current_agent_session(conn, task_id, agent_id)


def upsert_agent_session(conn, *, collaboration_id: str, task_id: str,
                         agent_id: str, adapter_session_id: str,
                         native_session_id: str | None = None,
                         capabilities: dict | None = None,
                         resume_capability: str = "unknown",
                         recovery_state: str = "none",
                         context_snapshot: dict | None = None,
                         adapter_instance_id: str | None = None,
                         commit: bool = True):
    """Idempotently refresh the current binding or create a replacement."""
    current = get_current_agent_session(conn, task_id, agent_id)
    if (current is not None
            and current["adapter_session_id"] == adapter_session_id):
        try:
            resolved_native = native_session_id or current["native_session_id"]
            conn.execute(
                "UPDATE agent_session_bindings SET native_session_id = ?,"
                " adapter_instance_id = ?, resume_capability = ?,"
                " capabilities_json = ?, recovery_state = ?, status = 'active',"
                " context_revision = ?, context_snapshot_json = COALESCE(?,"
                " context_snapshot_json), last_error = NULL, last_active_at = ?"
                " WHERE id = ?;",
                (resolved_native,
                 adapter_instance_id or current["adapter_instance_id"],
                 resume_capability, _json(capabilities or {}), recovery_state,
                 _current_revision(conn, collaboration_id)[1],
                 (_json(context_snapshot)
                  if context_snapshot is not None else None),
                 now_iso(), current["id"]),
            )
            if (resolved_native != current["native_session_id"]
                    or recovery_state != current["recovery_state"]):
                _audit(
                    conn, "agent.session.refreshed", task_id=task_id,
                    source="hermes",
                    payload={
                        "binding_id": current["id"],
                        "agent_id": agent_id,
                        "adapter_session_id": adapter_session_id,
                        "native_session_id": resolved_native,
                        "recovery_state": recovery_state,
                    })
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        return get_current_agent_session(conn, task_id, agent_id)

    return bind_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id=agent_id, adapter_session_id=adapter_session_id,
        native_session_id=native_session_id,
        adapter_instance_id=adapter_instance_id,
        resume_capability=resume_capability, capabilities=capabilities,
        recovery_state=recovery_state,
        replacement_of_id=current["id"] if current is not None else None,
        context_snapshot=context_snapshot, commit=commit)


def update_agent_session_status(conn, binding_id: str, *, status: str,
                                recovery_state: str | None = None,
                                error: str | None = None,
                                commit: bool = True) -> None:
    assignments = ["status = ?", "last_error = ?", "last_active_at = ?"]
    params: list[Any] = [status, error, now_iso()]
    if recovery_state is not None:
        assignments.append("recovery_state = ?")
        params.append(recovery_state)
    params.append(binding_id)
    cur = conn.execute(
        f"UPDATE agent_session_bindings SET {', '.join(assignments)}"
        " WHERE id = ?;", params)
    if cur.rowcount != 1:
        conn.rollback()
        raise KeyError(f"agent session binding not found: {binding_id}")
    if commit:
        conn.commit()


def advance_agent_session(conn, binding_id: str, *, message_seq: int,
                          context_revision: int) -> None:
    """Acknowledge a delivered turn without allowing counters to move back."""
    cur = conn.execute(
        "UPDATE agent_session_bindings SET last_message_seq = CASE"
        " WHEN last_message_seq < ? THEN ? ELSE last_message_seq END,"
        " context_revision = CASE WHEN context_revision < ? THEN ?"
        " ELSE context_revision END, last_active_at = ? WHERE id = ?;",
        (message_seq, message_seq, context_revision, context_revision,
         now_iso(), binding_id),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise KeyError(f"agent session binding not found: {binding_id}")


def session_recovery_plan(binding) -> str:
    """Return native_resume, replacement, or blocked without side effects."""
    if binding is None:
        return "new"
    if binding["status"] == "canceled":
        return "blocked"
    try:
        capabilities = json.loads(binding["capabilities_json"] or "{}")
    except (TypeError, ValueError):
        capabilities = {}
    if (binding["native_session_id"]
            and capabilities.get("native_resume") is True):
        return "native_resume"
    if binding["context_snapshot_json"]:
        return "replacement"
    return "blocked"


def get_current_agent_session(conn, task_id: str, agent_id: str):
    return conn.execute(
        "SELECT * FROM agent_session_bindings WHERE task_id = ? AND agent_id = ?"
        " AND is_current = 1;", (task_id, agent_id),
    ).fetchone()


def upsert_session_interaction(
    conn,
    *,
    collaboration_id: str,
    task_id: str,
    session_binding_id: str,
    agent_id: str,
    interaction: dict,
    commit: bool = True,
):
    """Persist an adapter interaction once and preserve its decision state."""
    adapter_id = interaction.get("interactionId")
    kind = interaction.get("kind")
    if not isinstance(adapter_id, str) or not adapter_id:
        raise ValueError("interactionId is required")
    if kind not in {"approval", "question"}:
        raise ValueError(f"unsupported interaction kind: {kind}")
    existing = conn.execute(
        "SELECT * FROM agent_session_interactions"
        " WHERE session_binding_id = ? AND adapter_interaction_id = ?;",
        (session_binding_id, adapter_id),
    ).fetchone()
    if existing is not None:
        return existing

    interaction_id = _id("INT")
    payload = interaction.get("payload") or {}
    try:
        conn.execute(
            "INSERT INTO agent_session_interactions (id, collaboration_id,"
            " task_id, session_binding_id, agent_id, adapter_interaction_id,"
            " native_request_id, kind, payload_json, status, requested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'pending',?);",
            (interaction_id, collaboration_id, task_id, session_binding_id,
             agent_id, adapter_id, interaction.get("nativeRequestId"), kind,
             _json(payload), now_iso()),
        )
        _audit(
            conn, "agent.interaction.requested", task_id=task_id,
            source=agent_id,
            payload={
                "interaction_id": interaction_id,
                "adapter_interaction_id": adapter_id,
                "binding_id": session_binding_id,
                "kind": kind,
                "native_request_id": interaction.get("nativeRequestId"),
            },
        )
        # Keep the lifecycle wakeup in the same transaction as the durable
        # interaction record.  This also covers adapters or recovery paths
        # that persist an interaction without a separate task.input_required
        # event reaching StateWriter.
        from orchestrator import supervision_store

        supervision_store.sync_task(conn, task_id, commit=False)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_session_interaction(conn, interaction_id)


def get_session_interaction(conn, interaction_id: str):
    return conn.execute(
        "SELECT * FROM agent_session_interactions WHERE id = ?;",
        (interaction_id,),
    ).fetchone()


def list_session_interactions(conn, *, task_id: str | None = None,
                              status: str | None = None):
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        "SELECT * FROM agent_session_interactions" + where
        + " ORDER BY requested_at, id;", params,
    ).fetchall()


def _interaction_view_from_row(row) -> dict:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        targets = json.loads(row["targets_json"] or "null")
    except (TypeError, ValueError):
        targets = None
    details = _safe_interaction_details(
        payload=payload,
        targets=targets,
        operation=row["operation"],
        rollback_plan=row["rollback_plan"],
        inspectable=payload.get("inspectable") is True,
    )
    if row["kind"] == "approval":
        allowed_responses = ["allowed-once", "rejected"]
    else:
        raw_options = payload.get("options")
        allowed_responses = []
        if isinstance(raw_options, list) and len(raw_options) <= 32:
            for option in raw_options:
                safe_option, valid = _safe_a2a_text(option, limit=256)
                if not valid:
                    allowed_responses = []
                    break
                allowed_responses.append(safe_option)
    action_status = row["action_intent_status"]
    tool_name, tool_name_valid = _safe_a2a_text(
        payload.get("toolName"), limit=256)
    reason, reason_valid = _safe_a2a_text(
        payload.get("reason"), limit=2000)
    policy_reason, policy_reason_valid = _safe_a2a_text(
        row["policy_reason"], limit=2000)
    return {
        "interaction_id": row["id"],
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "kind": row["kind"],
        "status": row["status"],
        "inspectable": details["inspectable"],
        "tool_name": tool_name if tool_name_valid else None,
        "reason": reason if reason_valid else None,
        "tool_view": details["tool_view"],
        "action_intent_id": row["action_intent_id"],
        "operation": row["operation"],
        "risk": row["risk"],
        "policy_route": row["policy_route"],
        "action_intent_status": action_status,
        "policy_reason": policy_reason if policy_reason_valid else None,
        "targets": details["targets"],
        "command": details["command"],
        "args": details["args"],
        "cwd": details["cwd"],
        "workspace": details["workspace"],
        "rollback": details["rollback_plan"],
        "rollback_plan": details["rollback_plan"],
        "allowed_responses": allowed_responses,
        "awaiting": (
            action_status if action_status in {
                "awaiting_hermes", "awaiting_user"
            } else None
        ),
        "awaiting_hermes": action_status == "awaiting_hermes",
        "awaiting_user": action_status == "awaiting_user",
    }


def _interaction_view_rows(conn, *, interaction_id: str | None = None,
                           task_id: str | None = None,
                           pending_only: bool = False):
    clauses = []
    params: list[Any] = []
    if interaction_id is not None:
        clauses.append("i.id = ?")
        params.append(interaction_id)
    if task_id is not None:
        clauses.append("i.task_id = ?")
        params.append(task_id)
    if pending_only:
        clauses.append("i.status IN ('pending', 'failed')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute(
        "SELECT i.id, i.task_id, i.agent_id, i.kind, i.status, i.payload_json,"
        " i.action_intent_id, a.operation, a.risk, a.policy_route,"
        " a.status AS action_intent_status, a.policy_reason,"
        " a.targets_json, a.rollback_plan"
        " FROM agent_session_interactions i"
        " LEFT JOIN action_intents a ON a.id = i.action_intent_id"
        + where + " ORDER BY i.requested_at, i.id;",
        params,
    ).fetchall()


def get_session_interaction_view(conn, interaction_id: str) -> dict | None:
    """Return one bounded interaction view, including resolved records."""
    rows = _interaction_view_rows(conn, interaction_id=interaction_id)
    return _interaction_view_from_row(rows[0]) if rows else None


def pending_interaction_views(conn, task_id: str) -> list[dict]:
    """Return safe, structured pending interaction details for Hermes/UI."""
    return [
        _interaction_view_from_row(row)
        for row in _interaction_view_rows(conn, task_id=task_id,
                                          pending_only=True)
    ]


def attach_action_intent(conn, interaction_id: str,
                         action_intent_id: str, *,
                         commit: bool = True) -> None:
    cur = conn.execute(
        "UPDATE agent_session_interactions SET action_intent_id = ?"
        " WHERE id = ? AND action_intent_id IS NULL;",
        (action_intent_id, interaction_id),
    )
    if commit:
        conn.commit()
    if cur.rowcount != 1:
        raise ValueError(
            f"interaction already linked or missing: {interaction_id}")


def resolve_session_interaction(
    conn,
    interaction_id: str,
    *,
    status: str,
    resolved_by: str,
    response: dict,
    error: str | None = None,
):
    if status not in {"responding", "resolved", "failed"}:
        raise ValueError(f"unsupported interaction status: {status}")
    if resolved_by not in {"user", "hermes"}:
        raise PermissionError("only user or hermes may resolve interactions")
    row = get_session_interaction(conn, interaction_id)
    if row is None:
        raise KeyError(f"interaction not found: {interaction_id}")
    if row["status"] not in {"pending", "responding", "failed"}:
        raise ValueError(f"interaction already {row['status']}")
    resolved_at = now_iso() if status in {"resolved", "failed"} else None
    try:
        conn.execute(
            "UPDATE agent_session_interactions SET status = ?,"
            " resolved_by = ?, response_json = ?, resolved_at = ?,"
            " last_error = ? WHERE id = ?;",
            (status, resolved_by, _json(response), resolved_at, error,
             interaction_id),
        )
        _audit(
            conn, f"agent.interaction.{status}", task_id=row["task_id"],
            source=resolved_by,
            payload={"interaction_id": interaction_id,
                     "kind": row["kind"], "status": status,
                     "error": error},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_session_interaction(conn, interaction_id)


def create_action_intent(conn, *, collaboration_id: str, task_id: str,
                         requested_by_agent_id: str, operation: str,
                         targets: list | dict, purpose: str,
                         expected_effects: list | dict,
                         based_on_revision: int,
                         risk: str = "unknown",
                         rollback_plan: str | None = None,
                         session_binding_id: str | None = None,
                         commit: bool = True):
    _, current = _current_revision(conn, collaboration_id)
    if based_on_revision != current:
        raise ContextConflict(collaboration_id, based_on_revision, current)
    intent_id = _id("AI")
    try:
        conn.execute(
            "INSERT INTO action_intents (id, collaboration_id, task_id,"
            " session_binding_id, requested_by_agent_id, operation,"
            " targets_json, purpose, expected_effects_json, rollback_plan,"
            " risk, based_on_revision, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?);",
            (intent_id, collaboration_id, task_id, session_binding_id,
             requested_by_agent_id, operation, _json(targets), purpose,
             _json(expected_effects), rollback_plan, risk, based_on_revision,
             now_iso()),
        )
        _audit(
            conn, "action.intent.created", task_id=task_id,
            source=requested_by_agent_id,
            payload={
                "intent_id": intent_id,
                "collaboration_id": collaboration_id,
                "operation": operation,
                "risk": risk,
                "based_on_revision": based_on_revision,
            })
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;", (intent_id,)
    ).fetchone()


def route_action_intent(conn, intent_id: str, *, policy=None,
                        commit: bool = True):
    """Apply structured policy and route to auto/Hermes/user authority."""
    from common import config as cfg
    from hermes.action_policy import ActionDecision, ActionPolicy

    row = conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;", (intent_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"action intent not found: {intent_id}")
    if row["status"] != "pending":
        raise ValueError(f"action intent already routed: {row['status']}")
    if policy is None:
        task = conn.execute(
            "SELECT plan_context_json FROM tasks WHERE id = ?;",
            (row["task_id"],),
        ).fetchone()
        execution_workspace = None
        if task is not None:
            try:
                context = json.loads(task["plan_context_json"] or "null")
            except (TypeError, ValueError):
                context = None
            if isinstance(context, dict):
                execution_workspace = context.get("execution_workspace")
        policy = ActionPolicy(
            workspace=Path(execution_workspace).expanduser().resolve()
            if execution_workspace else cfg.workspace())
    agent = conn.execute(
        "SELECT profile_id FROM agents WHERE id = ?;",
        (row["requested_by_agent_id"],),
    ).fetchone()
    profile_id = agent["profile_id"] if agent is not None else None
    profile = None
    if profile_id:
        from orchestrator import agent_profile_store

        profile = agent_profile_store.profile_policy(conn, profile_id)
    decision = policy.evaluate(
        operation=row["operation"], targets=json.loads(row["targets_json"]),
        rollback_plan=row["rollback_plan"], profile=profile)
    from orchestrator import task_plan_store

    plan_step = task_plan_store.get_step_for_task(conn, row["task_id"])
    if plan_step is not None:
        expected = set(json.loads(
            plan_step["expected_operations_json"] or "[]"))
        profile_version = profile.get("version") if profile else None
        current_revision = _current_revision(
            conn, plan_step["plan_collaboration_id"])[1]
        if (plan_step["plan_status"] != "active"
                or plan_step["plan_context_revision"] != current_revision
                or plan_step["agent_id"] != row["requested_by_agent_id"]
                or plan_step["profile_id"] != profile_id
                or plan_step["profile_version"] != profile_version):
            decision = ActionDecision(
                "user", "critical",
                "Task Plan 的 Agent/Profile 快照已失效，必须重新规划")
        elif row["operation"] not in expected:
            decision = ActionDecision(
                "user", "critical",
                f"操作不在 Task Plan 预期范围: {row['operation']}")
    status = {
        "auto": "approved",
        "hermes": "awaiting_hermes",
        "user": "awaiting_user",
    }[decision.route]
    decided_by = "policy" if decision.route == "auto" else None
    decided_at = now_iso() if decided_by else None
    try:
        conn.execute(
            "UPDATE action_intents SET status = ?, risk = ?, policy_route = ?,"
            " policy_reason = ?, decided_by = ?, decided_at = ?"
            " WHERE id = ? AND status = 'pending';",
            (status, decision.risk, decision.route, decision.reason,
             decided_by, decided_at, intent_id),
        )
        if decision.route != "auto":
            conn.execute(
                "UPDATE collaborations SET phase = ?, updated_at = ?"
                " WHERE id = ?;",
                (CollaborationPhase.AWAITING_APPROVAL.value, now_iso(),
                 row["collaboration_id"]),
            )
        _audit(
            conn, "action.intent.routed", task_id=row["task_id"],
            source="policy",
            payload={
                "intent_id": intent_id,
                "collaboration_id": row["collaboration_id"],
                "operation": row["operation"],
                "route": decision.route,
                "risk": decision.risk,
                "status": status,
                "reason": decision.reason,
                "profile_id": profile_id,
                "plan_step_id": plan_step["id"] if plan_step else None,
            })
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;", (intent_id,)
    ).fetchone()


def request_action_intent(conn, *, policy=None, commit: bool = True, **kwargs):
    """Create, audit, and immediately route one agent ActionIntent."""
    intent = create_action_intent(conn, commit=commit, **kwargs)
    return route_action_intent(
        conn, intent["id"], policy=policy, commit=commit)


def decide_action_intent(conn, intent_id: str, *, approved: bool,
                         decided_by: str, note: str = ""):
    if decided_by not in {"user", "hermes"}:
        raise PermissionError("only user or hermes may decide action intents")
    row = conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;", (intent_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"action intent not found: {intent_id}")
    if row["status"] not in {"awaiting_hermes", "awaiting_user"}:
        raise ValueError(f"action intent already decided: {row['status']}")
    if row["status"] == "awaiting_user" and decided_by != "user":
        raise PermissionError("this action intent requires user approval")
    _, current = _current_revision(conn, row["collaboration_id"])
    if approved and row["based_on_revision"] != current:
        raise ContextConflict(
            row["collaboration_id"], row["based_on_revision"], current)
    status = "approved" if approved else "rejected"
    previous_status = row["status"]
    try:
        conn.execute(
            "UPDATE action_intents SET status = ?, decided_by = ?,"
            " decision_note = ?, decided_at = ? WHERE id = ? AND status = ?;",
            (status, decided_by, note, now_iso(), intent_id, previous_status),
        )
        _audit(
            conn, f"action.intent.{status}", task_id=row["task_id"],
            source=decided_by,
            payload={
                "intent_id": intent_id,
                "collaboration_id": row["collaboration_id"],
                "operation": row["operation"],
                "decided_by": decided_by,
                "note": note,
                "based_on_revision": row["based_on_revision"],
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;", (intent_id,)
    ).fetchone()
