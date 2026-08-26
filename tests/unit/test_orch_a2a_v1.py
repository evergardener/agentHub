"""Hermes → 单一 agentHub peer → Registry 动态委派契约。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes.policy import ApprovalPolicy
from orchestrator import agent_control_store, collaboration_store, state_store
from orchestrator.a2a_server import create_app
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio

HUB_TOKEN = "test-agenthub-peer-token-0123456789"
OTHER_HUB_TOKEN = "other-agenthub-peer-token-0123456789"
LEGACY_TOKEN = "test-legacy-token-0123456789"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LAS_API_TOKEN", LEGACY_TOKEN)
    monkeypatch.setenv("LAS_A2A_PEERS", json.dumps({
        HUB_TOKEN: {"peer": "qishuo"},
        OTHER_HUB_TOKEN: {"peer": "other"},
    }))
    monkeypatch.delenv("LAS_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("LAS_ORCH_REQUIRE_AUTH", raising=False)
    tm = TaskManager(db_path=tmp_path / "state.db",
                     workspace=tmp_path / "ws")
    from orchestrator import agent_profile_store
    agent_profile_store.seed_catalog(tm.conn)
    state_store.update_heartbeat(tm.conn, "codex",
                                 endpoint="http://worker:8201")
    state_store.update_heartbeat(tm.conn, "kimi",
                                 endpoint="http://worker:8202")
    assert agent_profile_store.assign_seed_profile(tm.conn, "codex")
    assert agent_profile_store.assign_seed_profile(tm.conn, "kimi")
    delegated: list[tuple[str, str]] = []

    async def fake_delegate(self, task_id, endpoint, agent_id, attempt=1):
        state_store.transition_task(self.conn, task_id,
                                    state_store.TaskStatus.ASSIGNED)
        delegated.append((task_id, agent_id))

    monkeypatch.setattr(TaskManager, "delegate_task", fake_delegate)
    client = TestClient(create_app(tm=tm, policy=ApprovalPolicy()))
    return tm, client, delegated


def _bearer(token: str = HUB_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _control(action: str, *, context_id: str | None = "ctx-live",
             **fields) -> dict:
    text = json.dumps({"agenthub": "v1", "action": action, **fields},
                      ensure_ascii=False)
    message = {
        "role": "user",
        "parts": [{"text": text, "mediaType": "application/json"}],
    }
    if context_id is not None:
        message["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": "1", "method": "SendMessage",
            "params": {"message": message}}


def _legacy(text: str, **metadata) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": metadata}}}


def test_registry_discovery_uses_single_peer(env):
    tm, client, delegated = env
    state_store.update_heartbeat(tm.conn, "fake",
                                 endpoint="http://worker:8298")
    tm.conn.execute(
        "UPDATE agents SET lease_expires_at = '2000-01-01T00:00:00+08:00'"
        " WHERE id = 'fake';")
    tm.conn.commit()
    response = client.post("/a2a", json=_control("agents/list"),
                           headers=_bearer()).json()
    message = response["result"]["message"]
    payload = json.loads(message["parts"][0]["text"])
    assert {item["id"] for item in payload["agents"]} >= {"codex", "kimi"}
    codex = next(item for item in payload["agents"]
                 if item["id"] == "codex")
    assert "gpt-5.6-luna" in codex["runtime_policy"]["allowed_models"]
    assert "max" in codex["runtime_policy"][
        "allowed_reasoning_efforts"]
    assert "fake" not in {item["id"] for item in payload["agents"]}
    assert message["contextId"] == "ctx-live"
    assert not delegated


def test_dynamic_worker_can_be_added_without_peer_config(env):
    tm, client, delegated = env
    state_store.update_heartbeat(tm.conn, "pi",
                                 endpoint="http://worker:8299")
    response = client.post(
        "/a2a", json=_control("tasks/create", agent="pi",
                              objective="查询运行状态"),
        headers=_bearer()).json()
    task = response["result"]["task"]
    assert delegated == [(task["id"], "pi")]


def test_disabled_agent_requires_confirmation_and_is_not_delegated(env):
    tm, client, delegated = env
    agent_control_store.set_enabled(
        tm.conn, agent_id="kimi", enabled=False, updated_by="test")
    response = client.post(
        "/a2a", json=_control("tasks/create", agent="kimi",
                              objective="查询状态"),
        headers=_bearer()).json()
    assert response["error"]["code"] == -32602
    assert "disabled" in response["error"]["message"]
    assert "询问用户" in response["error"]["message"]
    assert not delegated
    assert tm.conn.execute(
        "SELECT COUNT(*) FROM collaborations;").fetchone()[0] == 0


def test_task_response_is_native_hermes_readable(env):
    tm, client, delegated = env
    response = client.post(
        "/a2a", json=_control("tasks/create", agent="codex",
                              objective="查询当前任务列表"),
        headers=_bearer()).json()
    task = response["result"]["task"]
    assert task["status"]["state"] == "submitted"
    assert task["status"]["message"]["role"] == "agent"
    text = task["status"]["message"]["parts"][0]["text"]
    assert f"task_id={task['id']}" in text
    assert task["contextId"] == "ctx-live"
    row = state_store.get_task(tm.conn, task["id"])
    assert row["collaboration_id"]
    collaboration = collaboration_store.get_collaboration(
        tm.conn, row["collaboration_id"])
    messages = collaboration_store.list_collaboration_messages(
        tm.conn, row["collaboration_id"])
    assert collaboration["conversation_id"]
    assert len(messages) == 1
    assert messages[0]["task_id"] == task["id"]
    assert messages[0]["sender_type"] == "hermes"
    assert messages[0]["sender_id"] == "qishuo"
    assert messages[0]["recipient_id"] == "codex"
    assert json.loads(messages[0]["content_json"])["text"] == \
        "查询当前任务列表"
    bound = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.a2a_context.bound';",
        (task["id"],),
    ).fetchone()
    assert json.loads(bound["payload_json"])["context_id"] == "ctx-live"


def test_tasks_create_persists_explicit_execution_workspace(env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询项目状态",
            workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    row = state_store.get_task(tm.conn, task["id"])
    context = json.loads(row["plan_context_json"])
    assert context["execution_workspace"] == str(execution_workspace.resolve())
    assert task["metadata"]["execution_workspace"] == \
        str(execution_workspace.resolve())
    messages = collaboration_store.list_collaboration_messages(
        tm.conn, row["collaboration_id"])
    assert json.loads(messages[0]["content_json"])["workspace"] == \
        str(execution_workspace.resolve())
    assert delegated == [(task["id"], "codex")]


def test_tasks_create_rejects_relative_execution_workspace(env):
    tm, client, delegated = env
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询项目状态",
            workspace="relative/project"),
        headers=_bearer()).json()

    assert response["error"]["code"] == -32602
    assert "absolute" in response["error"]["message"]
    assert delegated == []
    assert tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0


def test_tasks_create_rejects_prose_only_absolute_repository_path(env):
    tm, client, delegated = env
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex",
            objective="修改 /Users/example/project/Dockerfile 并运行测试"),
        headers=_bearer()).json()

    assert response["error"]["code"] == -32602
    assert "缺少结构化 workspace" in response["error"]["message"]
    assert delegated == []
    assert tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0


def test_tasks_create_persists_structured_display_copy(env, tmp_path):
    tm, client, _ = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex",
            objective="很长的完整下发指令，包含全部约束和事实证据",
            title="修复 GitHub 流水线构建失败问题",
            summary="更新 Debian Trxie 安全补丁并保持 Trivy 门禁。",
            workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    row = state_store.get_task(tm.conn, task["id"])
    context = json.loads(row["plan_context_json"])
    assert context["display_title"] == "修复 GitHub 流水线构建失败问题"
    assert context["objective_summary"].startswith("更新 Debian")
    assert task["metadata"]["display_title"] == context["display_title"]
    message = collaboration_store.list_collaboration_messages(
        tm.conn, row["collaboration_id"])[0]
    payload = json.loads(message["content_json"])
    assert payload["title"] == context["display_title"]
    assert payload["summary"] == context["objective_summary"]


def test_tasks_create_persists_profile_allowed_runtime_config(env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询运行状态",
            model="gpt-5.6-luna", reasoning_effort="max",
            workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    row = state_store.get_task(tm.conn, task["id"])
    context = json.loads(row["plan_context_json"])
    assert context["runtime_config"] == {
        "model": "gpt-5.6-luna", "reasoning_effort": "max"}
    assert task["metadata"]["runtime_config"] == context["runtime_config"]
    message = collaboration_store.list_collaboration_messages(
        tm.conn, row["collaboration_id"])[0]
    assert json.loads(message["content_json"])["runtime_config"] == \
        context["runtime_config"]
    audit = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.runtime_config.selected';",
        (task["id"],),
    ).fetchone()
    assert json.loads(audit["payload_json"])["runtime_config"] == \
        context["runtime_config"]
    assert delegated == [(task["id"], "codex")]


@pytest.mark.parametrize("fields, expected", [
    ({"model": "unlisted-model"}, "allowed_models"),
    ({"reasoning_effort": "extreme"}, "allowed_reasoning_efforts"),
])
def test_tasks_create_rejects_runtime_config_outside_profile(
        env, fields, expected):
    tm, client, delegated = env
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询状态", **fields),
        headers=_bearer()).json()

    assert response["error"]["code"] == -32602
    assert expected in response["error"]["message"]
    assert delegated == []
    assert tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0


def test_tasks_create_rejects_runtime_override_for_unsupported_adapter(env):
    tm, client, delegated = env
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="kimi", objective="查询状态",
            model="gpt-5.6-luna"),
        headers=_bearer()).json()

    assert response["error"]["code"] == -32602
    assert "does not support task runtime overrides" in \
        response["error"]["message"]
    assert delegated == []
    assert tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0


def test_hub_can_respond_to_hermes_routed_native_interaction(
        env, monkeypatch):
    tm, client, _ = env
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id="ctx-live", objective="修复",
    )["collaboration_id"]
    task_id = tm.create_task("修复问题", collaboration_id=collaboration_id)
    for status in (
        state_store.TaskStatus.ASSIGNED,
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.BLOCKED,
    ):
        state_store.transition_task(tm.conn, task_id, status)
    binding = collaboration_store.bind_agent_session(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", adapter_session_id="S-codex",
        native_session_id="native-codex", resume_capability="native")
    interaction = collaboration_store.upsert_session_interaction(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], agent_id="codex",
        interaction={
            "interactionId": "codex:respond-1", "kind": "approval",
            "nativeRequestId": "rpc-1",
            "payload": {
                "toolName": "edit", "inspectable": True,
                "reason": "更新 Dockerfile",
                "toolView": {"kind": "edit", "paths": ["/repo/Dockerfile"]},
            },
        })
    intent = collaboration_store.create_action_intent(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], requested_by_agent_id="codex",
        operation="filesystem.write",
        targets={"paths": ["/repo/Dockerfile"]},
        purpose="更新 Dockerfile", expected_effects={"toolName": "edit"},
        rollback_plan="git restore Dockerfile", based_on_revision=1)
    tm.conn.execute(
        "UPDATE action_intents SET status = 'awaiting_hermes',"
        " policy_route = 'hermes' WHERE id = ?;", (intent["id"],))
    tm.conn.commit()
    collaboration_store.attach_action_intent(
        tm.conn, interaction["id"], intent["id"])
    captured = {}

    async def fake_respond(self, interaction_id, *, response,
                           requested_by, endpoint=None):
        captured.update({
            "interaction_id": interaction_id,
            "response": response,
            "requested_by": requested_by,
        })
        return {"status": {"state": "working"}}

    monkeypatch.setattr(TaskManager, "respond_agent_interaction", fake_respond)
    response = client.post(
        "/a2a", json=_control(
            "interactions/respond", interaction_id=interaction["id"],
            outcome="allowed-once", note="已核对可回滚变更"),
        headers=_bearer()).json()

    payload = json.loads(response["result"]["message"]["parts"][0]["text"])
    assert payload["status"] == "responded"
    assert captured == {
        "interaction_id": interaction["id"],
        "response": {"outcome": "allowed-once", "note": "已核对可回滚变更"},
        "requested_by": "hermes",
    }


def test_tasks_get_exposes_safe_pending_native_interaction(env):
    tm, client, _ = env
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id="ctx-live", objective="修复",
    )["collaboration_id"]
    task_id = tm.create_task(
        "修复问题", collaboration_id=collaboration_id)
    for status in (
        state_store.TaskStatus.ASSIGNED,
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.BLOCKED,
    ):
        state_store.transition_task(tm.conn, task_id, status)
    binding = collaboration_store.bind_agent_session(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", adapter_session_id="S-codex",
        native_session_id="native-codex", resume_capability="native")
    interaction = collaboration_store.upsert_session_interaction(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], agent_id="codex",
        interaction={
            "interactionId": "codex:edit-1", "kind": "approval",
            "nativeRequestId": "rpc-1",
            "payload": {
                "toolName": "edit", "inspectable": True,
                "reason": "更新 Dockerfile",
                "toolView": {"kind": "edit", "paths": ["/repo/Dockerfile"]},
            },
        })
    intent = collaboration_store.create_action_intent(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], requested_by_agent_id="codex",
        operation="filesystem.write",
        targets={"paths": ["/repo/Dockerfile"]},
        purpose="更新 Dockerfile", expected_effects={"toolName": "edit"},
        rollback_plan="git restore Dockerfile", based_on_revision=1)
    tm.conn.execute(
        "UPDATE action_intents SET status = 'awaiting_hermes',"
        " policy_route = 'hermes' WHERE id = ?;", (intent["id"],))
    tm.conn.commit()
    collaboration_store.attach_action_intent(
        tm.conn, interaction["id"], intent["id"])

    task = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    pending = task["metadata"]["pending_interactions"]
    assert pending == [{
        "interaction_id": interaction["id"],
        "task_id": task_id,
        "agent_id": "codex", "kind": "approval", "status": "pending",
        "inspectable": True, "tool_name": "edit",
        "reason": "更新 Dockerfile",
        "tool_view": {"kind": "edit", "paths": ["/repo/Dockerfile"]},
        "action_intent_id": intent["id"],
        "operation": "filesystem.write", "risk": "unknown",
        "policy_route": "hermes",
        "action_intent_status": "awaiting_hermes",
        "policy_reason": None,
        "targets": {"paths": ["/repo/Dockerfile"]},
        "command": None, "args": [], "cwd": None, "workspace": None,
        "rollback": "git restore Dockerfile",
        "rollback_plan": "git restore Dockerfile",
        "allowed_responses": ["allowed-once", "rejected"],
        "awaiting": "awaiting_hermes",
        "awaiting_hermes": True,
        "awaiting_user": False,
    }]
    status_text = task["status"]["message"]["parts"][0]["text"]
    assert "pending_interactions=" in status_text
    assert interaction["id"] in status_text
    assert "awaiting_hermes" in status_text
    tm.conn.execute(
        "UPDATE action_intents SET status = 'awaiting_user',"
        " policy_route = 'user' WHERE id = ?;", (intent["id"],))
    tm.conn.commit()
    denied = client.post(
        "/a2a", json=_control(
            "interactions/respond", interaction_id=interaction["id"],
            outcome="allowed-once"),
        headers=_bearer()).json()
    assert denied["error"]["code"] == -32003
    assert "requires user approval" in denied["error"]["message"]


def _command_read_interaction(tm, *, command="docker", args=None,
                              context_id="ctx-live"):
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id=context_id, objective="诊断",
    )["collaboration_id"]
    task_id = tm.create_task("只读诊断", collaboration_id=collaboration_id)
    for status in (
        state_store.TaskStatus.ASSIGNED,
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.BLOCKED,
    ):
        state_store.transition_task(tm.conn, task_id, status)
    binding = collaboration_store.bind_agent_session(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", adapter_session_id="S-codex",
        native_session_id="native-codex", resume_capability="native")
    args = ["ps"] if args is None else args
    interaction = collaboration_store.upsert_session_interaction(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], agent_id="codex",
        interaction={
            "interactionId": "codex:docker-read-1", "kind": "approval",
            "nativeRequestId": "rpc-docker-read",
            "payload": {
                "toolName": "shell", "inspectable": True,
                "reason": "读取 Docker 状态",
                "toolView": {
                    "kind": "shell", "command": "docker ps",
                    "cwd": "/repo",
                },
            },
        })
    intent = collaboration_store.create_action_intent(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], requested_by_agent_id="codex",
        operation="command.read",
        targets={
            "workspace": "/repo", "paths": ["/repo"], "cwd": "/repo",
            "command": command, "args": args,
        },
        purpose="读取 Docker 状态", expected_effects={"read": True},
        rollback_plan=None, based_on_revision=1)
    tm.conn.execute(
        "UPDATE action_intents SET status = 'awaiting_hermes',"
        " policy_route = 'hermes', risk = 'read' WHERE id = ?;",
        (intent["id"],))
    tm.conn.commit()
    collaboration_store.attach_action_intent(
        tm.conn, interaction["id"], intent["id"])
    return task_id, interaction, intent


def test_command_read_details_are_context_scoped_and_not_delegation_approval(
        env):
    tm, client, _ = env
    task_id, interaction, _ = _command_read_interaction(tm)

    task = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    pending = task["metadata"]["pending_interactions"][0]
    assert pending["inspectable"] is True
    assert pending["command"] == "docker"
    assert pending["args"] == ["ps"]
    assert pending["cwd"] == "/repo"
    assert pending["workspace"] == "/repo"
    assert pending["rollback_plan"] is None
    assert pending["allowed_responses"] == ["allowed-once", "rejected"]
    status_text = task["status"]["message"]["parts"][0]["text"]
    assert '"command":"docker"' in status_text
    assert '"args":["ps"]' in status_text
    assert '"allowed_responses":["allowed-once","rejected"]' in status_text

    detail_response = client.post(
        "/a2a", json=_control(
            "interactions/get", interaction_id=interaction["id"]),
        headers=_bearer()).json()
    detail = json.loads(
        detail_response["result"]["message"]["parts"][0]["text"]
    )["interaction"]
    assert detail["interaction_id"] == interaction["id"]
    assert detail["command"] == "docker"
    assert detail["args"] == ["ps"]
    assert detail["awaiting_hermes"] is True

    direct = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0", "id": "direct-get",
            "method": "interactions/get",
            "params": {"id": interaction["id"], "contextId": "ctx-live"},
        },
        headers=_bearer(),
    ).json()
    assert direct["result"]["interaction"]["interaction_id"] == \
        interaction["id"]
    missing_direct_context = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0", "id": "direct-get-missing-context",
            "method": "interactions/get",
            "params": {"id": interaction["id"]},
        },
        headers=_bearer(),
    ).json()
    assert missing_direct_context["error"]["code"] == -32602

    wrong_context = client.post(
        "/a2a", json=_control(
            "interactions/get", context_id="ctx-other",
            interaction_id=interaction["id"]),
        headers=_bearer()).json()
    assert wrong_context["error"]["code"] == -32003

    wrong_peer = client.post(
        "/a2a", json=_control(
            "interactions/get", interaction_id=interaction["id"]),
        headers=_bearer(OTHER_HUB_TOKEN)).json()
    assert wrong_peer["error"]["code"] == -32003

    # tasks/approve is only the delegation gate.  It cannot replace the
    # native interaction's allowed-once receipt.
    delegation_approval = client.post(
        "/a2a", json=_control("tasks/approve", task_id=task_id),
        headers=_bearer()).json()
    assert delegation_approval["error"]["code"] == -32602
    assert "不在待批准状态" in delegation_approval["error"]["message"]


def test_command_read_sensitive_or_unbounded_details_fail_closed(env):
    tm, client, _ = env
    task_id, interaction, _ = _command_read_interaction(
        tm, args=["--format", "token=not-for-display"])
    detail = client.post(
        "/a2a", json=_control(
            "interactions/get", interaction_id=interaction["id"]),
        headers=_bearer()).json()
    view = json.loads(
        detail["result"]["message"]["parts"][0]["text"]
    )["interaction"]
    assert view["inspectable"] is False
    assert view["args"] == []

    _, long_interaction, _ = _command_read_interaction(
        tm, args=["x" * 2049])
    long_detail = client.post(
        "/a2a", json=_control(
            "interactions/get", interaction_id=long_interaction["id"]),
        headers=_bearer()).json()
    long_view = json.loads(
        long_detail["result"]["message"]["parts"][0]["text"]
    )["interaction"]
    assert long_view["inspectable"] is False
    assert long_view["args"] == []


def test_same_peer_context_reuses_collaboration_for_multiple_tasks(env):
    tm, client, delegated = env
    first = client.post(
        "/a2a", json=_control("tasks/create", agent="codex",
                              objective="查询第一项状态"),
        headers=_bearer()).json()["result"]["task"]
    second = client.post(
        "/a2a", json=_control("tasks/create", agent="codex",
                              objective="查询第二项状态"),
        headers=_bearer()).json()["result"]["task"]

    rows = tm.conn.execute(
        "SELECT id, collaboration_id FROM tasks WHERE id IN (?,?)"
        " ORDER BY id;", (first["id"], second["id"]),
    ).fetchall()
    assert rows[0]["collaboration_id"] == rows[1]["collaboration_id"]
    assert tm.conn.execute(
        "SELECT COUNT(*) FROM conversations;").fetchone()[0] == 1
    assert tm.conn.execute(
        "SELECT COUNT(*) FROM collaborations;").fetchone()[0] == 1
    assert tm.conn.execute(
        "SELECT COUNT(*) FROM conversation_messages;").fetchone()[0] == 2
    assert delegated == [(first["id"], "codex"), (second["id"], "codex")]


def test_missing_context_id_is_generated_returned_and_persisted(env):
    tm, client, _ = env
    task = client.post(
        "/a2a", json=_control(
            "tasks/create", context_id=None, agent="codex",
            objective="查询无上下文请求"),
        headers=_bearer()).json()["result"]["task"]

    assert task["contextId"].startswith("ctx-agenthub-")
    row = state_store.get_task(tm.conn, task["id"])
    assert row["collaboration_id"]
    event = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.a2a_context.bound';", (task["id"],),
    ).fetchone()
    assert json.loads(event["payload_json"])["context_id"] == task["contextId"]


def test_plain_text_and_worker_bound_envelope_are_rejected(env):
    _, client, delegated = env
    plain = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage",
             "params": {"message": {"role": "user",
                         "parts": [{"text": "请直接调用 dsh"}]}}}
    assert client.post("/a2a", json=plain,
                       headers=_bearer()).json()["error"]["code"] == -32602
    assert not delegated


def test_approval_and_get_work_through_same_peer(env):
    _, client, delegated = env
    created = client.post(
        "/a2a", json=_control("tasks/create", agent="codex",
                              objective="在工作区创建 x.md"),
        headers=_bearer()).json()["result"]["task"]
    assert created["status"]["state"] == "input-required"
    text = created["status"]["message"]["parts"][0]["text"]
    assert f"task_id={created['id']}" in text
    assert not delegated

    approved = client.post(
        "/a2a", json=_control("tasks/approve", task_id=created["id"]),
        headers=_bearer()).json()["result"]["task"]
    assert approved["status"]["state"] == "submitted"
    assert delegated == [(created["id"], "codex")]

    fetched = client.post(
        "/a2a", json=_control("tasks/get", task_id=created["id"]),
        headers=_bearer()).json()["result"]["task"]
    assert fetched["id"] == created["id"]


def test_acceptance_is_distinct_input_required_and_explicit_user_action(env):
    tm, client, _ = env
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id="ctx-live", objective="验收",
    )["collaboration_id"]
    task_id = tm.create_task("完成后等待用户验收",
                             collaboration_id=collaboration_id)
    for status in (
        state_store.TaskStatus.ASSIGNED,
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.AWAITING_ACCEPTANCE,
    ):
        state_store.transition_task(tm.conn, task_id, status)

    pending = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    assert pending["status"]["state"] == "input-required"
    assert pending["metadata"]["input_required_kind"] == "acceptance"

    accepted = client.post(
        "/a2a", json=_control("tasks/accept", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    assert accepted["status"]["state"] == "completed"
    assert accepted["metadata"]["internal_status"] == "accepted"


def test_a2a_rework_requires_feedback_and_does_not_reopen_completed(env):
    tm, client, _ = env
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id="ctx-live", objective="返工",
    )["collaboration_id"]
    task_id = tm.create_task("等待验收后返工",
                             collaboration_id=collaboration_id)
    for status in (
        state_store.TaskStatus.ASSIGNED,
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.AWAITING_ACCEPTANCE,
    ):
        state_store.transition_task(tm.conn, task_id, status)

    missing = client.post(
        "/a2a", json=_control(
            "tasks/request-rework", task_id=task_id, feedback="  "),
        headers=_bearer()).json()
    assert missing["error"]["code"] == -32602
    assert "feedback" in missing["error"]["message"]

    rework = client.post(
        "/a2a", json=_control(
            "tasks/request-rework", task_id=task_id,
            feedback="测试尚未覆盖恢复路径"),
        headers=_bearer()).json()["result"]["task"]
    assert rework["metadata"]["internal_status"] == "rework_pending"
    assert rework["status"]["state"] == "working"


def test_auth_and_card_contract(env):
    _, client, _ = env
    assert client.post("/a2a", json=_control("agents/list")).status_code == 401
    assert client.post("/a2a", json=_control("agents/list"),
                       headers=_bearer("wrong")).status_code == 401
    card = client.get("/.well-known/agent-card.json",
                      headers=_bearer()).json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert {item["id"] for item in card["skills"]} >= {
        "orchestrate", "registry-discovery", "approval-gate",
        "durable-supervision"}


def test_supervision_register_pull_and_ack_are_peer_scoped(env):
    tm, client, _ = env
    created = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询当前状态"),
        headers=_bearer()).json()["result"]["task"]

    registered = client.post(
        "/a2a", json=_control(
            "supervision/register", task_id=created["id"]),
        headers=_bearer()).json()["result"]["message"]
    watch = json.loads(registered["parts"][0]["text"])
    assert watch["status"] == "active"
    assert watch["task_id"] == created["id"]
    assert watch["context_id"] == "ctx-live"

    state_store.transition_task(
        tm.conn, created["id"], state_store.TaskStatus.WORKING)
    state_store.transition_task(
        tm.conn, created["id"], state_store.TaskStatus.AWAITING_ACCEPTANCE)

    pulled = client.post(
        "/a2a", json=_control(
            "supervision/pull", watch_ids=[watch["watch_id"]]),
        headers=_bearer()).json()["result"]["message"]
    notifications = json.loads(pulled["parts"][0]["text"])["notifications"]
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification["watch_id"] == watch["watch_id"]
    assert notification["task_id"] == created["id"]
    assert notification["event_type"] == "task.awaiting_acceptance"
    assert set(notification) == {
        "notification_id", "watch_id", "task_id", "context_id",
        "event_type", "internal_status", "created_at",
    }

    acked = client.post(
        "/a2a", json=_control(
            "supervision/ack",
            notification_id=notification["notification_id"]),
        headers=_bearer()).json()["result"]["message"]
    assert json.loads(acked["parts"][0]["text"])["status"] == "acknowledged"
    acked_again = client.post(
        "/a2a", json=_control(
            "supervision/ack",
            notification_id=notification["notification_id"]),
        headers=_bearer()).json()["result"]["message"]
    assert json.loads(acked_again["parts"][0]["text"])["status"] == \
        "acknowledged"


def test_supervision_rejects_context_or_watch_not_owned_by_peer(env):
    _, client, _ = env
    created = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询当前状态"),
        headers=_bearer()).json()["result"]["task"]

    wrong_context = client.post(
        "/a2a", json=_control(
            "supervision/register", context_id="ctx-other",
            task_id=created["id"]), headers=_bearer()).json()
    assert wrong_context["error"]["code"] == -32003

    response = client.post(
        "/a2a", json=_control(
            "supervision/pull", watch_ids=["WATCH-does-not-belong"]),
        headers=_bearer(OTHER_HUB_TOKEN)).json()
    assert response["error"]["code"] in {-32003, -32602}


def test_worker_proxy_treats_adapter_token_as_downstream_credential(
        env, monkeypatch):
    tm, _, _ = env
    gateway_token = "test-gateway-token-0123456789"
    monkeypatch.setenv("LAS_GATEWAY_API_KEY", gateway_token)
    client = TestClient(create_app(tm=tm, policy=ApprovalPolicy()))
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "X-Agent-Token": "different-downstream-adapter-token",
    }
    # Authentication reaches registry resolution instead of rejecting the
    # intentionally different downstream adapter credential as a conflict.
    response = client.get("/worker-proxy/not-registered/health",
                          headers=headers)
    assert response.status_code == 503
    assert "unknown agent" in response.json()["error"]
    # The exception is narrowly scoped: normal control-plane routes continue
    # to reject conflicting identity headers.
    assert client.post("/a2a", json=_control("agents/list"),
                       headers=headers).status_code == 401


def test_legacy_metadata_agent_remains_compatible(env):
    tm, client, delegated = env
    result = client.post(
        "/a2a", json=_legacy("查询任务", agent="codex"),
        headers={"X-Agent-Token": LEGACY_TOKEN}).json()["result"]
    assert "task" not in result
    assert delegated == [(result["id"], "codex")]
    assert state_store.get_task(tm.conn, result["id"])["collaboration_id"] is None
