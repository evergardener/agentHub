"""Web UI API 测试（Evolution v3 §5.2）：FastAPI TestClient + SQLite 临时库。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_DATABASE_URL",
                       f"sqlite:///{tmp_path}/webui.db")
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "ws"))

    from common.models import TaskStatus
    from orchestrator import state_store
    from state.db import init_db

    conn = init_db(tmp_path / "webui.db")
    # 一个 blocked 任务（等审批）+ 一个 completed 任务
    state_store.create_task(conn, task_id="T-1", objective="重启 nginx",
                            created_by="test", status=TaskStatus.QUEUED)
    state_store.transition_task(conn, "T-1", TaskStatus.ASSIGNED)
    state_store.transition_task(conn, "T-1", TaskStatus.WORKING)
    state_store.transition_task(conn, "T-1", TaskStatus.BLOCKED)
    state_store.create_task(conn, task_id="T-2", objective="调研 X",
                            created_by="test", status=TaskStatus.QUEUED)
    state_store.record_event(conn, {"event_id": "e1",
                                    "event_type": "task.blocked",
                                    "task_id": "T-1"})
    state_store.update_heartbeat(conn, "codex", lease_ttl_seconds=90,
                                 endpoint="http://x:8201", skills=["coding"])
    conn.close()

    from webui.server import create_app

    return TestClient(create_app())


def test_overview(client):
    ov = client.get("/api/overview").json()
    assert ov["task_counts"]["blocked"] == 1
    assert ov["agents"][0]["id"] == "codex"
    assert ov["agents"][0]["endpoint"] == "http://x:8201"


def test_tasks_and_detail(client):
    tasks = client.get("/api/tasks").json()["tasks"]
    assert len(tasks) == 2
    blocked = client.get("/api/tasks?status=blocked").json()["tasks"]
    assert [t["id"] for t in blocked] == ["T-1"]
    detail = client.get("/api/tasks/T-1").json()
    assert detail["task"]["objective"] == "重启 nginx"
    assert detail["events"][0]["event_type"] == "task.blocked"
    assert client.get("/api/tasks/NOPE").status_code == 404


def test_events_cursor(client):
    evs = client.get("/api/events?after=0").json()["events"]
    assert evs[0]["seq"] >= 1
    assert client.get(f"/api/events?after={evs[-1]['seq']}").json()["events"] == []


def test_approve_and_reject(client):
    r = client.post("/api/tasks/T-1/approve", json={"notes": "ok"}).json()
    assert r["status"] == "working"
    # 非 blocked 状态再批准 → 409
    assert client.post("/api/tasks/T-1/approve", json={}).status_code == 409
    # T-2 未到 blocked，拒绝也 409
    assert client.post("/api/tasks/T-2/reject", json={}).status_code == 409


def test_grants_api(client):
    r = client.post("/api/grants", json={"pattern": "重启"}).json()
    assert r["grant_id"] >= 1
    bad = client.post("/api/grants", json={"pattern": "删除"})
    assert bad.status_code == 400
    ov = client.get("/api/overview").json()
    assert ov["grants"][0]["pattern"] == "重启"
    r2 = client.post(f"/api/grants/{r['grant_id']}/revoke").json()
    assert r2["revoked"] is True
    assert client.get("/api/overview").json()["grants"] == []


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "agentHub" in r.text
