"""Codex Adapter A2A Server — 设计文档 §10 / Phase 2。"""

from __future__ import annotations

from adapters.codex import runner
from adapters.codex.card import agent_card
from adapters.server_common import build_app

AGENT_ID = "codex"


def create_app():
    return build_app(AGENT_ID, agent_card, runner.run, max_concurrent=1)


app = create_app()
