"""Phase 5 验收：Hermes → agentgateway → A2A Worker（设计文档 §Phase 5）。

默认跳过（需要 agentgateway 二进制）；显式开启：
  LAS_RUN_GW=1 pytest tests/integration/test_agentgateway.py

验收点（§Phase 5）：
  1. 无 key 请求被 gateway 拒绝（401）
  2. Hermes 经 gateway 委派 fake worker 成功（auth + 路由 + 前缀重写）
  3. 禁用某 Agent 权限后，gateway 阻止请求（403）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from orchestrator.a2a_client import A2aClient

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_GW") != "1",
        reason="set LAS_RUN_GW=1 to run agentgateway acceptance",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
AGW_BIN = ROOT / "infra" / "agentgateway" / "bin" / "agentgateway"
AGW_CONF = ROOT / "infra" / "agentgateway" / "config.yaml"

WORKER_PORT = 8201
GW_PORT = 8300
GW_BASE = f"http://127.0.0.1:{GW_PORT}"


def _gateway_key() -> str:
    return subprocess.run(
        ["security", "find-generic-password", "-s", "agent-system",
         "-a", "gateway-api-key", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if not AGW_BIN.exists():
        pytest.skip("agentgateway binary not installed")
    ws = tmp_path_factory.mktemp("agent-workspace-gw")
    (ws / "logs").mkdir()
    os.environ["AGENT_WORKSPACE"] = str(ws)

    from adapters.fake.server import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1",
                            port=WORKER_PORT, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    # gateway 用配置副本，ACL 测试会修改它验证热加载
    conf_copy = ws / "config.yaml"
    conf_copy.write_text(AGW_CONF.read_text(encoding="utf-8"), encoding="utf-8")

    env = dict(os.environ, GATEWAY_API_KEY=_gateway_key())
    gw = subprocess.Popen([str(AGW_BIN), "-f", str(conf_copy)],
                          env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                # 无 key 也应得到 gateway 的 401（而不是连接失败）
                if httpx.get(f"{GW_BASE}/agents/codex/health",
                             timeout=1).status_code == 401:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            raise RuntimeError("agentgateway did not start")
        yield conf_copy
    finally:
        gw.terminate()
        gw.wait(timeout=5)
        srv.should_exit = True
        thread.join(timeout=5)


async def test_gateway_requires_api_key(stack):
    """无 key → 401，Worker 不直接暴露给未认证调用方。"""
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{GW_BASE}/agents/codex/health")
        assert r.status_code == 401


async def test_hermes_delegates_via_gateway(stack, monkeypatch):
    """Hermes 只访问 gateway：for_agent + Bearer key → 任务完成。"""
    monkeypatch.setenv("AGENT_GATEWAY_URL", GW_BASE)
    client = A2aClient.for_agent("codex", "http://127.0.0.1:9/unused",
                                 timeout=30)

    card = await client.get_agent_card()
    assert card["name"] == "fake-worker"

    task = await client.send_message(
        "gateway acceptance task",
        idempotency_key="T-P5-GW:1",
    )
    assert task["status"]["state"] == "completed"


async def test_acl_blocks_disabled_agent(stack):
    """§Phase 5 验收：禁用某 Agent 权限后，gateway 阻止请求（403）。

    把 key 元数据里的 agents 列表改为只含 kimi（热加载生效），
    再访问 codex 路由应被 authorization 规则拒绝。
    """
    conf = stack
    original = conf.read_text(encoding="utf-8")
    assert "agents: codex,kimi" in original
    key = _gateway_key()
    try:
        conf.write_text(original.replace("agents: codex,kimi", "agents: kimi"),
                        encoding="utf-8")
        denied = None
        for _ in range(50):  # 等热加载
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{GW_BASE}/agents/codex/health",
                                headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 403:
                denied = r.status_code
                break
            time.sleep(0.2)
        assert denied == 403, "ACL 禁用 codex 后请求未被拦截"
    finally:
        conf.write_text(original, encoding="utf-8")


async def test_direct_bypass_still_possible_but_unauthenticated(stack):
    """直连模式（无 gateway env）不带 key —— 仅证明 for_agent 回退行为。"""
    os.environ.pop("AGENT_GATEWAY_URL", None)
    client = A2aClient.for_agent("codex",
                                 f"http://127.0.0.1:{WORKER_PORT}",
                                 timeout=5)
    # 直连 adapter 本身无鉴权（Phase 1-4 显式接受的风险，
    # 见 §3.4 风险声明）；生产部署应只暴露 gateway 端口。
    assert client.auth_token is None
    assert client.base_url == f"http://127.0.0.1:{WORKER_PORT}"
