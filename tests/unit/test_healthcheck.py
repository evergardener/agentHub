"""Container health probes fail closed without leaking connection details."""

from __future__ import annotations

from common import healthcheck


def test_database_ready(monkeypatch):
    class ReadyConnection:
        def execute(self, _sql):
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    monkeypatch.setattr("state.db.connect", lambda: ReadyConnection())
    assert healthcheck.database_ready() is True


def test_database_ready_fails_closed(monkeypatch):
    monkeypatch.setenv("LAS_DATABASE_URL", "unsupported://database")
    assert healthcheck.database_ready() is False


def test_tcp_ready_fails_closed():
    assert healthcheck.tcp_ready("127.0.0.1", 1) is False


def test_http_ready_fails_closed():
    assert healthcheck.http_ready("http://127.0.0.1:1/ready") is False
