"""Isolated real-HTTPS alert delivery failure and recovery drill.

The test uses a temporary self-signed certificate, random loopback port and
temporary SQLite database. It never connects to a configured external webhook.
Run explicitly with ``LAS_RUN_ALERT_WEBHOOK=1``.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from common import config
from state import alert_store
from state.db import init_db
from state.notifier import WebhookNotifier

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_ALERT_WEBHOOK") != "1",
        reason="set LAS_RUN_ALERT_WEBHOOK=1 to run HTTPS webhook drill",
    ),
]

OPENSSL_BIN = shutil.which("openssl")


class _WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.requests.append({
            "authorization": self.headers.get("Authorization"),
            "payload": payload,
        })
        self.send_response(503 if self.server.fail_delivery else 204)
        self.end_headers()

    def log_message(self, _format: str, *args) -> None:
        return


def _certificate(tmp_path: Path) -> tuple[Path, Path]:
    if not OPENSSL_BIN:
        pytest.skip("openssl not installed")
    cert = tmp_path / "webhook-cert.pem"
    key = tmp_path / "webhook-key.pem"
    subprocess.run(
        [
            OPENSSL_BIN, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-subj", "/CN=127.0.0.1", "-addext",
            "subjectAltName=IP:127.0.0.1", "-keyout", str(key),
            "-out", str(cert), "-days", "1",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return cert, key


def _make_due(conn, alert_id: str) -> None:
    conn.execute(
        "UPDATE alerts SET next_delivery_at = '2000-01-01T00:00:00+08:00'"
        " WHERE id = ?;",
        (alert_id,),
    )
    conn.commit()


async def test_real_https_webhook_failure_escalation_and_recovery(tmp_path):
    cert, key = _certificate(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
    server.fail_delivery = True
    server.requests = []
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"https://127.0.0.1:{server.server_port}/agenthub"
    token = "isolated-webhook-token-20260820"
    config.validate_alert_webhook(url, token)
    conn = init_db(tmp_path / "alerts.db")
    alert = alert_store.upsert_alert(
        conn,
        kind="adapter_unavailable",
        severity="warning",
        source="fault-drill",
        task_id="T-ALERT-FAULT-1",
        detail="isolated HTTPS failure/recovery",
    )
    client_tls = ssl.create_default_context(cafile=str(cert))
    client = httpx.AsyncClient(verify=client_tls, timeout=5)
    notifier = WebhookNotifier(conn, url, token, client=client)
    try:
        for _ in range(3):
            _make_due(conn, alert["id"])
            assert await notifier.deliver_once() == {
                "delivered": 0, "failed": 1}

        failed = conn.execute(
            "SELECT severity, delivery_attempts, delivered_at"
            " FROM alerts WHERE id = ?;",
            (alert["id"],),
        ).fetchone()
        assert failed["severity"] == "critical"
        assert failed["delivery_attempts"] == 3
        assert failed["delivered_at"] is None

        server.fail_delivery = False
        _make_due(conn, alert["id"])
        assert await notifier.deliver_once() == {
            "delivered": 1, "failed": 0}
        assert await notifier.deliver_once() == {
            "delivered": 0, "failed": 0}
    finally:
        await client.aclose()
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(server.requests) == 4
    assert all(
        request["authorization"] == f"Bearer {token}"
        for request in server.requests
    )
    assert all(
        request["payload"]["id"] == alert["id"]
        for request in server.requests
    )
