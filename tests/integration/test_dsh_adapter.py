"""Opt-in check against the locally installed DSH Web service."""

from __future__ import annotations

import os

import pytest

from adapters.dsh.session import DshWebSessionAdapter

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_DSH") != "1",
        reason="set LAS_RUN_DSH=1 after starting `dsh web`",
    ),
]


async def test_real_dsh_web_api_is_reachable_without_model_call():
    adapter = DshWebSessionAdapter(timeout_seconds=10)
    result = await adapter._request("session.list", {})
    assert isinstance(result.get("items"), list)
