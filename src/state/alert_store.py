"""Durable, deduplicated alert inbox and webhook outbox."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from common.events import Event
from state.db import CST, now_iso

SEVERITIES = {"info", "warning", "critical"}


def _as_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _dedupe_key(kind: str, task_id: str | None, detail: str | None) -> str:
    raw = "\0".join((kind, task_id or "", detail or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert_alert(
    conn,
    *,
    kind: str,
    severity: str,
    source: str,
    task_id: str | None = None,
    detail: str | None = None,
    commit: bool = True,
) -> dict:
    if severity not in SEVERITIES:
        raise ValueError(f"unsupported alert severity: {severity}")
    key = _dedupe_key(kind, task_id, detail)
    existing = conn.execute(
        "SELECT id FROM alerts WHERE dedupe_key = ?;", (key,)
    ).fetchone()
    alert_id = existing["id"] if existing else f"AL-{uuid.uuid4().hex[:16]}"
    timestamp = now_iso()
    conn.execute(
        "INSERT INTO alerts (id, dedupe_key, kind, severity, source, task_id,"
        " detail, status, occurrences, first_seen_at, last_seen_at,"
        " next_delivery_at) VALUES (?,?,?,?,?,?,?,'open',1,?,?,?)"
        " ON CONFLICT(dedupe_key) DO UPDATE SET"
        " occurrences = alerts.occurrences + 1,"
        " last_seen_at = excluded.last_seen_at;",
        (alert_id, key, kind, severity, source, task_id, detail,
         timestamp, timestamp, timestamp),
    )
    if existing is None:
        from orchestrator import state_store

        state_store.record_event(conn, Event(
            event_type="system.alert",
            source=source,
            task_id=task_id,
            payload={"alert_id": alert_id, "kind": kind,
                     "severity": severity, "detail": detail},
        ).to_dict(), commit=False)
    row = conn.execute("SELECT * FROM alerts WHERE id = ?;", (alert_id,)).fetchone()
    if commit:
        conn.commit()
    return _as_dict(row)


def list_alerts(conn, *, status: str | None = None, limit: int = 200) -> list[dict]:
    limit = min(max(int(limit), 1), 1000)
    where = " WHERE status = ?" if status else ""
    params = (status, limit) if status else (limit,)
    rows = conn.execute(
        "SELECT * FROM alerts" + where
        + " ORDER BY CASE severity WHEN 'critical' THEN 0"
          " WHEN 'warning' THEN 1 ELSE 2 END, last_seen_at DESC LIMIT ?;",
        params,
    ).fetchall()
    return [_as_dict(row) for row in rows]


def due_alerts(conn, *, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alerts WHERE status = 'open' AND delivered_at IS NULL"
        " AND next_delivery_at <= ? ORDER BY first_seen_at LIMIT ?;",
        (now_iso(), min(max(int(limit), 1), 100)),
    ).fetchall()
    return [_as_dict(row) for row in rows]


def record_delivery(conn, alert_id: str, *, error: str | None = None) -> None:
    timestamp = now_iso()
    if error is None:
        conn.execute(
            "UPDATE alerts SET delivered_at = ?, last_delivery_error = NULL"
            " WHERE id = ?;", (timestamp, alert_id))
    else:
        row = conn.execute(
            "SELECT delivery_attempts FROM alerts WHERE id = ?;",
            (alert_id,),
        ).fetchone()
        if row is None:
            return
        attempts = int(row["delivery_attempts"]) + 1
        delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
        next_at = (datetime.now(CST) + timedelta(seconds=delay)).isoformat(
            timespec="seconds")
        conn.execute(
            "UPDATE alerts SET delivery_attempts = ?, next_delivery_at = ?,"
            " last_delivery_error = ?,"
            " severity = CASE WHEN ? >= 3 THEN 'critical' ELSE severity END"
            " WHERE id = ?;",
            (attempts, next_at, error[:240], attempts, alert_id),
        )
    conn.commit()


def acknowledge_alert(
    conn, alert_id: str, *, actor: str, note: str | None = None
) -> bool:
    cur = conn.execute(
        "UPDATE alerts SET status = 'acknowledged', acknowledged_at = ?,"
        " acknowledged_by = ?, acknowledgement_note = ?"
        " WHERE id = ? AND status = 'open';",
        (now_iso(), actor, (note or "")[:500], alert_id),
    )
    conn.commit()
    return cur.rowcount == 1


def resolve_condition(
    conn,
    *,
    kind: str,
    task_id: str | None = None,
    detail: str | None = None,
    source: str = "system",
) -> bool:
    """Close an open condition alert after the source verifies recovery."""
    cur = conn.execute(
        "UPDATE alerts SET status = 'resolved', acknowledged_at = ?,"
        " acknowledged_by = ?, acknowledgement_note = ?"
        " WHERE dedupe_key = ? AND status = 'open';",
        (now_iso(), source, "condition recovered",
         _dedupe_key(kind, task_id, detail)),
    )
    conn.commit()
    return cur.rowcount == 1
