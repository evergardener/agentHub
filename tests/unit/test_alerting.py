"""Durable alert deduplication, delivery retry/escalation and task failure routing."""

from __future__ import annotations

import httpx
import pytest

from common.models import TaskStatus
from orchestrator import state_store
from state import alert_store
from state.db import init_db
from state.notifier import WebhookNotifier
from state.writer import StateWriter

pytestmark = pytest.mark.anyio


def test_webhook_config_rejects_plaintext_remote_credentials():
    from common import config

    with pytest.raises(ValueError, match="HTTPS"):
        config.validate_alert_webhook(
            "http://alerts.example.test/hook", "t" * 24)
    with pytest.raises(ValueError, match="至少 16"):
        config.validate_alert_webhook(
            "https://alerts.example.test/hook", "short")
    config.validate_alert_webhook("http://127.0.0.1:9099/hook")


def test_alerts_are_deduplicated_and_acknowledged(tmp_path):
    conn = init_db(tmp_path / "alerts.db")
    first = alert_store.upsert_alert(
        conn, kind="artifact_missing", severity="warning", source="janitor",
        task_id="T-1", detail="/workspace/result.md")
    second = alert_store.upsert_alert(
        conn, kind="artifact_missing", severity="warning", source="janitor",
        task_id="T-1", detail="/workspace/result.md")

    assert first["id"] == second["id"]
    assert second["occurrences"] == 2
    assert len(alert_store.list_alerts(conn, status="open")) == 1
    events = conn.execute(
        "SELECT * FROM events WHERE event_type = 'system.alert';").fetchall()
    assert len(events) == 1
    assert alert_store.acknowledge_alert(
        conn, first["id"], actor="webui:operator", note="investigating")
    assert alert_store.list_alerts(conn, status="open") == []
    assert not alert_store.acknowledge_alert(
        conn, first["id"], actor="webui:operator")


async def test_webhook_delivery_marks_the_digest_once(tmp_path):
    conn = init_db(tmp_path / "delivery.db")
    alert = alert_store.upsert_alert(
        conn, kind="timeout_swept", severity="critical", source="janitor",
        task_id="T-2", detail="timeout")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(
        conn, "https://alerts.example.test/hook", "t" * 24, client=client)
    try:
        assert await notifier.deliver_once() == {"delivered": 1, "failed": 0}
        assert await notifier.deliver_once() == {"delivered": 0, "failed": 0}
    finally:
        await client.aclose()

    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer " + "t" * 24
    row = conn.execute(
        "SELECT delivered_at FROM alerts WHERE id = ?;", (alert["id"],)
    ).fetchone()
    assert row["delivered_at"] is not None


async def test_delivery_failures_back_off_and_escalate(tmp_path):
    conn = init_db(tmp_path / "retry.db")
    alert = alert_store.upsert_alert(
        conn, kind="artifact_missing", severity="warning", source="janitor",
        task_id="T-3", detail="missing")

    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(503)))
    notifier = WebhookNotifier(
        conn, "https://alerts.example.test/hook", client=client)
    try:
        for _ in range(3):
            conn.execute(
                "UPDATE alerts SET next_delivery_at = '2000-01-01T00:00:00+08:00'"
                " WHERE id = ?;", (alert["id"],))
            conn.commit()
            assert (await notifier.deliver_once())["failed"] == 1
    finally:
        await client.aclose()

    row = conn.execute(
        "SELECT severity, delivery_attempts, last_delivery_error FROM alerts"
        " WHERE id = ?;", (alert["id"],)).fetchone()
    assert row["severity"] == "critical"
    assert row["delivery_attempts"] == 3
    assert row["last_delivery_error"] == "webhook HTTP 503"


def test_exhausted_task_failure_creates_critical_alert(tmp_path):
    writer = StateWriter(tmp_path / "writer.db")
    state_store.create_task(
        writer.conn, task_id="T-4", objective="fail", created_by="test",
        status=TaskStatus.WORKING, max_retries=1)
    result = writer.apply({
        "event_id": "E-failed",
        "event_type": "task.failed",
        "task_id": "T-4",
        "source": "codex",
        "payload": {"error": "tests failed", "attempt": 1},
    })

    assert result == "applied"
    alerts = alert_store.list_alerts(writer.conn, status="open")
    assert alerts[0]["kind"] == "task_retries_exhausted"
    assert alerts[0]["severity"] == "critical"
