"""Durable, peer-scoped wakeups for asynchronous Hermes supervision."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from state.db import CST, now_iso

_WATCH_ID_RE = re.compile(r"^WATCH-[A-Za-z0-9_-]{8,80}$")
_NOTIFICATION_ID_RE = re.compile(r"^SN-[A-Za-z0-9_-]{8,80}$")
_MAX_WATCH_IDS = 100
_MAX_PULL = 20


def _as_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).astimezone(CST).isoformat(timespec="seconds")


def _validate_watch_ids(values: Iterable[str]) -> list[str]:
    watch_ids = list(dict.fromkeys(values))
    if not watch_ids or len(watch_ids) > _MAX_WATCH_IDS:
        raise ValueError(f"watch_ids must contain 1-{_MAX_WATCH_IDS} values")
    if any(not isinstance(value, str) or not _WATCH_ID_RE.fullmatch(value)
           for value in watch_ids):
        raise ValueError("invalid watch_id")
    return watch_ids


def _task_belongs_to_context(conn, *, task_id: str, peer: str,
                             context_id: str):
    from orchestrator import collaboration_store

    task = conn.execute(
        "SELECT id, collaboration_id, status, updated_at FROM tasks"
        " WHERE id = ?;", (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"task not found: {task_id}")
    expected = collaboration_store.a2a_context_ids(
        peer=peer, context_id=context_id)["collaboration_id"]
    if not task["collaboration_id"] or task["collaboration_id"] != expected:
        raise PermissionError("task does not belong to this peer/context")
    return task


def register_watch(conn, *, peer: str, context_id: str,
                   task_id: str) -> dict:
    """Create or reactivate the sole watch for one peer-owned task."""
    peer = str(peer or "").strip()
    context_id = str(context_id or "").strip()
    task_id = str(task_id or "").strip()
    if not peer or not context_id or not task_id:
        raise ValueError("peer, context_id and task_id are required")
    _task_belongs_to_context(
        conn, task_id=task_id, peer=peer, context_id=context_id)
    existing = conn.execute(
        "SELECT * FROM supervision_watches WHERE peer = ? AND task_id = ?;",
        (peer, task_id)).fetchone()
    timestamp = now_iso()
    if existing is None:
        watch_id = f"WATCH-{uuid.uuid4().hex[:20]}"
        conn.execute(
            "INSERT INTO supervision_watches (id, task_id, peer, context_id,"
            " status, created_at, updated_at)"
            " VALUES (?,?,?,?,'active',?,?);",
            (watch_id, task_id, peer, context_id, timestamp, timestamp))
    else:
        watch_id = existing["id"]
        conn.execute(
            "UPDATE supervision_watches SET context_id = ?, status = 'active',"
            " updated_at = ? WHERE id = ?;",
            (context_id, timestamp, watch_id))
    sync_watch(conn, watch_id, commit=False)
    conn.commit()
    saved = _as_dict(conn.execute(
        "SELECT * FROM supervision_watches WHERE id = ?;",
        (watch_id,)).fetchone())
    return {**saved, "watch_id": saved["id"]}


def _latest_delegation_approval(conn, task_id: str):
    return conn.execute(
        "SELECT id, event_type FROM events WHERE task_id = ?"
        " AND event_type IN ('task.approval_requested', 'task.approved',"
        " 'task.auto_approved', 'task.rejected')"
        " ORDER BY seq DESC LIMIT 1;", (task_id,)).fetchone()


def _pending_interaction_ids(conn, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM agent_session_interactions WHERE task_id = ?"
        " AND status IN ('pending', 'failed') ORDER BY id;", (task_id,)
    ).fetchall()
    return [row["id"] for row in rows]


def _enqueue(conn, *, watch_id: str, task_id: str, dedupe_key: str,
             event_type: str, internal_status: str) -> None:
    timestamp = now_iso()
    notification_id = f"SN-{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO supervision_outbox (id, watch_id, task_id, dedupe_key,"
        " event_type, internal_status, status, attempts, available_at,"
        " created_at) VALUES (?,?,?,?,?,?,'pending',0,?,?)"
        " ON CONFLICT(watch_id, dedupe_key) DO NOTHING;",
        (notification_id, watch_id, task_id, dedupe_key, event_type,
         internal_status, timestamp, timestamp))


def sync_watch(conn, watch_id: str, *, commit: bool = True) -> None:
    """Reconcile current authoritative state into a deduplicated wakeup."""
    watch = conn.execute(
        "SELECT * FROM supervision_watches WHERE id = ? AND status = 'active';",
        (watch_id,)).fetchone()
    if watch is None:
        return
    task = conn.execute(
        "SELECT id, status, updated_at FROM tasks WHERE id = ?;",
        (watch["task_id"],)).fetchone()
    if task is None:
        return
    task_id = task["id"]
    status = task["status"]
    if status in {"created", "queued"}:
        approval = _latest_delegation_approval(conn, task_id)
        if approval is not None and approval["event_type"] == \
                "task.approval_requested":
            _enqueue(
                conn, watch_id=watch_id, task_id=task_id,
                dedupe_key=f"delegation:{approval['id']}",
                event_type="task.approval_requested",
                internal_status=status)
    elif status == "blocked":
        interaction_ids = _pending_interaction_ids(conn, task_id)
        digest = hashlib.sha256(
            "\0".join(interaction_ids).encode("utf-8")).hexdigest()[:20]
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"blocked:{task['updated_at']}:{digest}",
            event_type=("agent.interaction.requested" if interaction_ids
                        else "task.blocked"),
            internal_status=status)
    elif status in {"awaiting_acceptance", "completed", "reviewed"}:
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"acceptance:{task['updated_at']}",
            event_type="task.awaiting_acceptance",
            internal_status=status)
    elif status in {"failed", "cancelled"}:
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"terminal:{status}:{task['updated_at']}",
            event_type=f"task.{status}", internal_status=status)
    elif status == "accepted":
        conn.execute(
            "UPDATE supervision_watches SET status = 'completed',"
            " updated_at = ? WHERE id = ?;", (now_iso(), watch_id))
    if commit:
        conn.commit()


def sync_task(conn, task_id: str, *, commit: bool = True) -> None:
    rows = conn.execute(
        "SELECT id FROM supervision_watches WHERE task_id = ?"
        " AND status = 'active';", (task_id,)).fetchall()
    for row in rows:
        sync_watch(conn, row["id"], commit=False)
    if commit:
        conn.commit()


def pull_notifications(conn, *, peer: str, watch_ids: Iterable[str],
                       limit: int = _MAX_PULL,
                       now: datetime | None = None) -> list[dict]:
    watch_ids = _validate_watch_ids(watch_ids)
    limit = min(max(int(limit), 1), _MAX_PULL)
    for watch_id in watch_ids:
        watch = conn.execute(
            "SELECT peer FROM supervision_watches WHERE id = ?;",
            (watch_id,)).fetchone()
        if watch is None or watch["peer"] != peer:
            raise PermissionError("watch does not belong to authenticated peer")
        sync_watch(conn, watch_id, commit=False)

    current = _utc(now)
    current_iso = _iso(current)
    placeholders = ",".join("?" for _ in watch_ids)
    rows = conn.execute(
        "SELECT o.*, w.context_id FROM supervision_outbox o"
        " JOIN supervision_watches w ON w.id = o.watch_id"
        f" WHERE o.watch_id IN ({placeholders}) AND w.peer = ?"
        " AND w.status = 'active' AND ("
        " (o.status = 'pending' AND o.available_at <= ?) OR"
        " (o.status = 'inflight' AND o.lease_until <= ?))"
        " ORDER BY o.created_at, o.id LIMIT ?;",
        (*watch_ids, peer, current_iso, current_iso, limit)).fetchall()
    public = []
    for row in rows:
        attempts = int(row["attempts"]) + 1
        lease_seconds = min(300, 60 * (2 ** min(attempts - 1, 2)))
        lease_until = _iso(current + timedelta(seconds=lease_seconds))
        conn.execute(
            "UPDATE supervision_outbox SET status = 'inflight', attempts = ?,"
            " lease_until = ? WHERE id = ?;",
            (attempts, lease_until, row["id"]))
        public.append({
            "notification_id": row["id"],
            "watch_id": row["watch_id"],
            "task_id": row["task_id"],
            "context_id": row["context_id"],
            "event_type": row["event_type"],
            "internal_status": row["internal_status"],
            "created_at": row["created_at"],
        })
    conn.commit()
    return public


def acknowledge_notification(conn, *, peer: str,
                             notification_id: str) -> dict:
    if not isinstance(notification_id, str) or not \
            _NOTIFICATION_ID_RE.fullmatch(notification_id):
        raise ValueError("invalid notification_id")
    row = conn.execute(
        "SELECT o.*, w.peer FROM supervision_outbox o"
        " JOIN supervision_watches w ON w.id = o.watch_id"
        " WHERE o.id = ?;", (notification_id,)).fetchone()
    if row is None:
        raise KeyError(f"notification not found: {notification_id}")
    if row["peer"] != peer:
        raise PermissionError("notification does not belong to authenticated peer")
    if row["status"] != "acknowledged":
        conn.execute(
            "UPDATE supervision_outbox SET status = 'acknowledged',"
            " acknowledged_at = ?, acknowledged_by = ?, lease_until = NULL"
            " WHERE id = ?;", (now_iso(), peer, notification_id))
        conn.commit()
    return {"notification_id": notification_id, "status": "acknowledged"}


def stop_watch(conn, *, peer: str, task_id: str) -> dict:
    row = conn.execute(
        "SELECT id FROM supervision_watches WHERE peer = ? AND task_id = ?;",
        (peer, task_id)).fetchone()
    if row is None:
        raise KeyError(f"watch not found for task: {task_id}")
    conn.execute(
        "UPDATE supervision_watches SET status = 'stopped', updated_at = ?"
        " WHERE id = ?;", (now_iso(), row["id"]))
    conn.commit()
    return {"watch_id": row["id"], "task_id": task_id, "status": "stopped"}
