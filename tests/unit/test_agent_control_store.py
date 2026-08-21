"""Persistent Agent desired-state routing controls."""

from orchestrator import agent_control_store, state_store
from state.db import init_db


def test_control_override_invalidates_lease_and_is_audited(tmp_path):
    conn = init_db(tmp_path / "state.db")
    state_store.update_heartbeat(
        conn, "kimi", endpoint="http://127.0.0.1:8202",
        skills=["research"], lease_ttl_seconds=90)
    assert agent_control_store.desired_enabled(conn, "kimi", False) is False

    enabled = agent_control_store.set_enabled(
        conn, agent_id="kimi", enabled=True, updated_by="webui:admin")
    assert enabled["status"] == "probing"
    assert agent_control_store.desired_enabled(conn, "kimi", False) is True
    row = conn.execute("SELECT * FROM agents WHERE id = 'kimi';").fetchone()
    assert row["status"] == "offline"
    assert row["lease_expires_at"] is None

    disabled = agent_control_store.set_enabled(
        conn, agent_id="kimi", enabled=False, updated_by="webui:admin")
    assert disabled["status"] == "disabled"
    assert agent_control_store.desired_enabled(conn, "kimi", True) is False
    events = conn.execute(
        "SELECT event_type, payload_json FROM events"
        " WHERE event_type = 'agent.control.changed' ORDER BY seq;"
    ).fetchall()
    assert len(events) == 2
    assert '"enabled": false' in events[-1]["payload_json"]
