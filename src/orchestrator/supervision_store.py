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
    task = _task_belongs_to_context(
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
    conn.execute(
        "INSERT INTO supervision_conversation_routes"
        " (collaboration_id, peer, context_id, watch_id, created_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(collaboration_id) DO NOTHING;",
        (task["collaboration_id"], peer, context_id, watch_id, timestamp),
    )
    route = conn.execute(
        "SELECT peer, context_id FROM supervision_conversation_routes"
        " WHERE collaboration_id = ?;",
        (task["collaboration_id"],),
    ).fetchone()
    if (route is None or route["peer"] != peer
            or route["context_id"] != context_id):
        conn.rollback()
        raise PermissionError(
            "collaboration is already bound to another Hermes route")
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
             event_type: str, internal_status: str,
             message_id: str | None = None):
    timestamp = now_iso()
    notification_id = f"SN-{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO supervision_outbox (id, watch_id, task_id, dedupe_key,"
        " event_type, internal_status, message_id, status, attempts,"
        " available_at, created_at) VALUES (?,?,?,?,?,?,?,'pending',0,?,?)"
        " ON CONFLICT(watch_id, dedupe_key) DO NOTHING;",
        (notification_id, watch_id, task_id, dedupe_key, event_type,
         internal_status, message_id, timestamp, timestamp))
    return conn.execute(
        "SELECT * FROM supervision_outbox WHERE watch_id = ?"
        " AND dedupe_key = ?;",
        (watch_id, dedupe_key),
    ).fetchone()


def enqueue_user_message(conn, *, collaboration_id: str,
                         message_id: str) -> dict:
    """Queue one identifiers-only wake for the originating Hermes context.

    A completed task remains immutable.  Its supervision watch is reused only
    as a durable route back to the originating peer/session; the user message
    itself remains collaboration-scoped and can lead Hermes to answer directly
    or create a separate follow-up task.
    """
    from orchestrator import collaboration_store

    collaboration = collaboration_store.get_collaboration(
        conn, collaboration_id)
    if collaboration is None:
        raise KeyError(f"collaboration not found: {collaboration_id}")
    conversation = collaboration_store.get_conversation(
        conn, collaboration["conversation_id"])
    created_by = str(conversation["created_by"] if conversation else "")
    if not created_by.startswith("a2a:"):
        raise ValueError(
            "collaboration has no external Hermes delivery route")
    peer = created_by.removeprefix("a2a:")
    message = conn.execute(
        "SELECT * FROM conversation_messages WHERE id = ?;",
        (message_id,),
    ).fetchone()
    if (message is None or message["collaboration_id"] != collaboration_id
            or message["sender_type"] != "user"
            or message["recipient_type"] != "hermes"
            or message["recipient_id"] != "hermes"
            or message["message_type"] != "user.comment"):
        raise PermissionError("message is not a user message for Hermes")
    if message["delivery_status"] == "delivered":
        return {
            "message_id": message_id,
            "delivery_status": "delivered",
        }
    route = conn.execute(
        "SELECT r.watch_id FROM supervision_conversation_routes r"
        " WHERE r.collaboration_id = ? AND r.peer = ?;",
        (collaboration_id, peer),
    ).fetchone()
    if route is None:
        # Lazy, deterministic backfill for conversations created before
        # migration 014.  Once selected, this route never drifts to a later
        # task/session in the same A2A context.
        candidate = conn.execute(
            "SELECT w.id, w.context_id, w.created_at"
            " FROM supervision_watches w JOIN tasks t ON t.id = w.task_id"
            " WHERE t.collaboration_id = ? AND w.peer = ?"
            " AND w.status != 'stopped'"
            " ORDER BY w.created_at, w.id LIMIT 1;",
            (collaboration_id, peer),
        ).fetchone()
        if candidate is not None:
            conn.execute(
                "INSERT INTO supervision_conversation_routes"
                " (collaboration_id, peer, context_id, watch_id, created_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(collaboration_id) DO NOTHING;",
                (collaboration_id, peer, candidate["context_id"],
                 candidate["id"], candidate["created_at"]),
            )
            route = conn.execute(
                "SELECT watch_id FROM supervision_conversation_routes"
                " WHERE collaboration_id = ? AND peer = ?;",
                (collaboration_id, peer),
            ).fetchone()
    watch = (conn.execute(
        "SELECT * FROM supervision_watches WHERE id = ?"
        " AND peer = ? AND status IN ('active', 'completed');",
        (route["watch_id"], peer),
    ).fetchone() if route is not None else None)
    if watch is None:
        raise ValueError(
            "originating Hermes session has no registered delivery route")
    expected = collaboration_store.a2a_context_ids(
        peer=peer, context_id=watch["context_id"])
    if expected["collaboration_id"] != collaboration_id:
        raise PermissionError("Hermes route does not own this collaboration")
    conn.execute(
        "UPDATE supervision_watches SET status = 'active', updated_at = ?"
        " WHERE id = ?;",
        (now_iso(), watch["id"]),
    )
    outbox = _enqueue(
        conn,
        watch_id=watch["id"],
        task_id=watch["task_id"],
        dedupe_key=f"conversation-message:{message_id}",
        event_type="conversation.user_message",
        internal_status="message_pending",
        message_id=message_id,
    )
    if (outbox is None or outbox["message_id"] != message_id
            or outbox["event_type"] != "conversation.user_message"):
        raise PermissionError("message delivery dedupe conflict")
    if outbox["status"] not in {"pending", "inflight"}:
        raise ValueError(
            f"message delivery is not retryable: {outbox['status']}")
    delivery_status = (
        "processing" if outbox["status"] == "inflight" else "queued")
    conn.execute(
        "UPDATE conversation_messages SET delivery_status = ?"
        " WHERE id = ?;",
        (delivery_status, message_id),
    )
    conn.commit()
    return {
        "message_id": message_id,
        "watch_id": watch["id"],
        "task_id": watch["task_id"],
        "peer": peer,
        "context_id": watch["context_id"],
        "delivery_status": delivery_status,
    }


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
    # A native interaction is itself a lifecycle edge.  Usually the worker
    # emits ``task.input_required`` first (which moves the task to blocked),
    # but a durable interaction record can also arrive independently.  Do not
    # make Hermes wait for a later status poll in that case.
    interaction_ids = _pending_interaction_ids(conn, task_id)
    interaction_wakeup = bool(
        interaction_ids and status in {"assigned", "working", "blocked"})
    if interaction_wakeup:
        digest = hashlib.sha256(
            "\0".join(interaction_ids).encode("utf-8")).hexdigest()[:20]
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"interaction:{task['updated_at']}:{digest}",
            event_type="agent.interaction.requested",
            internal_status=status)
    if status in {"created", "queued"}:
        approval = _latest_delegation_approval(conn, task_id)
        if approval is not None and approval["event_type"] == \
                "task.approval_requested":
            _enqueue(
                conn, watch_id=watch_id, task_id=task_id,
                dedupe_key=f"delegation:{approval['id']}",
                event_type="task.approval_requested",
                internal_status=status)
    elif status == "blocked" and not interaction_wakeup:
        # A structured native interaction is the more specific reason for a
        # blocked task.  Emitting both edges creates two Hermes wakeups for the
        # same gate, so the generic blocked event is only the fallback when no
        # durable interaction exists.
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"blocked:{task['updated_at']}",
            event_type="task.blocked",
            internal_status=status)
    elif status in {"awaiting_acceptance", "completed", "reviewed"}:
        notification_status = (
            "awaiting_acceptance" if status == "completed" else status)
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"acceptance:{task['updated_at']}",
            event_type="task.awaiting_acceptance",
            internal_status=notification_status)
    elif status in {"failed", "cancelled"}:
        _enqueue(
            conn, watch_id=watch_id, task_id=task_id,
            dedupe_key=f"terminal:{status}:{task['updated_at']}",
            event_type=f"task.{status}", internal_status=status)
    elif status == "accepted":
        pending_message = conn.execute(
            "SELECT 1 FROM supervision_outbox WHERE watch_id = ?"
            " AND event_type = 'conversation.user_message'"
            " AND status IN ('pending', 'inflight') LIMIT 1;",
            (watch_id,),
        ).fetchone()
        if pending_message is None:
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
    from orchestrator import collaboration_store

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
        claimed = conn.execute(
            "UPDATE supervision_outbox SET status = 'inflight', attempts = ?,"
            " lease_until = ? WHERE id = ? AND ("
            " (status = 'pending' AND available_at <= ?) OR"
            " (status = 'inflight' AND lease_until <= ?));",
            (attempts, lease_until, row["id"], current_iso, current_iso))
        if claimed.rowcount != 1:
            continue
        if row["event_type"] == "conversation.user_message" and \
                row["message_id"]:
            # A successful claim is the first authoritative signal that Hermes
            # has accepted ownership of this delivery. Keep that distinct from
            # merely queued so the WebUI does not look stalled during a long
            # recovery/compression turn. Redelivery remains processing until a
            # persisted Hermes response moves the message to delivered.
            collaboration_store.update_message_delivery_status(
                conn,
                message_id=row["message_id"],
                delivery_status="processing",
                expected_statuses={"queued", "processing"},
                commit=False,
            )
        item = {
            "notification_id": row["id"],
            "watch_id": row["watch_id"],
            "task_id": row["task_id"],
            "context_id": row["context_id"],
            "event_type": row["event_type"],
            "internal_status": row["internal_status"],
            "created_at": row["created_at"],
        }
        if row["message_id"]:
            item["message_id"] = row["message_id"]
            item["delivery_status"] = "processing"
        public.append(item)
    conn.commit()
    return public


def acknowledge_notification(conn, *, peer: str, context_id: str,
                             notification_id: str) -> dict:
    if (not isinstance(context_id, str) or not context_id.strip()
            or len(context_id.strip()) > 512):
        raise ValueError("invalid context_id")
    context_id = context_id.strip()
    if not isinstance(notification_id, str) or not \
            _NOTIFICATION_ID_RE.fullmatch(notification_id):
        raise ValueError("invalid notification_id")
    row = conn.execute(
        "SELECT o.*, w.peer, w.context_id FROM supervision_outbox o"
        " JOIN supervision_watches w ON w.id = o.watch_id"
        " WHERE o.id = ?;", (notification_id,)).fetchone()
    if row is None:
        raise KeyError(f"notification not found: {notification_id}")
    if row["peer"] != peer or row["context_id"] != context_id:
        raise PermissionError(
            "notification does not belong to authenticated peer/context")
    if row["event_type"] == "conversation.user_message":
        response = conn.execute(
            "SELECT response.id FROM conversation_messages response"
            " JOIN conversation_messages original"
            " ON original.id = response.parent_message_id"
            " WHERE original.id = ?"
            " AND response.conversation_id = original.conversation_id"
            " AND response.collaboration_id = original.collaboration_id"
            " AND response.sender_type = 'hermes'"
            " AND response.sender_id = ?"
            " AND response.recipient_type = 'user'"
            " AND response.recipient_id = 'user'"
            " AND response.message_type = 'llm.assistant' LIMIT 1;",
            (row["message_id"], peer),
        ).fetchone()
        if response is None:
            raise ValueError(
                "Hermes response is required before message notification ACK")
    if row["status"] != "acknowledged":
        try:
            conn.execute(
                "UPDATE supervision_outbox SET status = 'acknowledged',"
                " acknowledged_at = ?, acknowledged_by = ?, lease_until = NULL"
                " WHERE id = ?;", (now_iso(), peer, notification_id))
            sync_watch(conn, row["watch_id"], commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"notification_id": notification_id, "status": "acknowledged"}


def stop_watch(conn, *, peer: str, task_id: str) -> dict:
    row = conn.execute(
        "SELECT id FROM supervision_watches WHERE peer = ? AND task_id = ?;",
        (peer, task_id)).fetchone()
    if row is None:
        raise KeyError(f"watch not found for task: {task_id}")
    pending_messages = conn.execute(
        "SELECT DISTINCT message_id FROM supervision_outbox"
        " WHERE watch_id = ? AND event_type = 'conversation.user_message'"
        " AND message_id IS NOT NULL AND status IN ('pending','inflight');",
        (row["id"],),
    ).fetchall()
    conn.execute(
        "UPDATE supervision_outbox SET status = 'failed', lease_until = NULL"
        " WHERE watch_id = ? AND event_type = 'conversation.user_message'"
        " AND status IN ('pending','inflight');",
        (row["id"],),
    )
    from orchestrator import collaboration_store

    for message in pending_messages:
        collaboration_store.update_message_delivery_status(
            conn,
            message_id=message["message_id"],
            delivery_status="failed",
            expected_statuses={"queued", "processing"},
            reason="delivery_route_stopped",
            commit=False,
        )
    conn.execute(
        "UPDATE supervision_watches SET status = 'stopped', updated_at = ?"
        " WHERE id = ?;", (now_iso(), row["id"]))
    conn.commit()
    return {"watch_id": row["id"], "task_id": task_id, "status": "stopped"}
