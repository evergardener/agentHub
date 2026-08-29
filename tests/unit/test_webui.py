"""Web UI API 测试（Evolution v3 §5.2）：FastAPI TestClient + SQLite 临时库。"""

from __future__ import annotations

import json

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
        supervision_store,
        task_plan_store,
    )
    agent_profile_store.seed_catalog(conn)
    agent_profile_store.assign_seed_profile(conn, "codex")
    mapping = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-webui-test",
        objective="planned research")
    collaboration_id = mapping["collaboration_id"]
    conn.execute(
        "UPDATE tasks SET collaboration_id = ? WHERE id = 'T-2';",
        (collaboration_id,),
    )
    supervision_store.register_watch(
        conn, peer="qishuo", context_id="ctx-webui-test", task_id="T-2")
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


def test_agent_control_requires_boolean_and_updates_overview(client):
    assert client.patch("/api/agents/nope", json={"enabled": True}).status_code == 404
    assert client.patch("/api/agents/kimi", json={"enabled": "yes"}).status_code == 400
    enabled = client.patch("/api/agents/kimi", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "probing"
    kimi = next(a for a in client.get("/api/overview").json()["agents"]
                if a["id"] == "kimi")
    assert kimi["enabled"] is True
    assert kimi["status"] == "offline"

    disabled = client.patch("/api/agents/kimi", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    kimi = next(a for a in client.get("/api/overview").json()["agents"]
                if a["id"] == "kimi")
    assert kimi["enabled"] is False
    assert kimi["status"] == "disabled"


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


def test_approvals_exclude_blocked_tasks_without_pending_interaction(client):
    """A historical blocked task is not an actionable approval by itself."""
    assert client.get("/api/approvals").json()["tasks"] == []


def test_approve_and_reject(client):
    r = client.post("/api/tasks/T-1/approve", json={"notes": "ok"}).json()
    assert r["status"] == "working"
    # 非 blocked 状态再批准 → 409
    assert client.post("/api/tasks/T-1/approve", json={}).status_code == 409
    # T-2 未到 blocked，拒绝也 409
    assert client.post("/api/tasks/T-2/reject", json={}).status_code == 409


def test_predelegation_approval_is_listed_and_can_be_approved(
        client, monkeypatch):
    from common.models import TaskStatus
    from orchestrator import state_store
    from orchestrator.task_manager import TaskManager
    from state.db import connect

    conn = connect()
    state_store.create_task(
        conn, task_id="T-3", objective="modify config", created_by="qishuo",
        status=TaskStatus.QUEUED)
    state_store.record_event(conn, {
        "event_id": "approval-T-3",
        "event_type": "task.approval_requested", "task_id": "T-3",
        "payload": {"agent_id": "codex", "endpoint": "http://x:8201",
                    "risk": "write", "reason": "test"},
    })
    conn.close()

    async def fake_delegate(self, task_id, endpoint, agent_id, attempt=1):
        state_store.transition_task(self.conn, task_id, TaskStatus.ASSIGNED)

    monkeypatch.setattr(TaskManager, "delegate_task", fake_delegate)
    approvals = client.get("/api/approvals").json()["tasks"]
    pending = next(item for item in approvals if item["id"] == "T-3")
    assert pending["status"] == "input_required"
    assert pending["approval_kind"] == "delegation"

    approved = client.post(
        "/api/tasks/T-3/approve", json={"notes": "approved once"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "assigned"
    assert "T-3" not in {
        item["id"] for item in client.get("/api/approvals").json()["tasks"]}


def test_acceptance_queue_is_separate_and_rework_requires_feedback(client):
    from common.models import TaskStatus
    from orchestrator import state_store
    from state.db import connect

    conn = connect()
    for task_id in ("T-accept", "T-rework", "T-legacy-completed"):
        state_store.create_task(
            conn, task_id=task_id, objective="验收任务",
            created_by="test", status=TaskStatus.QUEUED)
        final_status = (
            TaskStatus.COMPLETED if task_id == "T-legacy-completed"
            else TaskStatus.AWAITING_ACCEPTANCE)
        for status in (
            TaskStatus.ASSIGNED, TaskStatus.WORKING, final_status,
        ):
            state_store.transition_task(conn, task_id, status)
    conn.close()

    assert {row["id"] for row in client.get(
        "/api/acceptance").json()["tasks"]} == {
            "T-accept", "T-rework", "T-legacy-completed"}
    assert {row["id"] for row in client.get(
        "/api/approvals").json()["tasks"]}.isdisjoint(
            {"T-accept", "T-rework", "T-legacy-completed"})

    legacy_accepted = client.post(
        "/api/tasks/T-legacy-completed/accept", json={"notes": "历史任务确认"})
    assert legacy_accepted.status_code == 200
    assert legacy_accepted.json()["status"] == "accepted"

    accepted = client.post(
        "/api/tasks/T-accept/accept", json={"notes": "用户确认"})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert client.post(
        "/api/tasks/T-rework/request-rework", json={}).status_code == 409
    rework = client.post(
        "/api/tasks/T-rework/request-rework",
        json={"feedback": "请补充失败恢复测试"})
    assert rework.status_code == 200
    assert rework.json()["status"] == "rework_pending"


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
    assert "待用户验收" in r.text
    assert "/request-rework" in r.text
    assert "agentHub" in r.text
    assert "AGENT 交互" in r.text
    assert "Hermes 处理中" in r.text
    assert "任务控制" in r.text
    assert "接管子 Agent" in r.text
    assert "归还 Hermes 并重新规划" in r.text
    assert "协作会话" in r.text
    assert "任务目标" in r.text
    assert "SESSIONS" in r.text
    assert "任务导航" not in r.text
    assert "点击查看详情" not in r.text
    assert 'id="session-filter"' in r.text
    assert 'id="conversation-list"' in r.text
    assert 'class="card task-detail-pane" id="drawer"' in r.text
    assert 'id="ops-drawer"' in r.text
    assert 'data-agent-toggle=' in r.text
    assert "backdrop-filter: blur(24px)" in r.text
    assert 'aria-label="协作会话工作区"' in r.text
    assert 'id="conversation-select"' in r.text
    assert 'id="chat-transcript"' in r.text
    assert 'id="chat-composer"' in r.text
    assert 'id="chat-target"' in r.text
    assert 'id="chat-mention-menu"' in r.text
    assert 'role="listbox"' in r.text
    assert 'id="chat-recipient"' not in r.text
    assert 'id="settings-drawer"' in r.text
    assert 'id="session-title-layer"' in r.text
    assert "输入 @ 选择 Hermes 或当前 Agent" in r.text
    assert "function composerRecipients" in r.text
    assert "function parseComposerTarget" in r.text
    assert 'const id = ids[0] || "hermes";' in r.text
    assert "多个 @ 接收者" in r.text
    assert "等待 Hermes 处理" in r.text
    assert "需要执行时将创建关联的新任务" in r.text
    assert "terminal || !target.available" in r.text
    assert 'agent_id: target.id' in r.text
    assert 'class="task-action-input" id="acceptance-feedback"' in r.text
    # Task follow-up text is sent from the central @mention composer; the
    # task-control pane must not expose a second competing conversation box.
    assert 'id="d-intervention-text"' not in r.text
    assert ".task-action-input:focus" in r.text
    assert 'id="acceptance-feedback" rows="2" style=' not in r.text
    assert 'id="d-intervention-text" rows="2" style=' not in r.text
    assert "const wasNearBottom = panel.scrollHeight - panel.scrollTop" in r.text
    assert "wasNearBottom ? panel.scrollHeight : previousTop" in r.text
    assert 'data-task-detail-tab="goals"' in r.text
    assert 'data-task-detail-tab="execution"' in r.text
    assert "目标与交付" in r.text
    assert "执行记录" in r.text
    assert "S.taskDetailTab = name" in r.text
    assert "selectTaskDetailTab(S.taskDetailTab, {rememberScroll: false})" in r.text
    assert "taskDetailScrolls: new Map()" in r.text
    assert "function rememberTaskDetailScroll()" in r.text
    assert "function restoreTaskDetailScroll()" in r.text
    assert "panel.dataset.taskDetailPanel" in r.text
    assert '$("#d-body").scrollTop = 0;' not in r.text
    assert "previousTaskScroll" not in r.text
    assert "无法批准此请求" in r.text
    assert "绝对 workspace" in r.text
    assert ".workspace-card > .chat-transcript" in r.text
    assert "overscroll-behavior: contain" in r.text
    assert 'class="markdown-body"' in r.text
    assert ".message.agent .message-shell" in r.text
    assert "align-items: start" in r.text
    assert "grid-column: 1; grid-row: 1; align-self: start" in r.text
    assert r.text.count("${avatarHtml}") == 1
    assert ".task-detail-pane .d-body" in r.text
    assert "canWriteHermes" in r.text
    assert "/messages`" in r.text
    assert "产物已显示在右侧" in r.text
    assert '<div class="task-section-label">结果摘要</div>' not in r.text
    assert 'esc(d.objective_title || "（空）")' in r.text
    assert "d.objective_summary" in r.text
    assert "查看完整下发指令" in r.text
    assert 'esc(d.dispatched_objective || "（空）")' in r.text
    assert 'esc(t.objective || "（空）")' not in r.text
    assert "esc(plan.plan_objective)" not in r.text


def test_task_detail_uses_task_scoped_dispatch_objective(client):
    from orchestrator import collaboration_store
    from state.db import connect

    conn = connect()
    task = conn.execute(
        "SELECT id, collaboration_id FROM tasks WHERE id = 'T-2';"
    ).fetchone()
    collaboration = collaboration_store.get_collaboration(
        conn, task["collaboration_id"])
    collaboration_store.append_message(
        conn,
        conversation_id=collaboration["conversation_id"],
        collaboration_id=task["collaboration_id"],
        sender_type="user",
        sender_id="user",
        message_type="llm.user",
        content={"text": "这是 Session 第一句对话，不是任务目标"},
        based_on_revision=collaboration["context_revision"],
    )
    collaboration_store.append_message(
        conn,
        conversation_id=collaboration["conversation_id"],
        collaboration_id=task["collaboration_id"],
        task_id=task["id"],
        agent_id="codex",
        sender_type="hermes",
        sender_id="qishuo",
        recipient_type="agent",
        recipient_id="codex",
        message_type="a2a.task.request",
        content={"text": "实际下发给 Codex 的完整实施目标"},
        based_on_revision=collaboration["context_revision"],
    )
    conn.close()

    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["objective"] == "调研 X"
    assert detail["dispatched_objective"] == "实际下发给 Codex 的完整实施目标"
    assert detail["objective_title"] == "实际下发给 Codex 的完整实施目标"
    assert detail["instruction_source"] == "a2a_task_request"


def test_task_detail_summarizes_long_dispatch_but_preserves_audit_text(client):
    from orchestrator import collaboration_store
    from state.db import connect

    objective = (
        "修复 agentHub GitHub Actions Docker 发布流水线在 commit "
        "71581e944614c3e4558e2e2cd52e3d92b6b2cbb0 失败的问题。"
        "事实证据：Actions run 32681623853 的 test 与 multi-platform "
        "candidate build/push 成功；Trivy gate 失败，扫描结果 Total 36。"
        "不得降低扫描门禁，也不得直接修改 main。"
    )
    conn = connect()
    task = conn.execute(
        "SELECT id, collaboration_id FROM tasks WHERE id = 'T-2';"
    ).fetchone()
    collaboration = collaboration_store.get_collaboration(
        conn, task["collaboration_id"])
    collaboration_store.append_message(
        conn,
        conversation_id=collaboration["conversation_id"],
        collaboration_id=task["collaboration_id"],
        task_id=task["id"], agent_id="codex",
        sender_type="hermes", sender_id="hermes",
        recipient_type="agent", recipient_id="codex",
        message_type="a2a.task.request",
        content={"text": objective},
        based_on_revision=collaboration["context_revision"],
    )
    conn.close()

    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["objective_title"] == "修复 GitHub 流水线构建失败问题"
    assert detail["objective_summary"].startswith("事实证据：Actions run")
    assert len(detail["objective_summary"]) <= 181
    assert detail["dispatched_objective"] == objective


def test_task_detail_prefers_structured_display_copy(client):
    from state.db import connect

    conn = connect()
    conn.execute(
        "UPDATE tasks SET plan_context_json = ? WHERE id = 'T-2';",
        (json.dumps({
            "display_title": "结构化任务标题",
            "objective_summary": "结构化简要说明",
        }, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()

    detail = client.get("/api/tasks/T-2").json()
    assert detail["objective_title"] == "结构化任务标题"
    assert detail["objective_summary"] == "结构化简要说明"
    assert detail["dispatched_objective"] == "调研 X"


def test_task_detail_falls_back_to_plan_step_then_task_record(client):
    planned = client.get("/api/tasks/T-2").json()
    assert planned["dispatched_objective"] == "调研 X"
    assert planned["instruction_source"] == "task_plan_step"

    legacy = client.get("/api/tasks/T-1").json()
    assert legacy["dispatched_objective"] == "重启 nginx"
    assert legacy["instruction_source"] == "task_record"


def test_collaboration_detail_renders_safe_markdown(client):
    from orchestrator import collaboration_store
    from state.db import connect

    conn = connect()
    collaboration = conn.execute(
        "SELECT id, conversation_id, context_revision FROM collaborations"
        " ORDER BY created_at LIMIT 1;"
    ).fetchone()
    collaboration_store.append_message(
        conn,
        conversation_id=collaboration["conversation_id"],
        collaboration_id=collaboration["id"],
        sender_type="agent",
        sender_id="codex",
        message_type="llm.assistant",
        content={"text": (
            "# 修复结果\n\n- 支持列表\n- 支持 `code`\n\n"
            "| 项目 | 状态 |\n| --- | --- |\n| WebUI | 完成 |\n\n"
            "<script>alert(1)</script>\n\n[x](javascript:alert(1))"
        )},
        based_on_revision=collaboration["context_revision"],
    )
    conn.close()

    detail = client.get(
        f"/api/collaborations/{collaboration['id']}"
    ).json()
    html = detail["messages"][-1]["content_html"]
    assert "<h1>修复结果</h1>" in html
    assert "<li>支持列表</li>" in html
    assert "<code>code</code>" in html
    assert "<table>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html
    assert 'href="javascript:' not in html


def test_collaboration_detail_includes_native_agent_messages_and_tools(client):
    from orchestrator import state_store
    from state.db import connect

    conn = connect()
    task = conn.execute(
        "SELECT id, collaboration_id FROM tasks WHERE id = 'T-2';"
    ).fetchone()
    conn.execute("UPDATE tasks SET assigned_to = 'codex' WHERE id = ?;",
                 (task["id"],))
    state_store.record_event(conn, {
        "event_id": "native-message", "event_type": "agent.session.event",
        "task_id": task["id"], "source": "codex",
        "payload": {
            "nativeEventType": "item.lifecycle",
            "data": {"phase": "completed", "item": {
                "id": "msg-1", "type": "agentMessage",
                "text": "我已定位 Dockerfile 中的基础镜像问题。",
                "phase": "commentary",
            }},
        },
    })
    state_store.record_event(conn, {
        "event_id": "native-tool-start", "event_type": "agent.session.event",
        "task_id": task["id"], "source": "codex",
        "payload": {
            "nativeEventType": "item.lifecycle",
            "data": {"phase": "started", "item": {
                "id": "edit-1", "type": "fileChange",
                "changes": [{"path": "/project/Dockerfile",
                             "kind": {"type": "update"}}],
            }},
        },
    })
    state_store.record_event(conn, {
        "event_id": "native-tool-complete", "event_type": "agent.session.event",
        "task_id": task["id"], "source": "codex",
        "payload": {
            "nativeEventType": "item.lifecycle",
            "data": {"phase": "completed", "item": {
                "id": "edit-1", "type": "fileChange", "status": "completed",
                "changes": [{"path": "/project/Dockerfile",
                             "kind": {"type": "update"}}],
            }},
        },
    })
    conn.close()

    detail = client.get(
        f"/api/collaborations/{task['collaboration_id']}"
    ).json()
    activity = detail["agent_activity"]
    assert [item["message_type"] for item in activity] == [
        "agent.activity.message", "agent.activity.tool"]
    assert json.loads(activity[0]["content_json"])["text"].startswith(
        "我已定位 Dockerfile")
    tool = json.loads(activity[1]["content_json"])["tool_calls"][0]
    assert tool["name"] == "fileChange"
    assert tool["arguments"]["status"] == "completed"
    assert activity[1]["sequence"].startswith("event-")


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
    assert item["assigned_agents"] == []
    detail = client.get(f"/api/collaborations/{collaboration['id']}").json()
    assert [m["sequence"] for m in detail["messages"]] == [1, 2, 3]
    assert detail["messages"][2]["content_json"].endswith(
        '"content":"第二轮：继续同一上下文"}')
    assert detail["tasks"][0]["id"] == "T-2"
    assert client.get("/api/collaborations/COL-NOPE").status_code == 404

    renamed = client.patch(
        f"/api/collaborations/{collaboration['id']}",
        json={"title": "后端调研与风险复核"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "后端调研与风险复核"
    assert client.get(
        f"/api/collaborations/{collaboration['id']}"
    ).json()["collaboration"]["title"] == "后端调研与风险复核"
    assert client.patch(
        f"/api/collaborations/{collaboration['id']}",
        json={"title": " "},
    ).status_code == 400
    assert client.patch(
        "/api/collaborations/COL-NOPE", json={"title": "不存在"}
    ).status_code == 404


def test_collaboration_detail_phase_matches_task_acceptance_state(client):
    from common.models import TaskStatus
    from orchestrator import collaboration_store, state_store
    from state.db import connect

    conn = connect()
    task = conn.execute(
        "SELECT id, collaboration_id FROM tasks WHERE id = 'T-2';"
    ).fetchone()
    for status in (
        TaskStatus.ASSIGNED,
        TaskStatus.WORKING,
        TaskStatus.AWAITING_ACCEPTANCE,
    ):
        state_store.transition_task(conn, task["id"], status)
    collaboration_store.sync_phase_from_tasks(
        conn, task["collaboration_id"])
    conn.close()

    detail = client.get(
        f"/api/collaborations/{task['collaboration_id']}").json()
    assert detail["collaboration"]["phase"] == "awaiting_acceptance"
    assert detail["tasks"][0]["status"] == "awaiting_acceptance"


def test_collaboration_message_does_not_require_active_task(client):
    from orchestrator import supervision_store
    from state.db import connect

    conn = connect()
    collaboration_id = conn.execute(
        "SELECT id FROM collaborations ORDER BY created_at LIMIT 1;"
    ).fetchone()["id"]
    conn.execute(
        "UPDATE tasks SET status = 'completed' WHERE collaboration_id = ?;",
        (collaboration_id,),
    )
    conn.commit()
    conn.close()

    response = client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": "@hermes 任务结束后继续询问",
              "recipient_id": "hermes", "idempotency_key": "continue-1"},
    )
    assert response.status_code == 200
    assert response.json()["context_revision"] == 1
    assert response.json()["recipient_id"] == "hermes"
    assert response.json()["delivery_status"] == "queued"
    replay = client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": "@hermes 任务结束后继续询问",
              "recipient_id": "hermes", "idempotency_key": "continue-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["message_id"] == response.json()["message_id"]
    detail = client.get(
        f"/api/collaborations/{collaboration_id}"
    ).json()
    assert detail["messages"][-1]["message_type"] == "user.comment"
    assert detail["messages"][-1]["delivery_status"] == "queued"
    assert "任务结束后继续询问" in detail["messages"][-1]["content_json"]
    assert detail["collaboration"]["controller"] == "hermes"

    conn = connect()
    watch = conn.execute(
        "SELECT id, peer FROM supervision_watches"
        " WHERE id = (SELECT watch_id FROM supervision_conversation_routes"
        " WHERE collaboration_id = ?)"
        " ORDER BY created_at DESC LIMIT 1;",
        (collaboration_id,),
    ).fetchone()
    assert watch is not None
    notifications = supervision_store.pull_notifications(
        conn, peer=watch["peer"], watch_ids=[watch["id"]]
    )
    conn.close()
    assert any(
        item.get("message_id") == response.json()["message_id"]
        for item in notifications
    )
    detail = client.get(f"/api/collaborations/{collaboration_id}").json()
    assert detail["messages"][-1]["delivery_status"] == "processing"

    assert client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": "错误路由", "recipient_id": "codex"},
    ).status_code == 400
    assert client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": " "},
    ).status_code == 400
    assert client.post(
        "/api/collaborations/COL-NOPE/messages",
        json={"text": "hello"},
    ).status_code == 404


def test_collaboration_message_fails_closed_without_hermes_route(client):
    from orchestrator import collaboration_store
    from state.db import connect

    conn = connect()
    conversation_id = collaboration_store.create_conversation(
        conn, created_by="user")
    collaboration_id = collaboration_store.create_collaboration(
        conn, conversation_id=conversation_id, objective="local only")
    conn.close()

    response = client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": "这条消息不能假装已发送"},
    )
    assert response.status_code == 409
    detail = client.get(
        f"/api/collaborations/{collaboration_id}").json()
    assert detail["collaboration"]["context_revision"] == 1
    assert detail["messages"] == []


def test_collaboration_detail_synthesizes_legacy_agent_result(client):
    from orchestrator import collaboration_store
    from state.db import connect

    conn = connect()
    row = conn.execute(
        "SELECT id, collaboration_id FROM tasks WHERE id = 'T-2';"
    ).fetchone()
    conn.execute(
        "UPDATE tasks SET status = 'completed', result_summary = ?,"
        " assigned_to = 'codex' WHERE id = 'T-2';",
        ("Agent 的历史最终输出",),
    )
    conn.commit()
    conn.close()

    detail = client.get(
        f"/api/collaborations/{row['collaboration_id']}"
    ).json()
    result = next(message for message in detail["messages"]
                  if message["task_id"] == row["id"])
    assert result["sender_type"] == "agent"
    assert result["sender_id"] == "codex"
    assert result["message_type"] == "agent.task.result.legacy"
    assert "Agent 的历史最终输出" in result["content_json"]

    conn = connect()
    collaboration = collaboration_store.get_collaboration(
        conn, row["collaboration_id"])
    collaboration_store.append_message(
        conn,
        conversation_id=collaboration["conversation_id"],
        collaboration_id=row["collaboration_id"],
        task_id=row["id"],
        agent_id="codex",
        sender_type="agent",
        sender_id="codex",
        message_type="agent.task.result",
        content={"text": "Agent 的历史最终输出"},
        based_on_revision=collaboration["context_revision"],
    )
    conn.close()
    messages = client.get(
        f"/api/collaborations/{row['collaboration_id']}"
    ).json()["messages"]
    results = [message for message in messages
               if message["task_id"] == row["id"]]
    assert len(results) == 1
    assert results[0]["message_type"] == "agent.task.result"


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
    collaboration_id = secure_client.get(
        "/api/collaborations"
    ).json()["collaborations"][0]["id"]
    assert secure_client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": "viewer cannot write"},
        headers={"X-CSRF-Token": viewer_csrf}).status_code == 403

    operator, operator_csrf = _login(
        secure_client, "operator-token-012345")
    assert operator.status_code == 200
    intervention = secure_client.post(
        "/api/tasks/T-1/interventions",
        json={"mode": "comment", "content": {"text": "note"}},
        headers={"X-CSRF-Token": operator_csrf})
    assert intervention.status_code == 200
    message = secure_client.post(
        f"/api/collaborations/{collaboration_id}/messages",
        json={"text": "operator follow-up"},
        headers={"X-CSRF-Token": operator_csrf})
    assert message.status_code == 200
    assert secure_client.post(
        "/api/grants", json={"pattern": "restart"},
        headers={"X-CSRF-Token": operator_csrf}).status_code == 403
    assert secure_client.post(
        f"/api/alerts/{alert_id}/acknowledge", json={"note": "owned"},
        headers={"X-CSRF-Token": operator_csrf}).status_code == 200
    assert secure_client.patch(
        "/api/agents/kimi", json={"enabled": True},
        headers={"X-CSRF-Token": operator_csrf}).status_code == 403

    admin, admin_csrf = _login(secure_client, "admin-token-0123456789")
    assert admin.status_code == 200
    assert secure_client.patch(
        "/api/agents/kimi", json={"enabled": True},
        headers={"X-CSRF-Token": admin_csrf}).status_code == 200


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
