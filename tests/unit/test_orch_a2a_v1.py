"""Orchestrator A2A v1.0 compatibility 契约测试（Hermes 原生 A2A 接入）。

对应 Hermes_Native_A2A_Integration_Handoff.md §7 测试清单：
  - v1.0 SendMessage 解析（member-presence text Part）与 {"task": ...} 包装
  - legacy message/send 保持兼容（bare Task）
  - Bearer 正确/错误/缺失；X-Agent-Token legacy；双 header 冲突拒绝
  - card supportedInterfaces 声明 JSONRPC / v1.0
  - peer token 固定路由：只能投映射 worker，伪造 metadata.agent 无效
  - tasks/approve | tasks/reject 精确动作；compat 路径不走自然语言审批
  - offline/未知/重复/终态操作的稳定错误
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes.policy import ApprovalPolicy
from orchestrator import state_store
from orchestrator.a2a_server import create_app
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio

CODEX_TOKEN = "test-peer-token-codex"
KIMI_TOKEN = "test-peer-token-kimi"
LEGACY_TOKEN = "test-legacy-token"

PEERS_JSON = json.dumps({
    CODEX_TOKEN: {"peer": "qishuo-codex", "worker": "codex"},
    KIMI_TOKEN: {"peer": "qishuo-kimi", "worker": "kimi"},
})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LAS_API_TOKEN", LEGACY_TOKEN)
    monkeypatch.delenv("LAS_ADAPTER_TOKEN", raising=False)
    monkeypatch.setenv("LAS_A2A_PEERS", PEERS_JSON)
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


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _send_v1(text: str, **metadata) -> dict:
    """A2A v1.0 SendMessage：text Part 用 member-presence 形状。"""
    return {"jsonrpc": "2.0", "id": "1", "method": "SendMessage",
            "params": {"message": {
                "role": "user",
                "parts": [{"text": text, "mediaType": "text/plain"}],
                "metadata": metadata}}}


def _send_legacy(text: str, **metadata) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": metadata}}}


# ---------- SendMessage / 响应形状 ----------


def test_sendmessage_v1_wrapped_task(env):
    _, client, delegated = env
    r = client.post("/a2a", json=_send_v1("查询当前任务列表"),
                    headers=_bearer(CODEX_TOKEN))
    assert r.status_code == 200
    result = r.json()["result"]
    assert set(result) == {"task"}, "v1.0 响应必须是 SendMessageResponse 包装"
    task = result["task"]
    assert task["status"]["state"] == "submitted"
    assert delegated == [(task["id"], "codex")]


def test_sendmessage_legacy_part_also_accepted(env):
    """v1.0 方法 + legacy kind:text Part 也要能解析（extractor 双向兼容）。"""
    _, client, _ = env
    body = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "查询当前任务列表"}]}}}
    r = client.post("/a2a", json=body, headers=_bearer(CODEX_TOKEN))
    assert r.json()["result"]["task"]["id"].startswith("T-")


def test_legacy_message_send_stays_bare(env):
    _, client, _ = env
    r = client.post("/a2a", json=_send_legacy("查询任务", agent="codex"),
                    headers={"X-Agent-Token": LEGACY_TOKEN})
    result = r.json()["result"]
    assert "id" in result and "task" not in result, "legacy 保持 bare Task"


# ---------- 鉴权 ----------


def test_bearer_wrong_or_missing_rejected(env):
    _, client, _ = env
    assert client.post("/a2a", json=_send_v1("hi")).status_code == 401
    bad = client.post("/a2a", json=_send_v1("hi"),
                      headers=_bearer("wrong-token"))
    assert bad.status_code == 401


def test_conflicting_headers_rejected(env):
    _, client, _ = env
    headers = {"Authorization": f"Bearer {CODEX_TOKEN}",
               "X-Agent-Token": LEGACY_TOKEN}
    assert client.post("/a2a", json=_send_v1("hi"),
                       headers=headers).status_code == 401


def test_same_value_double_header_accepted(env):
    """两 header 同值不算冲突（如 client 把 api token 同时塞进两种头）。"""
    _, client, _ = env
    headers = {"Authorization": f"Bearer {LEGACY_TOKEN}",
               "X-Agent-Token": LEGACY_TOKEN}
    r = client.post("/a2a", json=_send_legacy("查询任务", agent="codex"),
                    headers=headers)
    assert r.status_code == 200


def test_agent_card_advertises_callable_v1_rpc_interface(env):
    """Hermes selects supportedInterfaces.url and POSTs that URL verbatim."""
    _, client, delegated = env
    r = client.get("/.well-known/agent-card.json",
                   headers=_bearer(KIMI_TOKEN))
    card = r.json()
    assert card["url"] and card["version"]
    iface = card["supportedInterfaces"][0]
    assert iface["protocolBinding"] == "JSONRPC"
    assert iface["protocolVersion"] == "1.0"
    assert iface["url"] == f"{card['url']}/a2a"

    # Model Hermes native _rpc_url(card): select interface URL, then POST it.
    rpc = client.post(iface["url"], json=_send_v1("查询当前任务列表"),
                      headers=_bearer(KIMI_TOKEN))
    assert rpc.status_code == 200
    assert delegated and delegated[0][1] == "kimi"


# ---------- peer 固定路由 ----------


def test_peer_token_routes_to_fixed_worker(env):
    _, client, delegated = env
    r = client.post("/a2a", json=_send_v1("检索一下资料"),
                    headers=_bearer(KIMI_TOKEN))
    assert r.status_code == 200
    assert delegated and delegated[0][1] == "kimi"


def test_forged_metadata_agent_rejected(env):
    """peer token + 与映射冲突的 metadata.agent → 拒绝（不信任请求体）。"""
    _, client, delegated = env
    r = client.post("/a2a", json=_send_v1("查询任务", agent="kimi"),
                    headers=_bearer(CODEX_TOKEN))
    assert r.json()["error"]["code"] == -32602
    assert not delegated


def test_consistent_metadata_agent_accepted(env):
    _, client, delegated = env
    r = client.post("/a2a", json=_send_v1("查询任务", agent="codex"),
                    headers=_bearer(CODEX_TOKEN))
    assert r.status_code == 200
    assert delegated and delegated[0][1] == "codex"


def test_peer_offline_worker_stable_error(tmp_path, monkeypatch):
    """映射 worker offline → 稳定错误，不回退到其他 worker。"""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LAS_API_TOKEN", LEGACY_TOKEN)
    monkeypatch.setenv("LAS_A2A_PEERS", PEERS_JSON)
    tm = TaskManager(db_path=tmp_path / "s.db", workspace=tmp_path / "ws")
    state_store.update_heartbeat(tm.conn, "codex",
                                 endpoint="http://worker:8201")
    client = TestClient(create_app(tm=tm, policy=ApprovalPolicy()))
    r = client.post("/a2a", json=_send_v1("检索资料"),
                    headers=_bearer(KIMI_TOKEN))
    err = r.json()["error"]
    # unknown / offline 均为稳定错误，不回退到其他 worker
    assert err["code"] == -32602 and "kimi" in err["message"]


# ---------- tasks/approve | tasks/reject ----------


def _create_pending(client, token: str) -> str:
    r = client.post("/a2a", json=_send_v1("在工作区创建文件 x.md"),
                    headers=_bearer(token))
    task = r.json()["result"]["task"]
    assert task["status"]["state"] == "input-required"
    return task["id"]


def test_tasks_approve_delegates(env):
    tm, client, delegated = env
    tid = _create_pending(client, CODEX_TOKEN)
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": "2",
                                  "method": "tasks/approve",
                                  "params": {"id": tid}},
                    headers=_bearer(CODEX_TOKEN))
    task = r.json()["result"]
    assert task["status"]["state"] == "submitted"
    assert delegated == [(tid, "codex")]
    # 审计：记录 peer identity，不记 token
    ev = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.approved';", (tid,)).fetchone()
    payload = json.loads(ev["payload_json"])
    assert payload["by"] == "qishuo-codex"
    assert CODEX_TOKEN not in ev["payload_json"]


def test_tasks_reject_cancels(env):
    _, client, delegated = env
    tid = _create_pending(client, CODEX_TOKEN)
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": "2",
                                  "method": "tasks/reject",
                                  "params": {"id": tid}},
                    headers=_bearer(CODEX_TOKEN))
    assert r.json()["result"]["status"]["state"] == "canceled"
    assert not delegated


def test_duplicate_approve_stable_error_no_redelegate(env):
    _, client, delegated = env
    tid = _create_pending(client, CODEX_TOKEN)
    ok = client.post("/a2a", json={"jsonrpc": "2.0", "id": "2",
                                   "method": "tasks/approve",
                                   "params": {"id": tid}},
                     headers=_bearer(CODEX_TOKEN))
    assert "result" in ok.json()
    again = client.post("/a2a", json={"jsonrpc": "2.0", "id": "3",
                                      "method": "tasks/approve",
                                      "params": {"id": tid}},
                        headers=_bearer(CODEX_TOKEN))
    assert again.json()["error"]["code"] == -32602
    assert delegated == [(tid, "codex")]  # 未重复委派


def test_approve_unknown_or_terminal_task(env):
    tm, client, _ = env
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": "2",
                                  "method": "tasks/approve",
                                  "params": {"id": "T-9999"}},
                    headers=_bearer(CODEX_TOKEN))
    assert "task not found" in r.json()["error"]["message"]
    # 已完成任务（非待批准）→ 稳定错误
    done = client.post("/a2a", json=_send_v1("查询任务列表"),
                       headers=_bearer(CODEX_TOKEN)).json()["result"]["task"]
    r2 = client.post("/a2a", json={"jsonrpc": "2.0", "id": "3",
                                   "method": "tasks/reject",
                                   "params": {"id": done["id"]}},
                     headers=_bearer(CODEX_TOKEN))
    assert r2.json()["error"]["code"] == -32602


def test_sendmessage_followup_text_not_an_approval(env):
    """compat 路径：taskId + 自然语言「批准」不得放行。"""
    _, client, delegated = env
    tid = _create_pending(client, CODEX_TOKEN)
    r = client.post("/a2a", json=_send_v1("批准", taskId=tid),
                    headers=_bearer(CODEX_TOKEN))
    assert r.json()["error"]["code"] == -32602
    assert not delegated


def test_ambiguous_word_not_an_approval(env):
    """legacy 路径：「不批准」含子串「批准」，必须不被误放行。"""
    _, client, delegated = env
    r = client.post("/a2a", json=_send_legacy("创建文件 x.md 写入摘要",
                                              agent="codex"),
                    headers={"X-Agent-Token": LEGACY_TOKEN})
    tid = r.json()["result"]["id"]
    r2 = client.post("/a2a", json=_send_legacy("不批准", taskId=tid),
                     headers={"X-Agent-Token": LEGACY_TOKEN})
    assert "error" not in r2.json(), "「不批准」应被识别为拒绝"
    assert r2.json()["result"]["status"]["state"] == "canceled"
    assert not delegated


def test_legacy_followup_still_works(env):
    """deprecated 的 legacy 自然语言审批保持可用（精确整句）。"""
    _, client, delegated = env
    r = client.post("/a2a", json=_send_legacy("创建文件 x.md 写入摘要",
                                              agent="codex"),
                    headers={"X-Agent-Token": LEGACY_TOKEN})
    tid = r.json()["result"]["id"]
    ok = client.post("/a2a", json=_send_legacy("批准", taskId=tid),
                     headers={"X-Agent-Token": LEGACY_TOKEN})
    assert ok.json()["result"]["status"]["state"] == "submitted"
    assert delegated == [(tid, "codex")]
