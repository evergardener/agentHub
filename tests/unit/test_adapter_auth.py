"""Adapter token 鉴权中间件 + A2A 客户端 X-Agent-Token 头（v3 加固）。

规则：
- LAS_ADAPTER_TOKEN 非空 → 除 /health 外全部要求 X-Agent-Token 匹配
- 空（未配置）→ 不启用鉴权（本地开发默认）
- A2aClient 在直连与 gateway 两种路径都自动携带该头
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from adapters.server_common import build_app


def _card(base_url: str) -> dict:
    return {"name": "t", "skills": []}


async def _runner(task) -> list[dict]:
    return []


_RPC = {"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {"id": "x"}}


def test_auth_enforced_when_token_set(monkeypatch):
    monkeypatch.setenv("LAS_ADAPTER_TOKEN", "secret-tok")
    client = TestClient(build_app("t", _card, _runner))

    # /health 豁免（探活可用）
    assert client.get("/health").status_code == 200
    # 无头 / 错头 → 401
    assert client.get("/.well-known/agent-card.json").status_code == 401
    assert client.post("/a2a", json=_RPC).status_code == 401
    assert client.post("/a2a", json=_RPC,
                       headers={"X-Agent-Token": "nope"}).status_code == 401
    # 正确头放行（tasks/get 对不存在 id 返回 JSON-RPC error，但 HTTP 200）
    r = client.post("/a2a", json=_RPC, headers={"X-Agent-Token": "secret-tok"})
    assert r.status_code == 200
    assert "error" in r.json()  # -32602 task not found，说明通过了鉴权


def test_open_when_token_unset(monkeypatch):
    monkeypatch.delenv("LAS_ADAPTER_TOKEN", raising=False)
    client = TestClient(build_app("t", _card, _runner))
    assert client.get("/.well-known/agent-card.json").status_code == 200
    assert client.post("/a2a", json=_RPC).status_code == 200


def test_a2a_client_carries_agent_token(monkeypatch):
    from orchestrator.a2a_client import A2aClient

    monkeypatch.setenv("LAS_ADAPTER_TOKEN", "abc")
    monkeypatch.delenv("LAS_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)

    direct = A2aClient.for_agent("codex", "http://127.0.0.1:8201")
    assert direct._headers() == {"X-Agent-Token": "abc"}

    monkeypatch.setenv("LAS_GATEWAY_URL", "http://gw:8300")
    monkeypatch.setenv("LAS_GATEWAY_API_KEY", "gwk")
    via_gw = A2aClient.for_agent("codex", "http://127.0.0.1:8201")
    # gateway 路径：Bearer 给 gateway，X-Agent-Token 透传给 adapter
    assert via_gw._headers() == {
        "Authorization": "Bearer gwk",
        "X-Agent-Token": "abc",
    }

    monkeypatch.delenv("LAS_ADAPTER_TOKEN")
    assert A2aClient("http://x")._headers() == {}
