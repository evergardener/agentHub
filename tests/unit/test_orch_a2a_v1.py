"""Hermes → 单一 agentHub peer → Registry 动态委派契约。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes.policy import ApprovalPolicy
from orchestrator import agent_control_store, collaboration_store, state_store
from orchestrator.a2a_server import _message_interaction_summary, create_app
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


@pytest.mark.parametrize("context_id", [123, ["ctx"], {"id": "ctx"}, " "])
def test_hub_control_rejects_malformed_context_id(env, context_id):
    _, client, delegated = env
    body = _control("agents/list")
    body["params"]["message"]["contextId"] = context_id
    response = client.post(
        "/a2a", json=body, headers=_bearer()).json()
    assert response["error"]["code"] == -32602
    assert not delegated


def test_dynamic_worker_can_be_added_without_peer_config(env):
    tm, client, delegated = env
    state_store.update_heartbeat(tm.conn, "pi",
                                 endpoint="http://worker:8299")
    response = client.post(
        "/a2a", json=_control("tasks/create", agent="pi",
                                  objective="查询运行状态", access_mode="read"),
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
                                  objective="查询当前任务列表",
                                  access_mode="read"),
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
            access_mode="read", workspace=str(execution_workspace)),
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


def test_tasks_create_readonly_docker_status_dispatches_without_approval(
        env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    objective = (
        "只读获取当前 Docker 容器状态。核验 Docker 引擎可用性，列出容器名称、"
        "镜像、运行状态和健康状态，并标注停止、重启或不健康容器。"
        "不得修改配置、数据、容器或服务状态。输出简洁中文报告。"
    )
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective=objective,
            access_mode="read", workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    assert task["status"]["state"] == "submitted"
    assert task["metadata"]["internal_status"] == "assigned"
    assert task["metadata"]["access_mode"] == "read"
    assert delegated == [(task["id"], "codex")]
    approval = tm.conn.execute(
        "SELECT 1 FROM events WHERE task_id = ?"
        " AND event_type = 'task.approval_requested';", (task["id"],)
    ).fetchone()
    assert approval is None
    selected = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.access_mode.selected';", (task["id"],)
    ).fetchone()
    assert json.loads(selected["payload_json"]) == {
        "agent_id": "codex",
        "access_mode": "read",
        "source": "caller_semantic_declaration",
        "authority": "initial_dispatch_only",
    }
    classified = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.intent.classified';", (task["id"],)
    ).fetchone()
    assert json.loads(classified["payload_json"]) == {
        "risk": "read",
        "decision": "auto",
        "reason": "显式 read capability 且未发现写入意图，自动批准初始委派",
        "source": "structured_access_mode",
        "runtime_authority": False,
        "runtime_gate": "structured_action_intent",
    }


def test_tasks_create_explicit_read_dispatches_without_keyword_inference(
        env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex",
            objective="核验 TLS 证书有效期并返回结果",
            access_mode="read", workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    assert task["status"]["state"] == "submitted"
    assert task["metadata"]["internal_status"] == "assigned"
    assert task["metadata"]["access_mode"] == "read"
    assert delegated == [(task["id"], "codex")]


def test_tasks_create_readonly_legacy_inference_is_not_approval_authority(
        env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    objective = (
        "只读获取当前 Docker 容器状态。核验 Docker 引擎可用性，列出容器名称、"
        "镜像、运行状态和健康状态，并标注停止、重启或不健康容器。"
        "不得修改配置、数据、容器或服务状态。输出简洁中文报告。"
    )
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective=objective,
            workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    assert task["status"]["state"] == "input-required"
    assert task["metadata"].get("access_mode") is None
    assert delegated == []
    classified = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.intent.classified';", (task["id"],)
    ).fetchone()
    assert json.loads(classified["payload_json"])["source"] == \
        "legacy_keyword_compatibility"
    assert json.loads(classified["payload_json"])["decision"] == "ask"


def test_tasks_create_readonly_capability_does_not_bypass_restart(
        env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex",
            objective="只读检查后重启容器", access_mode="read",
            workspace=str(execution_workspace)),
        headers=_bearer()).json()

    task = response["result"]["task"]
    assert task["status"]["state"] == "input-required"
    assert task["metadata"]["internal_status"] == "queued"
    assert delegated == []
    approval = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.approval_requested';", (task["id"],)
    ).fetchone()
    assert json.loads(approval["payload_json"])["risk"] == "write"


def test_tasks_create_rejects_unknown_access_mode(env, tmp_path):
    tm, client, delegated = env
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    response = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="查询容器状态",
            access_mode="write", workspace=str(execution_workspace)),
        headers=_bearer()).json()

    assert response["error"]["code"] == -32602
    assert "access_mode" in response["error"]["message"]
    assert delegated == []
    assert tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0


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
            access_mode="read", workspace=str(execution_workspace)),
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
    assert task["metadata"]["input_required_kind"] == "blocked"
    assert task["metadata"]["state_detail"] == \
        "awaiting_hermes_interaction"
    assert task["metadata"]["required_action"] == "interaction_response"
    assert task["metadata"]["available_actions"] == [
        "interactions/respond"]
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
    assert "pending_interactions_total=1" in status_text
    assert "pending_interactions_truncated=false" in status_text
    assert task["metadata"]["pending_interactions_summary"] == {
        "total": 1,
        "included": 1,
        "truncated": False,
        "authoritative_field": "metadata.pending_interactions",
        "detail_action": "interactions/get",
    }
    assert interaction["id"] in status_text
    assert "awaiting_hermes" in status_text
    tm.conn.execute(
        "UPDATE action_intents SET status = 'awaiting_user',"
        " policy_route = 'user' WHERE id = ?;", (intent["id"],))
    tm.conn.commit()
    awaiting_user = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    assert awaiting_user["metadata"]["state_detail"] == \
        "awaiting_user_interaction"
    assert awaiting_user["metadata"]["required_action"] == \
        "user_interaction"
    assert awaiting_user["metadata"]["required_actor"] == "user"
    assert awaiting_user["metadata"]["available_actions"] == []
    assert awaiting_user["metadata"]["ui_actions"] == ["approve", "reject"]
    denied = client.post(
        "/a2a", json=_control(
            "interactions/respond", interaction_id=interaction["id"],
            outcome="allowed-once"),
        headers=_bearer()).json()
    assert denied["error"]["code"] == -32003
    assert "requires user approval" in denied["error"]["message"]


def test_pending_interaction_message_summary_is_bounded_and_explicit():
    summary, truncated = _message_interaction_summary([
        {
            "interaction_id": "INT-1", "inspectable": True,
            "command": "x" * 1000, "args": ["y" * 1000] * 20,
            "cwd": "/" + "c" * 1000,
            "workspace": "/" + "w" * 1000,
            "rollback_plan": "r" * 1000,
        },
        {"interaction_id": "INT-2"},
    ])

    assert truncated is True
    assert len(summary) == 1
    assert summary[0]["interaction_id"] == "INT-1"
    assert len(summary[0]["command"]) == 160
    assert len(summary[0]["args"]) == 4
    assert all(len(arg) == 96 for arg in summary[0]["args"])
    assert len(json.dumps(summary, ensure_ascii=False)) < 1800


def _command_read_interaction(tm, *, command="docker", args=None,
                              context_id="ctx-live", blocked=True):
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id=context_id, objective="诊断",
    )["collaboration_id"]
    task_id = tm.create_task("只读诊断", collaboration_id=collaboration_id)
    statuses = [state_store.TaskStatus.ASSIGNED, state_store.TaskStatus.WORKING]
    if blocked:
        statuses.append(state_store.TaskStatus.BLOCKED)
    for status in statuses:
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


def test_tasks_get_surfaces_interaction_before_blocked_status_event(env):
    tm, client, _ = env
    task_id, interaction, _ = _command_read_interaction(tm, blocked=False)

    task = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]

    assert state_store.get_task(tm.conn, task_id)["status"] == "working"
    assert task["status"]["state"] == "input-required"
    assert task["metadata"]["internal_status"] == "working"
    assert task["metadata"]["input_required_kind"] == "blocked"
    assert task["metadata"]["pending_interactions"][0][
        "interaction_id"] == interaction["id"]
    assert task["metadata"]["available_actions"] == [
        "interactions/respond"]


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
                               objective="查询第一项状态",
                               access_mode="read"),
        headers=_bearer()).json()["result"]["task"]
    second = client.post(
        "/a2a", json=_control("tasks/create", agent="codex",
                               objective="查询第二项状态",
                               access_mode="read"),
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
    assert created["metadata"]["input_required_kind"] == "delegation"
    assert created["metadata"]["state_detail"] == \
        "awaiting_delegation_approval"
    assert created["metadata"]["required_action"] == "hermes_approval"
    assert created["metadata"]["available_actions"] == [
        "tasks/approve", "tasks/reject"]
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
    state_store.add_artifact(
        tm.conn, task_id=task_id, agent_id="codex",
        name="last-message.md", path="/workspace/last-message.md",
        sha256="a" * 64)

    pending = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    assert pending["status"]["state"] == "input-required"
    assert pending["metadata"]["input_required_kind"] == "acceptance"
    assert pending["metadata"]["internal_status"] == "awaiting_acceptance"
    assert pending["metadata"]["state_detail"] == "awaiting_acceptance"
    assert pending["metadata"]["required_action"] == \
        "explicit_user_acceptance"
    assert pending["metadata"]["required_actor"] == "user"
    assert pending["metadata"]["available_actions"] == []
    assert pending["metadata"]["ui_actions"] == [
        "accept", "request-rework"]
    assert pending["artifacts"] == [{
        "name": "last-message.md", "type": "file",
        "path": "/workspace/last-message.md", "sha256": "a" * 64,
    }]
    status_message = pending["status"]["message"]["parts"][0]["text"]
    assert "等待用户验收，不是委派审批或运行时审批" in status_message
    assert "available_actions=none" in status_message

    denied = client.post(
        "/a2a", json=_control("tasks/accept", task_id=task_id),
        headers=_bearer()).json()
    assert denied["error"]["code"] == -32003
    assert "explicit user authority" in denied["error"]["message"]
    assert state_store.get_task(tm.conn, task_id)["status"] == \
        "awaiting_acceptance"

    tm.accept_result(task_id, decided_by="user", via="webui")
    accepted = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    assert accepted["status"]["state"] == "completed"
    assert accepted["metadata"]["internal_status"] == "accepted"


def test_legacy_completed_row_is_exposed_as_canonical_acceptance_gate(env):
    tm, client, _ = env
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id="ctx-live", objective="验收",
    )["collaboration_id"]
    task_id = tm.create_task("历史完成任务", collaboration_id=collaboration_id)
    for status in (
        state_store.TaskStatus.ASSIGNED,
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.COMPLETED,
    ):
        state_store.transition_task(tm.conn, task_id, status)

    task = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]

    assert task["status"]["state"] == "input-required"
    assert task["metadata"]["internal_status"] == "awaiting_acceptance"
    assert task["metadata"]["legacy_internal_status"] == "completed"
    assert task["metadata"]["input_required_kind"] == "acceptance"
    assert task["metadata"]["available_actions"] == []
    assert task["metadata"]["ui_actions"] == [
        "accept", "request-rework"]


def test_status_message_bounds_long_result_detail(env):
    tm, client, _ = env
    collaboration_id = collaboration_store.ensure_a2a_collaboration(
        tm.conn, peer="qishuo", context_id="ctx-live", objective="验收",
    )["collaboration_id"]
    task_id = tm.create_task("长结果", collaboration_id=collaboration_id)
    state_store.transition_task(
        tm.conn, task_id, state_store.TaskStatus.ASSIGNED)
    state_store.transition_task(
        tm.conn, task_id, state_store.TaskStatus.WORKING)
    state_store.transition_task(
        tm.conn, task_id, state_store.TaskStatus.AWAITING_ACCEPTANCE,
        result_summary="证" * 5000)

    task = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
        headers=_bearer()).json()["result"]["task"]
    message = task["status"]["message"]["parts"][0]["text"]

    assert task["metadata"]["status_detail_truncated"] is True
    assert "status_detail_truncated=true" in message
    assert len(message) < 1500


def test_a2a_rework_requires_explicit_webui_user_authority(env):
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
    assert missing["error"]["code"] == -32003
    assert "explicit user authority" in missing["error"]["message"]

    denied = client.post(
        "/a2a", json=_control(
            "tasks/request-rework", task_id=task_id,
            feedback="测试尚未覆盖恢复路径"),
        headers=_bearer()).json()
    assert denied["error"]["code"] == -32003
    assert state_store.get_task(tm.conn, task_id)["status"] == \
        "awaiting_acceptance"

    tm.reject_result(
        task_id, feedback="测试尚未覆盖恢复路径",
        decided_by="user", via="webui")
    rework = client.post(
        "/a2a", json=_control("tasks/get", task_id=task_id),
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
            "tasks/create", agent="codex", objective="查询当前状态",
            access_mode="read"),
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

    wrong_context = client.post(
        "/a2a", json=_control(
            "supervision/ack", context_id="ctx-other",
            notification_id=notification["notification_id"]),
        headers=_bearer()).json()
    assert wrong_context["error"]["code"] == -32003

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


def test_terminal_conversation_message_can_reply_or_create_followup(env):
    tm, client, delegated = env
    created = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="检查当前状态",
            access_mode="read"),
        headers=_bearer()).json()["result"]["task"]
    registered = client.post(
        "/a2a", json=_control(
            "supervision/register", task_id=created["id"]),
        headers=_bearer()).json()["result"]["message"]
    watch = json.loads(registered["parts"][0]["text"])
    for status in (
        state_store.TaskStatus.WORKING,
        state_store.TaskStatus.AWAITING_ACCEPTANCE,
        state_store.TaskStatus.ACCEPTED,
    ):
        state_store.transition_task(tm.conn, created["id"], status)

    mapping = collaboration_store.a2a_context_ids(
        peer="qishuo", context_id="ctx-live")
    user_message = collaboration_store.append_user_message_to_hermes(
        tm.conn,
        collaboration_id=mapping["collaboration_id"],
        user_id="user",
        content={"text": "解释一下结果；如果要执行请创建新任务"},
    )
    from orchestrator import supervision_store

    supervision_store.enqueue_user_message(
        tm.conn,
        collaboration_id=mapping["collaboration_id"],
        message_id=user_message["id"],
    )
    pulled = client.post(
        "/a2a", json=_control(
            "supervision/pull", watch_ids=[watch["watch_id"]]),
        headers=_bearer()).json()["result"]["message"]
    notifications = json.loads(pulled["parts"][0]["text"])["notifications"]
    notification = next(
        item for item in notifications
        if item["event_type"] == "conversation.user_message")
    assert notification["message_id"] == user_message["id"]

    fetched = client.post(
        "/a2a", json=_control(
            "conversations/messages/get", message_id=user_message["id"]),
        headers=_bearer()).json()["result"]["message"]
    assert json.loads(fetched["parts"][0]["text"])["message"]["text"].startswith(
        "解释一下结果")
    denied = client.post(
        "/a2a", json=_control(
            "conversations/messages/get", message_id=user_message["id"]),
        headers=_bearer(OTHER_HUB_TOKEN)).json()
    assert denied["error"]["code"] == -32003

    responded = client.post(
        "/a2a", json=_control(
            "conversations/respond", message_id=user_message["id"],
            text="这是结果说明；后续执行会创建独立任务。"),
        headers=_bearer()).json()["result"]["message"]
    assert json.loads(responded["parts"][0]["text"])["status"] == "responded"
    replay = client.post(
        "/a2a", json=_control(
            "conversations/respond", message_id=user_message["id"],
            text="这是结果说明；后续执行会创建独立任务。"),
        headers=_bearer()).json()
    assert "result" in replay
    conflict = client.post(
        "/a2a", json=_control(
            "conversations/respond", message_id=user_message["id"],
            text="不一致的第二份答复"),
        headers=_bearer()).json()
    assert conflict["error"]["code"] == -32602
    saved_response = tm.conn.execute(
        "SELECT message_type, content_json FROM conversation_messages"
        " WHERE parent_message_id = ?;",
        (user_message["id"],),
    ).fetchone()
    assert saved_response["message_type"] == "llm.assistant"
    assert json.loads(saved_response["content_json"])["role"] == "assistant"
    acked = client.post(
        "/a2a", json=_control(
            "supervision/ack",
            notification_id=notification["notification_id"]),
        headers=_bearer()).json()["result"]["message"]
    assert json.loads(acked["parts"][0]["text"])["status"] == "acknowledged"

    followup = client.post(
        "/a2a", json=_control(
            "tasks/create", agent="codex", objective="执行后续只读检查",
            access_mode="read", parent_task_id=created["id"]),
        headers=_bearer()).json()["result"]["task"]
    assert followup["id"] != created["id"]
    assert state_store.get_task(tm.conn, followup["id"])["parent_id"] == \
        created["id"]
    assert delegated[-1] == (followup["id"], "codex")


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
        "/a2a", json=_legacy(
            "查询任务", agent="codex", access_mode="read"),
        headers={"X-Agent-Token": LEGACY_TOKEN}).json()["result"]
    assert "task" not in result
    assert delegated == [(result["id"], "codex")]
    assert state_store.get_task(tm.conn, result["id"])["collaboration_id"] is None
