"""Kimi Adapter A2A Server — 设计文档 §Phase 6。"""

from __future__ import annotations

from adapters.kimi import runner
from adapters.kimi.card import agent_card
from adapters.server_common import build_app

AGENT_ID = "kimi"


def create_app():
    return build_app(AGENT_ID, agent_card, runner.run, max_concurrent=2)


app = create_app()
