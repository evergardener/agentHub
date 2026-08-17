"""Fake Worker A2A Server — 设计文档 §10。

接口与行为由 adapters.server_common.build_app 提供；
本模块只定义身份、Agent Card 与假运行时。
"""

from __future__ import annotations

from adapters.fake import runner
from adapters.fake.card import agent_card
from adapters.server_common import build_app

AGENT_ID = "fake"


def create_app():
    return build_app(AGENT_ID, agent_card, runner.run, max_concurrent=1)


app = create_app()
