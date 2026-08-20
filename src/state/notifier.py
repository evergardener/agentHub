"""Deliver durable alerts to an optional HTTPS webhook with retry/escalation."""

from __future__ import annotations

import asyncio

import httpx

from common import config as cfg
from state import alert_store
from state.db import connect


class WebhookNotifier:
    def __init__(self, conn, url: str, token: str = "", *, client=None):
        self.conn = conn
        self.url = url
        self.token = token
        self.client = client or httpx.AsyncClient(
            timeout=cfg.alert_webhook_timeout())
        self._owns_client = client is None

    async def deliver_once(self) -> dict[str, int]:
        stats = {"delivered": 0, "failed": 0}
        if not self.url:
            return stats
        headers = ({"Authorization": f"Bearer {self.token}"}
                   if self.token else {})
        for alert in alert_store.due_alerts(self.conn):
            payload = {key: alert[key] for key in (
                "id", "kind", "severity", "source", "task_id", "detail",
                "occurrences", "first_seen_at", "last_seen_at")}
            try:
                response = await self.client.post(
                    self.url, json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                alert_store.record_delivery(
                    self.conn, alert["id"],
                    error=f"webhook HTTP {exc.response.status_code}")
                stats["failed"] += 1
            except httpx.HTTPError as exc:
                alert_store.record_delivery(
                    self.conn, alert["id"],
                    error=f"webhook transport {type(exc).__name__}")
                stats["failed"] += 1
            else:
                alert_store.record_delivery(self.conn, alert["id"])
                stats["delivered"] += 1
        return stats

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


async def main() -> None:
    from common import tracing

    tracing.init_tracing("notifier")
    url = cfg.alert_webhook_url()
    cfg.validate_alert_webhook(url, cfg.alert_webhook_token())
    conn = connect()
    notifier = WebhookNotifier(conn, url, cfg.alert_webhook_token())
    if not url:
        print("[notifier] webhook disabled; alerts remain visible in WebUI")
    try:
        while True:
            stats = await notifier.deliver_once()
            if stats["delivered"] or stats["failed"]:
                print(f"[notifier] delivery {stats}")
            await asyncio.sleep(cfg.alert_poll_interval())
    finally:
        await notifier.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
