"""Hermes → 单一 agentHub peer → Registry 动态委派契约。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes.policy import ApprovalPolicy
from orchestrator import agent_control_store, state_store
from orchestrator.a2a_server import create_app
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio

HUB_TOKEN = "test-agenthub-peer-token-0123456789"
LEGACY_TOKEN = "test-legacy-token-0123456789"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LAS_API_TOKEN", LEGACY_TOKEN)
    monkeypatch.setenv("LAS_A2A_PEERS", json.dumps({
        HUB_TOKEN: {"peer": "qishuo"},
    }))
    monkeypatch.delenv("LAS_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("LAS_ORCH_REQUIRE_AUTH", raising=False)
    tm = TaskManager(db_path=tmp_path / "state.db",
                     workspace=tmp_path / "ws")
    state_store.update_heartbeat(tm.conn, "codex",
                                 endpoint="http://worker:8201")
    state_store.update_heartbeat(tm.conn, "kimi",
                                 endpoint="http://worker:8202")
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


def _control(action: str, **fields) -> dict:
    text = json.dumps({"agenthub": "v1", "action": action, **fields},
                      ensure_ascii=False)
    return {"jsonrpc": "2.0", "id": "1", "method": "SendMessage",
            "params": {"message": {
                "role": "user", "contextId": "ctx-live",
                "parts": [{"text": text, "mediaType": "application/json"}]}}}


def _legacy(text: str, **metadata) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": metadata}}}


def test_registry_discovery_uses_single_peer(env):
    _, client, delegated = env
    response = client.post("/a2a", json=_control("agents/list"),
                           headers=_bearer()).json()
    message = response["result"]["message"]
    payload = json.loads(message["parts"][0]["text"])
    assert {item["id"] for item in payload["agents"]} >= {"codex", "kimi"}
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


def test_task_response_is_native_hermes_readable(env):
    _, client, delegated = env
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
    assert delegated == [(task["id"], "codex")]


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


def test_auth_and_card_contract(env):
    _, client, _ = env
    assert client.post("/a2a", json=_control("agents/list")).status_code == 401
    assert client.post("/a2a", json=_control("agents/list"),
                       headers=_bearer("wrong")).status_code == 401
    card = client.get("/.well-known/agent-card.json",
                      headers=_bearer()).json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert {item["id"] for item in card["skills"]} >= {
        "orchestrate", "registry-discovery", "approval-gate"}


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
    _, client, delegated = env
    result = client.post(
        "/a2a", json=_legacy("查询任务", agent="codex"),
        headers={"X-Agent-Token": LEGACY_TOKEN}).json()["result"]
    assert "task" not in result
    assert delegated == [(result["id"], "codex")]
