"""Guarded historical repair for A2A tasks missing Collaboration linkage.

The caller supplies an operator-reviewed manifest.  Planning is read-only;
mutation requires an exact confirmation phrase and returns a rollback receipt.
No task lifecycle, result, event, run, or artifact record is rewritten.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from common.models import TaskStatus
from orchestrator import collaboration_store, state_store

APPLY_CONFIRMATION = "BACKFILL_A2A_COLLABORATIONS"
ROLLBACK_CONFIRMATION = "ROLLBACK_A2A_COLLABORATIONS"
HISTORICAL_TERMINAL_STATES = {
    TaskStatus.FAILED.value,
    TaskStatus.COMPLETED.value,
    TaskStatus.REVIEWED.value,
    TaskStatus.ACCEPTED.value,
    TaskStatus.CANCELLED.value,
}


def objective_sha256(objective: str) -> str:
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()


def _dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _session_metadata(conn, task_id: str) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'agent.session.event' ORDER BY seq;",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        session_id = payload.get("sessionId")
        native_id = payload.get("nativeSessionId")
        instance_id = payload.get("adapterInstanceId")
        if session_id or native_id:
            return {
                "adapter_session_id": session_id,
                "native_session_id": native_id,
                "adapter_instance_id": instance_id,
            }
    return {
        "adapter_session_id": None,
        "native_session_id": None,
        "adapter_instance_id": None,
    }


def plan_manifest(conn, manifest: dict) -> dict:
    """Validate a version-1 manifest without changing database state."""
    if manifest.get("version") != 1:
        raise ValueError("backfill manifest version must be 1")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("backfill manifest entries must be a non-empty list")
    seen: set[str] = set()
    planned: list[dict[str, Any]] = []
    for raw in entries:
        errors: list[str] = []
        if not isinstance(raw, dict):
            raise ValueError("each backfill entry must be an object")
        task_id = str(raw.get("task_id") or "").strip()
        if not task_id or task_id in seen:
            errors.append("task_id is missing or duplicated")
        seen.add(task_id)
        peer = str(raw.get("peer") or "").strip()
        context_id = str(raw.get("context_id") or "").strip()
        try:
            mapping = collaboration_store.a2a_context_ids(
                peer=peer, context_id=context_id)
        except ValueError as exc:
            mapping = {"conversation_id": None, "collaboration_id": None,
                       "peer": peer, "context_id": context_id}
            errors.append(str(exc))
        task = state_store.get_task(conn, task_id) if task_id else None
        if task is None:
            errors.append("task not found")
        else:
            expected_agent = str(raw.get("agent") or "").strip()
            if not expected_agent or task["assigned_to"] != expected_agent:
                errors.append("assigned agent mismatch")
            if task["status"] not in HISTORICAL_TERMINAL_STATES:
                errors.append("task is not in a historical terminal state")
            expected_digest = str(raw.get("objective_sha256") or "")
            if len(expected_digest) != 64 or objective_sha256(
                    task["objective"]) != expected_digest:
                errors.append("objective sha256 mismatch")
            expected_created = str(raw.get("created_at") or "")
            if not expected_created or task["created_at"] != expected_created:
                errors.append("created_at mismatch")
            current = task["collaboration_id"]
            if current not in (None, mapping["collaboration_id"]):
                errors.append(f"task already belongs to {current}")
        planned.append({
            **mapping,
            "task_id": task_id,
            "eligible": not errors,
            "errors": errors,
            "task": _dict(task) if task is not None else None,
            "session": _session_metadata(conn, task_id) if task else None,
            "evidence": raw.get("evidence"),
        })
    return {
        "version": 1,
        "mode": "dry-run",
        "eligible": all(item["eligible"] for item in planned),
        "entries": planned,
    }


def apply_manifest(conn, manifest: dict, *, confirmation: str) -> dict:
    """Apply a fully valid manifest and return a precise rollback receipt."""
    if confirmation != APPLY_CONFIRMATION:
        raise PermissionError("explicit A2A backfill confirmation required")
    plan = plan_manifest(conn, manifest)
    if not plan["eligible"]:
        raise ValueError("backfill manifest failed dry-run validation")
    prior_receipts: list[dict] = []
    prior_count = 0
    for item in plan["entries"]:
        rolled_back = conn.execute(
            "SELECT 1 FROM events WHERE id = ?;",
            (f"a2a-backfill-rollback-{item['task_id']}",),
        ).fetchone()
        if rolled_back is not None:
            raise RuntimeError(
                f"backfill was previously rolled back: {item['task_id']}")
        row = conn.execute(
            "SELECT event_type, payload_json FROM events WHERE id = ?;",
            (f"a2a-backfill-{item['task_id']}",),
        ).fetchone()
        if row is None:
            continue
        prior_count += 1
        try:
            payload = json.loads(row["payload_json"])
            receipt_entry = payload["receipt"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"existing backfill audit is not recoverable: {item['task_id']}"
            ) from exc
        if (row["event_type"] != "task.collaboration.backfilled"
                or receipt_entry.get("task_id") != item["task_id"]
                or receipt_entry.get("collaboration_id")
                != item["collaboration_id"]):
            raise RuntimeError(
                f"existing backfill audit conflicts: {item['task_id']}")
        prior_receipts.append(receipt_entry)
    if prior_count:
        if prior_count != len(plan["entries"]):
            raise RuntimeError("partial prior backfill detected; manual review required")
        return {"version": 1,
                "kind": "a2a-collaboration-backfill-receipt",
                "entries": prior_receipts}

    receipt_entries: list[dict] = []
    try:
        for item in plan["entries"]:
            task = item["task"]
            conversation_id = item["conversation_id"]
            collaboration_id = item["collaboration_id"]
            conversation_existed = collaboration_store.get_conversation(
                conn, conversation_id) is not None
            collaboration_existed = collaboration_store.get_collaboration(
                conn, collaboration_id) is not None
            collaboration_store.ensure_a2a_collaboration(
                conn, peer=item["peer"], context_id=item["context_id"],
                objective=task["objective"], project=task["project"],
                commit=False)
            cur = conn.execute(
                "UPDATE tasks SET collaboration_id = ? WHERE id = ?"
                " AND collaboration_id IS NULL;",
                (collaboration_id, item["task_id"]),
            )
            if cur.rowcount not in (0, 1):
                raise RuntimeError("unexpected task linkage row count")

            request = collaboration_store.append_message(
                conn, conversation_id=conversation_id,
                collaboration_id=collaboration_id, task_id=item["task_id"],
                agent_id=task["assigned_to"], sender_type="hermes",
                sender_id=item["peer"], recipient_type="agent",
                recipient_id=task["assigned_to"],
                message_type="a2a.task.request.historical",
                content={"text": task["objective"], "historical": True,
                         "contextId": item["context_id"],
                         "evidence": item.get("evidence")},
                based_on_revision=1,
                idempotency_key=f"a2a-backfill-request:{item['task_id']}",
                commit=False,
            )
            message_ids = [request["id"]]
            if task["result_summary"]:
                result = collaboration_store.append_message(
                    conn, conversation_id=conversation_id,
                    collaboration_id=collaboration_id, task_id=item["task_id"],
                    agent_id=task["assigned_to"], sender_type="agent",
                    sender_id=task["assigned_to"],
                    recipient_type="hermes", recipient_id=item["peer"],
                    message_type="a2a.task.result.historical",
                    content={"text": task["result_summary"], "historical": True,
                             "status": task["status"]},
                    based_on_revision=1,
                    idempotency_key=f"a2a-backfill-result:{item['task_id']}",
                    commit=False,
                )
                message_ids.append(result["id"])

            binding = conn.execute(
                "SELECT * FROM agent_session_bindings WHERE task_id = ?"
                " AND agent_id = ? AND is_current = 1;",
                (item["task_id"], task["assigned_to"]),
            ).fetchone()
            binding_created = False
            session = item["session"] or {}
            if binding is None and (
                    session.get("adapter_session_id")
                    or session.get("native_session_id")):
                binding = collaboration_store.bind_agent_session(
                    conn, collaboration_id=collaboration_id,
                    task_id=item["task_id"], agent_id=task["assigned_to"],
                    adapter_session_id=session.get("adapter_session_id"),
                    native_session_id=session.get("native_session_id"),
                    adapter_instance_id=session.get("adapter_instance_id"),
                    resume_capability="snapshot", capabilities={},
                    recovery_state="historical_import",
                    context_snapshot={"objective": task["objective"],
                                      "historical": True},
                    commit=False,
                )
                collaboration_store.update_agent_session_status(
                    conn, binding["id"], status=task["status"],
                    recovery_state="historical_import", commit=False)
                binding_created = True

            receipt_entry = {
                "task_id": item["task_id"],
                "conversation_id": conversation_id,
                "collaboration_id": collaboration_id,
                "message_ids": message_ids,
                "binding_id": binding["id"] if binding_created else None,
                "created_conversation": not conversation_existed,
                "created_collaboration": not collaboration_existed,
            }
            audit_id = f"a2a-backfill-{item['task_id']}"
            audit_exists = conn.execute(
                "SELECT id FROM events WHERE id = ?;", (audit_id,)
            ).fetchone()
            if audit_exists is None:
                state_store.record_event(conn, {
                    "event_id": audit_id,
                    "event_type": "task.collaboration.backfilled",
                    "task_id": item["task_id"],
                    "source": "a2a-backfill",
                    "payload": {
                        "peer": item["peer"],
                        "context_id": item["context_id"],
                        "conversation_id": conversation_id,
                        "collaboration_id": collaboration_id,
                        "evidence": item.get("evidence"),
                        "receipt": receipt_entry,
                    },
                }, commit=False)
            receipt_entries.append(receipt_entry)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"version": 1, "kind": "a2a-collaboration-backfill-receipt",
            "entries": receipt_entries}


def rollback_receipt(conn, receipt: dict, *, confirmation: str) -> dict:
    """Undo only rows named by a receipt; refuse mixed/newer collaboration data."""
    if confirmation != ROLLBACK_CONFIRMATION:
        raise PermissionError("explicit A2A backfill rollback confirmation required")
    if (receipt.get("version") != 1
            or receipt.get("kind") != "a2a-collaboration-backfill-receipt"):
        raise ValueError("invalid A2A backfill receipt")
    entries = receipt.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("A2A backfill receipt entries must be non-empty")
    expected_by_collaboration: dict[str, dict[str, set[str]]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("task_id"):
            raise ValueError("invalid A2A backfill receipt entry")
        audit = conn.execute(
            "SELECT event_type, payload_json FROM events WHERE id = ?;",
            (f"a2a-backfill-{entry['task_id']}",),
        ).fetchone()
        try:
            audited_receipt = json.loads(audit["payload_json"])["receipt"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"missing audited receipt for {entry['task_id']}") from exc
        if (audit["event_type"] != "task.collaboration.backfilled"
                or audited_receipt != entry):
            raise ValueError(
                f"receipt does not match audit for {entry['task_id']}")
        group = expected_by_collaboration.setdefault(
            entry["collaboration_id"],
            {"tasks": set(), "messages": set(), "bindings": set()},
        )
        group["tasks"].add(entry["task_id"])
        group["messages"].update(entry.get("message_ids") or [])
        if entry.get("binding_id"):
            group["bindings"].add(entry["binding_id"])
    for collaboration_id, expected in expected_by_collaboration.items():
        actual_tasks = {row["id"] for row in conn.execute(
            "SELECT id FROM tasks WHERE collaboration_id = ?;",
            (collaboration_id,),).fetchall()}
        actual_messages = {row["id"] for row in conn.execute(
            "SELECT id FROM conversation_messages WHERE collaboration_id = ?;",
            (collaboration_id,),).fetchall()}
        actual_bindings = {row["id"] for row in conn.execute(
            "SELECT id FROM agent_session_bindings WHERE collaboration_id = ?;",
            (collaboration_id,),).fetchall()}
        dependent_tables = (
            "action_intents", "agent_session_interactions", "task_plans")
        has_dependent_rows = any(conn.execute(
            f"SELECT 1 FROM {table} WHERE collaboration_id = ? LIMIT 1;",
            (collaboration_id,),
        ).fetchone() is not None for table in dependent_tables)
        if (actual_tasks != expected["tasks"]
                or actual_messages != expected["messages"]
                or actual_bindings != expected["bindings"]
                or has_dependent_rows):
            raise RuntimeError(
                f"rollback refused: collaboration {collaboration_id} has newer data")

    try:
        for entry in reversed(entries):
            if entry.get("binding_id"):
                conn.execute("DELETE FROM agent_session_bindings WHERE id = ?;",
                             (entry["binding_id"],))
            for message_id in entry.get("message_ids") or []:
                conn.execute("DELETE FROM conversation_messages WHERE id = ?;",
                             (message_id,))
            conn.execute(
                "UPDATE tasks SET collaboration_id = NULL WHERE id = ?"
                " AND collaboration_id = ?;",
                (entry["task_id"], entry["collaboration_id"]),
            )
            state_store.record_event(conn, {
                "event_id": f"a2a-backfill-rollback-{entry['task_id']}",
                "event_type": "task.collaboration.backfill_rolled_back",
                "task_id": entry["task_id"], "source": "a2a-backfill",
                "payload": {"collaboration_id": entry["collaboration_id"]},
            }, commit=False)
        for entry in reversed(entries):
            if entry.get("created_collaboration"):
                conn.execute(
                    "DELETE FROM collaborations WHERE id = ?"
                    " AND NOT EXISTS (SELECT 1 FROM tasks WHERE collaboration_id = ?)"
                    " AND NOT EXISTS (SELECT 1 FROM conversation_messages"
                    " WHERE collaboration_id = ?)"
                    " AND NOT EXISTS (SELECT 1 FROM agent_session_bindings"
                    " WHERE collaboration_id = ?);",
                    (entry["collaboration_id"],) * 4,
                )
            if entry.get("created_conversation"):
                conn.execute(
                    "DELETE FROM conversations WHERE id = ?"
                    " AND NOT EXISTS (SELECT 1 FROM collaborations"
                    " WHERE conversation_id = ?)"
                    " AND NOT EXISTS (SELECT 1 FROM conversation_messages"
                    " WHERE conversation_id = ?);",
                    (entry["conversation_id"],) * 3,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"rolled_back": [entry["task_id"] for entry in entries]}
