"""Hindsight client 单元测试（不触网，mock HTTP 层）。"""

import pytest

from common.memory import Memory
from memory.hindsight_client import (
    HindsightMemoryService,
    scope_to_bank,
)


def test_scope_to_bank_mapping():
    assert scope_to_bank("user") == "las-user"
    assert scope_to_bank("system") == "las-system"
    assert scope_to_bank("project:multi-agent-platform") == "las-project-multi-agent-platform"
    with pytest.raises(ValueError):
        scope_to_bank("bogus")


def _service_with(monkeypatch, response: dict) -> HindsightMemoryService:
    svc = HindsightMemoryService(base_url="http://test", api_key="k")
    captured = {}

    def fake_request(method, path, body):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return response

    monkeypatch.setattr(svc, "_request", fake_request)
    svc._captured = captured
    return svc


def test_retain_uses_mapped_bank(monkeypatch):
    svc = _service_with(monkeypatch, {"items": [{"id": "m-1"}]})
    mid = svc.retain("用户偏好中文回复", "user", {"source": "test"})
    assert mid == "m-1"
    assert svc._captured["path"] == "/v1/default/banks/las-user/memories"
    assert svc._captured["body"]["items"][0]["content"] == "用户偏好中文回复"


def test_recall_maps_results(monkeypatch):
    svc = _service_with(
        monkeypatch,
        {"results": [{"id": "m-1", "content": "c", "metadata": {"a": 1}}]},
    )
    memories = svc.recall("偏好？", "project:x", budget_tokens=1024)
    assert memories == [
        Memory(id="m-1", content="c", scope="project:x", metadata={"a": 1})
    ]
    assert svc._captured["path"] == "/v1/default/banks/las-project-x/memories/recall"
    assert svc._captured["body"]["budget_tokens"] == 1024


def test_reflect_returns_text(monkeypatch):
    svc = _service_with(monkeypatch, {"response": "归纳结果"})
    assert svc.reflect("架构决策") == "归纳结果"
