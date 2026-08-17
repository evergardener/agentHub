"""A2aClient.for_agent 的路由选择（离线，不起 gateway）。"""

from __future__ import annotations

from orchestrator.a2a_client import A2aClient


def test_direct_when_no_gateway(monkeypatch):
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    c = A2aClient.for_agent("codex", "http://127.0.0.1:8201")
    assert c.base_url == "http://127.0.0.1:8201"
    assert c.auth_token is None


def test_gateway_prefix_and_auth(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://127.0.0.1:8300")
    monkeypatch.setenv("GATEWAY_API_KEY", "sk-gw-test")
    c = A2aClient.for_agent("kimi", "http://127.0.0.1:8202")
    assert c.base_url == "http://127.0.0.1:8300/agents/kimi"
    assert c.auth_token == "sk-gw-test"
    assert c._headers() == {"Authorization": "Bearer sk-gw-test"}


def test_gateway_url_trailing_slash(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://127.0.0.1:8300/")
    monkeypatch.setenv("GATEWAY_API_KEY", "k")
    c = A2aClient.for_agent("codex", "http://x")
    assert c.base_url == "http://127.0.0.1:8300/agents/codex"
