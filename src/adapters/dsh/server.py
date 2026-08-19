"""DeepSeek Harness Adapter A2A server."""

from __future__ import annotations

from adapters.dsh.card import agent_card
from adapters.dsh.session import DshWebSessionAdapter
from adapters.server_common import build_app

AGENT_ID = "dsh"


def create_app():
    adapter = DshWebSessionAdapter()
    return build_app(
        AGENT_ID,
        agent_card,
        max_concurrent=1,
        session_adapter=adapter,
        health_check=adapter.health,
    )


app = create_app()
