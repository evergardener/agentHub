"""OTel tracing — Evolution v3 §5.1。

默认 NoOp（LAS_OTEL_ENDPOINT 未配置时零开销、零依赖行为变化）。
配置 LAS_OTEL_ENDPOINT=http://jaeger:4318 后经 OTLP HTTP 上报。

跨进程 trace 串联：系统现有 trace 字符串（trace-<root_id>，经 A2A
metadata.traceId 贯通 Hermes → Adapter）确定性派生 128-bit OTel
trace_id；各进程 span 挂到同一合成根下，Jaeger 中呈单 trace 树。
"""

from __future__ import annotations

import hashlib
import os

from opentelemetry import trace
from opentelemetry.trace import (NonRecordingSpan, SpanContext, TraceFlags,
                                 set_span_in_context)

_INITIALIZED = False


def _trace_int(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:32], 16)


def _span_int(key: str) -> int:
    return int(hashlib.sha256(("span:" + key).encode()).hexdigest()[:16], 16)


def init_tracing(service_name: str, exporter=None) -> bool:
    """初始化 TracerProvider。未配置 endpoint 且未给 exporter 时保持 NoOp。

    exporter 参数主要供测试注入 InMemorySpanExporter。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return True
    endpoint = os.environ.get("LAS_OTEL_ENDPOINT", "").strip()
    if exporter is None and not endpoint:
        return False
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _INITIALIZED = True
    return True


def task_context(trace_key: str | None):
    """从 trace 字符串派生跨进程一致的父上下文（合成根）。"""
    if not trace_key:
        return None
    ctx = SpanContext(
        trace_id=_trace_int(trace_key),
        span_id=_span_int(trace_key),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return set_span_in_context(NonRecordingSpan(ctx))


def get_tracer(name: str = "agenthub") -> trace.Tracer:
    return trace.get_tracer(name)
