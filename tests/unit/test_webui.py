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
    assert detail["interactions"] == []
    assert detail["sessions"] == []
    assert detail["messages"] == []
    assert client.get("/api/interactions").json()["interactions"] == []
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
    assert "AGENT 交互" in r.text
    assert "用户介入" in r.text


def test_intervention_api(client, monkeypatch):
    from orchestrator.task_manager import TaskManager

    async def fake(self, task_id, **kwargs):
        return {"task_id": task_id, "context_revision": 2, **kwargs}

    monkeypatch.setattr(TaskManager, "intervene_agent_session", fake)
    response = client.post("/api/tasks/T-1/interventions", json={
        "mode": "steer", "agent_id": "codex",
        "content": {"text": "不要修改数据库"},
        "idempotency_key": "webui-1",
    })
    assert response.status_code == 200
    assert response.json()["mode"] == "steer"
    assert response.json()["context_revision"] == 2


def test_artifact_content(client, tmp_path, monkeypatch):
    from orchestrator import state_store
    from state.db import connect

    ws_file = tmp_path / "ws" / "tasks" / "T-2" / "artifacts" / "last.md"
    ws_file.parent.mkdir(parents=True)
    ws_file.write_text("# 结果\n正文内容", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("工作区外", encoding="utf-8")

    conn = connect()
    state_store.add_artifact(conn, task_id="T-2", agent_id="kimi",
                             name="last.md", path=str(ws_file),
                             sha256="0" * 64)
    state_store.add_artifact(conn, task_id="T-2", agent_id="kimi",
                             name="evil", path=str(outside),
                             sha256="0" * 64)
    conn.close()

    ok = client.get("/api/tasks/T-2/artifact-content?name=last.md")
    assert ok.status_code == 200
    assert "正文内容" in ok.json()["content"]
    assert ok.json()["truncated"] is False

    assert client.get(
        "/api/tasks/T-2/artifact-content?name=evil").status_code == 403
    assert client.get(
        "/api/tasks/T-2/artifact-content?name=nope").status_code == 404
