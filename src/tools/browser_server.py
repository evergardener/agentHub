"""Browser MCP Server（只读）— §3.7 / Phase 7。

MVP 形态：无头抓取 + HTML→纯文本/链接提取（stdlib html.parser，无重依赖）。
交互式浏览器自动化后置（§Phase 7 的 browser 先接只读抓取）。

工具：
  fetch_text   GET URL，HTML 转纯文本
  fetch_links  GET URL，提取所有链接

运行：python -m tools.browser_server   （stdio 传输）
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from fastmcp import FastMCP

mcp = FastMCP("agent-browser")

MAX_BODY_BYTES = 2_000_000
MAX_TEXT_CHARS = 20_000
TIMEOUT = 30

_SKIP_TAGS = {"script", "style", "noscript", "template"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = " ".join(self.parts)
        return re.sub(r"\s+", " ", raw).strip()


class _LinkExtractor(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for k, v in attrs:
            if k == "href" and v:
                self.links.append(urljoin(self.base, v))


def _validate_url(url: str) -> None:
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"only http/https allowed, got: {scheme or url}")


def _get(url: str) -> tuple[str, str]:
    """返回 (content_type, body_text)。"""
    _validate_url(url)
    resp = httpx.get(
        url, timeout=TIMEOUT, follow_redirects=True,
        headers={"User-Agent": "agent-system-browser/0.1"},
    )
    resp.raise_for_status()
    return resp.headers.get("content-type", ""), resp.text[:MAX_BODY_BYTES]


def fetch_text(url: str) -> str:
    ct, body = _get(url)
    if "html" not in ct:
        return body[:MAX_TEXT_CHARS]
    ex = _TextExtractor()
    ex.feed(body)
    return ex.text()[:MAX_TEXT_CHARS]


def fetch_links(url: str) -> list[str]:
    _, body = _get(url)
    ex = _LinkExtractor(url)
    ex.feed(body)
    # 去重保序
    return list(dict.fromkeys(ex.links))[:200]


@mcp.tool
def text(url: str) -> str:
    """抓取 URL 并返回纯文本（HTML 去标签）。"""
    return fetch_text(url)


@mcp.tool
def links(url: str) -> list[str]:
    """抓取 URL 并提取页面中的链接（绝对化、去重）。"""
    return fetch_links(url)


if __name__ == "__main__":
    mcp.run()
