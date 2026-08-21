"""Orchestrator A2A Server 单元测试（外部 hermes 接入契约）。

覆盖：新任务委派（auto/granted）、写操作 input-required、批准放行、
拒绝取消、tasks/get 状态映射、鉴权。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes.policy import ApprovalPolicy
from orchestrator import state_store
from orchestrator.a2a_server import create_app, validate_orchestrator_security
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("LAS_API_TOKEN", raising=False)
    monkeypatch.delenv("LAS_ADAPTER_TOKEN", raising=False)
    monkeypatch.delenv("LAS_A2A_PEERS", raising=False)
    monkeypatch.delenv("LAS_ORCH_REQUIRE_AUTH", raising=False)
    tm = TaskManager(db_path=tmp_path / "state.db",
                     workspace=tmp_path / "ws")
    # 注册在线 worker
    state_store.update_heartbeat(tm.conn, "codex",
                                 endpoint="http://worker:8201")
    # 拦截真实 A2A 下发（委派只落状态，不发 HTTP）
    delegated: list[tuple[str, str]] = []

    async def fake_delegate(self, task_id, endpoint, agent_id, attempt=1):
        state_store.transition_task(self.conn, task_id,
                                    state_store.TaskStatus.ASSIGNED)
        delegated.append((task_id, agent_id))

    monkeypatch.setattr(TaskManager, "delegate_task", fake_delegate)
    client = TestClient(create_app(tm=tm, policy=ApprovalPolicy()))
    return tm, client, delegated


def _send(text, **metadata):
    return {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {"role": "user",
                                   "parts": [{"kind": "text", "text": text}],
                                   "metadata": metadata}}}


def test_delegate_auto_approved(env):
    tm, client, delegated = env
    r = client.post("/a2a", json=_send("查询当前任务列表", agent="codex"))
    assert r.status_code == 200
    task = r.json()["result"]
    assert task["status"]["state"] == "submitted"   # assigned → submitted
    assert task["metadata"]["internal_status"] == "assigned"
    assert delegated and delegated[0][1] == "codex"


def test_write_op_requires_approval(env):
    tm, client, delegated = env
    r = client.post("/a2a", json=_send("在工作区创建文件 x.md", agent="codex"))
    task = r.json()["result"]
    assert task["status"]["state"] == "input-required"
    assert "批准" in task["status"]["message"]["parts"][0]["text"]
    assert not delegated  # 未委派


def test_approve_followup_delegates(env):
    tm, client, delegated = env
    tid = client.post("/a2a", json=_send("创建文件 x.md 写入摘要",
                                         agent="codex")).json()["result"]["id"]
    r = client.post("/a2a", json=_send("批准", taskId=tid))
    task = r.json()["result"]
    assert task["status"]["state"] == "submitted"
    assert delegated == [(tid, "codex")]


def test_reject_followup_cancels(env):
    tm, client, delegated = env
    tid = client.post("/a2a", json=_send("创建文件 x.md 写入摘要",
                                         agent="codex")).json()["result"]["id"]
    r = client.post("/a2a", json=_send("拒绝", taskId=tid))
    task = r.json()["result"]
    assert task["status"]["state"] == "canceled"
    assert not delegated


def test_unknown_agent_error_lists_online(env):
    tm, client, _ = env
    r = client.post("/a2a", json=_send("查一下状态", agent="nobody"))
    err = r.json()["error"]
    assert err["code"] == -32602 and "codex" in err["message"]


def test_missing_agent_metadata_rejected(env):
    tm, client, _ = env
    r = client.post("/a2a", json=_send("查一下状态"))
    assert r.json()["error"]["code"] == -32602


def test_tasks_get_with_artifacts(env):
    tm, client, _ = env
    tid = client.post("/a2a", json=_send("查询任务列表",
                                         agent="codex")).json()["result"]["id"]
    state_store.add_artifact(tm.conn, task_id=tid, agent_id="codex",
                             name="workspace/out.md", path="/x/out.md",
                             sha256="0" * 64)
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": "2",
                                  "method": "tasks/get",
                                  "params": {"id": tid}})
    task = r.json()["result"]
    assert task["artifacts"][0]["name"] == "workspace/out.md"


def test_auth_enforced_when_token_set(tmp_path, monkeypatch):
    monkeypatch.delenv("LAS_ORCH_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("LAS_API_TOKEN", "orch-tok")
    tm = TaskManager(db_path=tmp_path / "s.db", workspace=tmp_path / "ws")
    client = TestClient(create_app(tm=tm))
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/.well-known/agent-card.json").status_code == 401
    ok = client.get("/.well-known/agent-card.json",
                    headers={"X-Agent-Token": "orch-tok"})
    assert ok.status_code == 200
    assert ok.json()["name"] == "agenthub-orchestrator"


def test_orchestrator_security_bind_validation(tmp_path, monkeypatch):
    for name in ("LAS_API_TOKEN", "LAS_ADAPTER_TOKEN", "LAS_A2A_PEERS",
                 "LAS_ORCH_REQUIRE_AUTH"):
        monkeypatch.delenv(name, raising=False)
    validate_orchestrator_security("127.0.0.1")
    with pytest.raises(RuntimeError, match="非 loopback"):
        validate_orchestrator_security("0.0.0.0")

    monkeypatch.setenv("LAS_ORCH_REQUIRE_AUTH", "true")
    with pytest.raises(RuntimeError, match="为空"):
        create_app(tm=TaskManager(
            db_path=tmp_path / "required.db", workspace=tmp_path / "ws2"))
    monkeypatch.setenv("LAS_API_TOKEN", "weak")
    with pytest.raises(RuntimeError, match="至少 16"):
        validate_orchestrator_security("0.0.0.0")
    with pytest.raises(RuntimeError, match="至少 16"):
        create_app(tm=TaskManager(
            db_path=tmp_path / "weak.db", workspace=tmp_path / "ws3"))
    monkeypatch.setenv("LAS_API_TOKEN", "strong-token-0123456789")
    validate_orchestrator_security("0.0.0.0")
