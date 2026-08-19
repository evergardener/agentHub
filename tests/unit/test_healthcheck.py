"""Container health probes fail closed without leaking connection details."""

from __future__ import annotations

from common import healthcheck


def test_database_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_DATABASE_URL", f"sqlite:///{tmp_path}/health.db")
    from state.db import init_db

    init_db(tmp_path / "health.db").close()
    assert healthcheck.database_ready() is True


def test_database_ready_fails_closed(monkeypatch):
    monkeypatch.setenv("LAS_DATABASE_URL", "unsupported://database")
    assert healthcheck.database_ready() is False


def test_tcp_ready_fails_closed():
    assert healthcheck.tcp_ready("127.0.0.1", 1) is False


def test_http_ready_fails_closed():
    assert healthcheck.http_ready("http://127.0.0.1:1/ready") is False
