"""Event envelope 测试（设计文档 §7.2）。"""

from common.events import Event


def test_event_roundtrip():
    e = Event(
        event_type="task.completed",
        source="codex",
        task_id="T-20260817-0001",
        trace_id="trace-1",
        payload={"status_from": "working", "status_to": "completed", "attempt": 1},
    )
    restored = Event.from_dict(e.to_dict())
    assert restored.event_id == e.event_id
    assert restored.event_id.startswith("E-")
    assert restored.payload["status_to"] == "completed"
    assert restored.trace_id == "trace-1"
