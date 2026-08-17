"""kimi runner 的响应解析：9router SSE 怪癖兼容（离线，不调真实 LLM）。"""

import httpx

from adapters.kimi.runner import _extract_content


def _resp(body: str, content_type: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode("utf-8"),
                          headers={"content-type": content_type})


def test_plain_json_response():
    body = '{"choices": [{"message": {"content": "你好"}}]}'
    assert _extract_content(_resp(body, "application/json")) == "你好"


def test_sse_normal_framing():
    body = (
        'data: {"choices": [{"delta": {"content": "你好"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "，世界"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    assert _extract_content(_resp(body, "text/event-stream")) == "你好，世界"


def test_sse_missing_separator_between_chunks():
    # 9router 实测怪癖：chunk 之间缺少 \n\n，直接粘连
    body = (
        'data: {"choices": [{"delta": {"content": "甲"}}]}'
        'data: {"choices": [{"delta": {"content": "乙"}}]}'
        'data: [DONE]\n\n'
    )
    assert _extract_content(_resp(body, "text/event-stream")) == "甲乙"


def test_sse_skips_usage_only_chunk():
    body = (
        'data: {"choices": [{"delta": {"content": "内容"}}]}\n\n'
        'data: {"choices": [], "usage": {"total_tokens": 10}}\n\n'
        'data: [DONE]\n\n'
    )
    assert _extract_content(_resp(body, "text/event-stream")) == "内容"
