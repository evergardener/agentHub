"""Web UI API 测试（Evolution v3 §5.2）：FastAPI TestClient + SQLite 临时库。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_DATABASE_URL",
                       f"sqlite:///{tmp_path}/webui.db")
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "ws"))
    for name in ("LAS_WEBUI_TOKENS", "LAS_WEBUI_SESSION_SECRET",
                 "LAS_WEBUI_REQUIRE_AUTH", "LAS_WEBUI_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)

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
    from orchestrator import (
        agent_profile_store,
        collaboration_store,
        task_plan_store,
    )
    agent_profile_store.seed_catalog(conn)
    agent_profile_store.assign_seed_profile(conn, "codex")
    conversation_id = collaboration_store.create_conversation(conn)
    collaboration_id = collaboration_store.create_collaboration(
        conn, conversation_id=conversation_id, objective="planned research")
    conn.execute(
        "UPDATE tasks SET collaboration_id = ? WHERE id = 'T-2';",
        (collaboration_id,),
    )
    task_plan_store.create_plan(
        conn, collaboration_id=collaboration_id, objective="planned research",
        project="webui-test", steps=[{
            "key": "research", "task_id": "T-2", "objective": "调研 X",
            "agent_id": "codex", "profile_id": "AP-CODEX-BACKEND",
            "profile_version": 1, "depends_on": [],
            "expected_artifacts": ["report.md"],
            "acceptance_criteria": ["report exists"],
            "expected_operations": ["filesystem.read"],
        }])
    from state import alert_store
    alert_store.upsert_alert(
        conn, kind="artifact_missing", severity="warning", source="janitor",
        task_id="T-2", detail="result.md")
    conn.close()

    from webui.server import create_app

    return TestClient(create_app())


@pytest.fixture
def secure_client(client, monkeypatch):
    monkeypatch.setenv(
        "LAS_WEBUI_TOKENS",
        '{"admin-token-0123456789":"admin",'
        '"operator-token-012345":"operator",'
        '"viewer-token-01234567":"viewer"}',
    )
    monkeypatch.setenv("LAS_WEBUI_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("LAS_WEBUI_REQUIRE_AUTH", "true")
    from webui.server import create_app

    return TestClient(create_app())


def _login(client, token):
    response = client.post("/api/auth/login", json={"token": token})
    return response, response.json().get("csrf")


def test_overview(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ready"
    ov = client.get("/api/overview").json()
    assert ov["task_counts"]["blocked"] == 1
    assert ov["agents"][0]["id"] == "codex"
    assert ov["agents"][0]["endpoint"] == "http://x:8201"
    assert ov["agents"][0]["online"] is True
    assert "fake" not in {agent["id"] for agent in ov["agents"]}
    kimi = next(agent for agent in ov["agents"] if agent["id"] == "kimi")
    assert kimi["enabled"] is False
    assert kimi["status"] == "disabled"
    assert ov["alert_counts"] == {"warning": 1}


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
    assert detail["plan_step"] is None
    assert detail["plan_steps"] == []
    planned = client.get("/api/tasks/T-2").json()
    assert planned["collaboration"]["controller"] == "hermes"
    assert planned["collaboration"]["phase"] == "planning"
    assert planned["collaboration"]["context_revision"] == 1
    assert planned["plan_step"]["step_key"] == "research"
    assert planned["plan_step"]["profile_id"] == "AP-CODEX-BACKEND"
    assert planned["plan_steps"][0]["task_status"] == "queued"
    assert client.get("/api/interactions").json()["interactions"] == []
    assert client.get("/api/tasks/NOPE").status_code == 404


def test_events_cursor(client):
    evs = client.get("/api/events?after=0").json()["events"]
    assert evs[0]["seq"] >= 1
    assert client.get(f"/api/events?after={evs[-1]['seq']}").json()["events"] == []


def test_alerts_can_be_acknowledged(client):
    alerts = client.get("/api/alerts?status=open").json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["occurrences"] == 1
    assert alerts[0]["task_exists"] is True
    response = client.post(
        f"/api/alerts/{alerts[0]['id']}/acknowledge",
        json={"note": "investigating"})
    assert response.status_code == 200
    assert client.get("/api/alerts?status=open").json()["alerts"] == []
    assert client.post(
        f"/api/alerts/{alerts[0]['id']}/acknowledge", json={}).status_code == 409


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
    assert r.headers["cache-control"] == "no-store"
    assert "agentHub" in r.text
    assert "AGENT 交互" in r.text
    assert "用户介入" in r.text
    assert "接管子 Agent" in r.text
    assert "归还 Hermes 并重新规划" in r.text
    assert "协作会话" in r.text
    assert "委派指令（原文）" in r.text
    assert "完整多轮消息" in r.text


def test_index_has_bounded_alert_center_and_in_page_dialogs(client):
    page = client.get("/").text
    assert 'id="alert-drawer"' in page
    assert 'class="alert-list" id="alerts"' in page
    assert "标记已知仅关闭此告警" in page
    assert "历史测试产物已被清理" in page
    assert "先处理问题，再标记已知" in page
    assert 'id="login-token"' in page
    assert "/api/alerts?status=open&limit=1000" in page
    assert "prompt(" not in page
    assert "confirm(" not in page
    assert "alert(" not in page


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

    detail = client.get("/api/tasks/T-2").json()
    availability = {item["name"]: item["available"]
                    for item in detail["artifacts"]}
    assert availability["last.md"] is True
    assert availability["evil"] is False

    ok = client.get("/api/tasks/T-2/artifact-content?name=last.md")
    assert ok.status_code == 200
    assert "正文内容" in ok.json()["content"]
    assert ok.json()["truncated"] is False

    assert client.get(
        "/api/tasks/T-2/artifact-content?name=evil").status_code == 403
    assert client.get(
        "/api/tasks/T-2/artifact-content?name=nope").status_code == 404


def test_orphan_alert_has_no_task_link(client):
    from state import alert_store
    from state.db import connect

    conn = connect()
    alert_store.upsert_alert(
        conn, kind="artifact_missing", severity="warning", source="janitor",
        task_id="T-DELETED", detail="old-result.md")
    conn.close()

    alerts = client.get("/api/alerts?status=open").json()["alerts"]
    orphan = next(alert for alert in alerts if alert["task_id"] == "T-DELETED")
    assert orphan["task_exists"] is False


def test_collaboration_multi_turn_api(client):
    from orchestrator import collaboration_store
    from state.db import connect

    conn = connect()
    collaboration = conn.execute(
        "SELECT id, conversation_id, context_revision FROM collaborations"
        " ORDER BY created_at LIMIT 1;").fetchone()
    for sender_type, sender_id, message_type, text in [
        ("user", "user", "llm.user", "第一轮：先分析风险"),
        ("hermes", "hermes", "llm.assistant", "已分析，等待下一轮"),
        ("user", "user", "llm.user", "第二轮：继续同一上下文"),
    ]:
        collaboration_store.append_message(
            conn, conversation_id=collaboration["conversation_id"],
            collaboration_id=collaboration["id"], sender_type=sender_type,
            sender_id=sender_id, message_type=message_type,
            content={"role": sender_type, "content": text},
            based_on_revision=collaboration["context_revision"])
    conn.close()

    rows = client.get("/api/collaborations").json()["collaborations"]
    item = next(row for row in rows if row["id"] == collaboration["id"])
    assert item["message_count"] == 3
    assert item["task_count"] == 1
    detail = client.get(f"/api/collaborations/{collaboration['id']}").json()
    assert [m["sequence"] for m in detail["messages"]] == [1, 2, 3]
    assert detail["messages"][2]["content_json"].endswith(
        '"content":"第二轮：继续同一上下文"}')
    assert detail["tasks"][0]["id"] == "T-2"
    assert client.get("/api/collaborations/COL-NOPE").status_code == 404


def test_secure_webui_login_cookie_and_csrf(secure_client):
    assert secure_client.get("/").status_code == 200
    assert secure_client.get("/health").status_code == 200
    assert secure_client.get("/ready").status_code == 200
    assert secure_client.get("/api/overview").status_code == 401
    assert secure_client.get("/api/auth/status").status_code == 401
    assert _login(secure_client, "wrong-token-012345")[0].status_code == 401

    login, csrf = _login(secure_client, "admin-token-0123456789")
    assert login.status_code == 200
    assert login.json()["role"] == "admin"
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert secure_client.get("/api/auth/status").json()["csrf"] == csrf

    assert secure_client.post(
        "/api/grants", json={"pattern": "restart"}).status_code == 403
    allowed = secure_client.post(
        "/api/grants", json={"pattern": "restart"},
        headers={"X-CSRF-Token": csrf})
    assert allowed.status_code == 200


def test_secure_webui_role_boundaries(secure_client, monkeypatch):
    from orchestrator.task_manager import TaskManager

    async def fake(self, task_id, **kwargs):
        return {"task_id": task_id, **kwargs}

    monkeypatch.setattr(TaskManager, "intervene_agent_session", fake)

    viewer, viewer_csrf = _login(secure_client, "viewer-token-01234567")
    assert viewer.status_code == 200
    assert secure_client.get("/api/overview").status_code == 200
    assert secure_client.post(
        "/api/tasks/T-1/interventions", json={"mode": "comment"},
        headers={"X-CSRF-Token": viewer_csrf}).status_code == 403
    alert_id = secure_client.get("/api/alerts?status=open").json()["alerts"][0]["id"]
    assert secure_client.post(
        f"/api/alerts/{alert_id}/acknowledge", json={},
        headers={"X-CSRF-Token": viewer_csrf}).status_code == 403

    operator, operator_csrf = _login(
        secure_client, "operator-token-012345")
    assert operator.status_code == 200
    intervention = secure_client.post(
        "/api/tasks/T-1/interventions",
        json={"mode": "comment", "content": {"text": "note"}},
        headers={"X-CSRF-Token": operator_csrf})
    assert intervention.status_code == 200
    assert secure_client.post(
        "/api/grants", json={"pattern": "restart"},
        headers={"X-CSRF-Token": operator_csrf}).status_code == 403
    assert secure_client.post(
        f"/api/alerts/{alert_id}/acknowledge", json={"note": "owned"},
        headers={"X-CSRF-Token": operator_csrf}).status_code == 200


def test_secure_webui_rejects_tampered_cookie(secure_client):
    login, _ = _login(secure_client, "admin-token-0123456789")
    assert login.status_code == 200
    value = secure_client.cookies.get("agenthub_session")
    secure_client.cookies.set("agenthub_session", value[:-1] + "x")
    assert secure_client.get("/api/overview").status_code == 401


def test_webui_security_bind_validation(monkeypatch):
    from webui.server import validate_webui_security

    for name in ("LAS_WEBUI_TOKENS", "LAS_WEBUI_SESSION_SECRET",
                 "LAS_WEBUI_REQUIRE_AUTH"):
        monkeypatch.delenv(name, raising=False)
    validate_webui_security("127.0.0.1")
    with pytest.raises(RuntimeError, match="非 loopback"):
        validate_webui_security("0.0.0.0")

    monkeypatch.setenv("LAS_WEBUI_REQUIRE_AUTH", "true")
    with pytest.raises(RuntimeError, match="TOKENS 为空"):
        validate_webui_security("127.0.0.1")
    monkeypatch.setenv(
        "LAS_WEBUI_TOKENS", '{"admin-token-0123456789":"admin"}')
    monkeypatch.setenv("LAS_WEBUI_SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="至少 32"):
        validate_webui_security("0.0.0.0")
