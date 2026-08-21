"""Persistent desired-state controls for Agent discovery and routing."""

from __future__ import annotations

from uuid import uuid4

from state.db import now_iso


def desired_enabled(conn, agent_id: str, catalog_enabled: bool = True) -> bool:
    """Return the operator override, falling back to the static catalog."""
    row = conn.execute(
        "SELECT enabled FROM agent_controls WHERE agent_id = ?;",
        (agent_id,),
    ).fetchone()
    return bool(row["enabled"]) if row is not None else bool(catalog_enabled)


def set_enabled(conn, *, agent_id: str, enabled: bool,
                updated_by: str) -> dict:
    """Persist desired state and invalidate any stale discovery lease."""
    ts = now_iso()
    value = int(enabled)
    conn.execute(
        "INSERT INTO agent_controls (agent_id, enabled, updated_by, updated_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(agent_id) DO UPDATE SET enabled = excluded.enabled,"
        " updated_by = excluded.updated_by, updated_at = excluded.updated_at;",
        (agent_id, value, updated_by, ts),
    )
    # Enabling never trusts an old lease. The next healthy heartbeat performs
    # discovery again; disabling immediately removes the Agent from routing.
    conn.execute(
        "UPDATE agents SET status = ?, lease_expires_at = NULL, updated_at = ?"
        " WHERE id = ?;",
        ("offline" if enabled else "disabled", ts, agent_id),
    )
    from orchestrator import state_store

    state_store.record_event(conn, {
        "event_id": f"agent-control-{agent_id}-{uuid4().hex[:12]}",
        "event_type": "agent.control.changed",
        "source": updated_by,
        "payload": {"agent_id": agent_id, "enabled": enabled},
        "timestamp": ts,
    }, commit=False)
    conn.commit()
    return {"agent_id": agent_id, "enabled": enabled,
            "status": "probing" if enabled else "disabled",
            "updated_by": updated_by, "updated_at": ts}
