"""A2aClient.for_agent 的路由选择（离线，不起 gateway）。"""

from __future__ import annotations

import ssl

import pytest

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


def test_remote_gateway_uses_rotatable_jwt_and_mtls(monkeypatch, tmp_path):
    token_file = tmp_path / "gateway.jwt"
    token_file.write_text("header.payload.signature\n", encoding="utf-8")
    calls = {}

    class FakeContext:
        minimum_version = None

        def load_cert_chain(self, *, certfile, keyfile):
            calls["cert"] = (certfile, keyfile)

    context = FakeContext()

    def fake_context(*, cafile=None):
        calls["ca"] = cafile
        return context

    monkeypatch.setattr("orchestrator.a2a_client.ssl.create_default_context",
                        fake_context)
    monkeypatch.setenv("LAS_GATEWAY_URL", "https://gateway.example.test:8443")
    monkeypatch.setenv("LAS_GATEWAY_JWT_FILE", str(token_file))
    monkeypatch.setenv("LAS_GATEWAY_CA_FILE", "/secrets/ca.pem")
    monkeypatch.setenv("LAS_GATEWAY_CLIENT_CERT_FILE", "/secrets/client.pem")
    monkeypatch.setenv("LAS_GATEWAY_CLIENT_KEY_FILE", "/secrets/client-key.pem")

    client = A2aClient.for_agent("dsh", "http://unused")
    assert client.base_url == "https://gateway.example.test:8443/agents/dsh"
    assert client.auth_token is None
    assert client._headers()["Authorization"] == "Bearer header.payload.signature"
    token_file.write_text("rotated.payload.signature", encoding="utf-8")
    assert client._headers()["Authorization"] == "Bearer rotated.payload.signature"
    assert client.ssl_context is context
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert calls == {
        "ca": "/secrets/ca.pem",
        "cert": ("/secrets/client.pem", "/secrets/client-key.pem"),
    }


def test_gateway_tls_rejects_plaintext_and_incomplete_pair(monkeypatch):
    monkeypatch.setenv("LAS_GATEWAY_URL", "http://gateway.example.test:8300")
    monkeypatch.setenv("LAS_GATEWAY_CA_FILE", "/secrets/ca.pem")
    with pytest.raises(ValueError, match="https"):
        A2aClient.for_agent("codex", "http://unused")

    monkeypatch.setenv("LAS_GATEWAY_URL", "https://gateway.example.test:8443")
    monkeypatch.delenv("LAS_GATEWAY_CA_FILE")
    monkeypatch.setenv("LAS_GATEWAY_CLIENT_CERT_FILE", "/secrets/client.pem")
    with pytest.raises(ValueError, match="client cert"):
        A2aClient.for_agent("codex", "http://unused")
