"""common.tracing 单元测试：NoOp 默认、跨进程 trace 派生一致性、span 导出。"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def tracing_reset(monkeypatch):
    monkeypatch.delenv("LAS_OTEL_ENDPOINT", raising=False)
    import common.tracing as t

    importlib.reload(t)
    yield t
    importlib.reload(t)


def test_noop_without_endpoint(tracing_reset, monkeypatch):
    assert tracing_reset.init_tracing("svc") is False
    tracer = tracing_reset.get_tracer()
    with tracer.start_as_current_span("x"):  # NoOp，不报错
        pass


def test_trace_derivation_deterministic(tracing_reset):
    c1 = tracing_reset.task_context("trace-T-1")
    c2 = tracing_reset.task_context("trace-T-1")
    from opentelemetry import trace as ot

    s1 = ot.get_current_span(c1).get_span_context()
    s2 = ot.get_current_span(c2).get_span_context()
    assert s1.trace_id == s2.trace_id  # 跨进程同 trace
    assert s1.trace_id != 0


def test_spans_exported_with_shared_trace(tracing_reset):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry import trace as ot
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    ot.set_tracer_provider(provider)

    t = tracing_reset
    tracer = t.get_tracer()
    # 模拟两个进程：同 trace_key → 同 trace_id
    with tracer.start_as_current_span(
            "task.delegate", context=t.task_context("trace-T-9")):
        pass
    with tracer.start_as_current_span(
            "adapter.execute", context=t.task_context("trace-T-9")):
        pass
    with tracer.start_as_current_span("unrelated"):
        pass

    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    assert by_name["task.delegate"].context.trace_id \
        == by_name["adapter.execute"].context.trace_id
    assert by_name["unrelated"].context.trace_id \
        != by_name["task.delegate"].context.trace_id
