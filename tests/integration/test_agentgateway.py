"""Phase 5 验收：Hermes → agentgateway → A2A Worker（设计文档 §Phase 5）。

默认跳过（需要 agentgateway 二进制）；显式开启：
  LAS_RUN_GW=1 pytest tests/integration/test_agentgateway.py

验收点（§Phase 5）：
  1. 无 key 请求被 gateway 拒绝（401）
  2. Hermes 经 gateway 委派 fake worker 成功（auth + 路由 + 前缀重写）
  3. 禁用某 Agent 权限后，gateway 阻止请求（403）
  4. 单一 Agent 路由突发超额后返回 429，不影响其他路由
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
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


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class GatewayStack:
    config_path: Path
    gateway_base: str
    worker_base: str
    process: subprocess.Popen
    env: dict[str, str]
    api_key: str

    def restart(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process = subprocess.Popen(
            [str(AGW_BIN), "-f", str(self.config_path)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                if httpx.get(
                    f"{self.gateway_base}/agents/codex/health", timeout=1
                ).status_code == 401:
                    return
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("agentgateway did not restart")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if not AGW_BIN.exists():
        pytest.skip("agentgateway binary not installed")
    ws = tmp_path_factory.mktemp("agent-workspace-gw")
    (ws / "logs").mkdir()
    os.environ["AGENT_WORKSPACE"] = str(ws)

    from adapters.fake.server import create_app

    worker_port = _free_loopback_port()
    gateway_port = _free_loopback_port()
    while gateway_port == worker_port:
        gateway_port = _free_loopback_port()

    config = uvicorn.Config(create_app(), host="127.0.0.1",
                            port=worker_port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    # gateway 用配置副本，ACL 测试会修改它验证热加载
    conf_copy = ws / "config.yaml"
    config_text = AGW_CONF.read_text(encoding="utf-8")
    config_text = config_text.replace("port: 8300", f"port: {gateway_port}", 1)
    config_text = config_text.replace(
        "host: 127.0.0.1:8201",
        f"host: 127.0.0.1:{worker_port}",
        1,
    )
    config_text = config_text.replace(
        "host: 127.0.0.1:8202",
        f"host: 127.0.0.1:{worker_port}",
        1,
    )
    config_text = config_text.replace(
        "host: 127.0.0.1:8203",
        f"host: 127.0.0.1:{worker_port}",
        1,
    )
    conf_copy.write_text(config_text, encoding="utf-8")

    api_key = secrets.token_urlsafe(32)
    env = dict(os.environ, GATEWAY_API_KEY=api_key)
    gw = subprocess.Popen([str(AGW_BIN), "-f", str(conf_copy)],
                          env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                # 无 key 也应得到 gateway 的 401（而不是连接失败）
                if httpx.get(f"http://127.0.0.1:{gateway_port}/agents/codex/health",
                             timeout=1).status_code == 401:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            raise RuntimeError("agentgateway did not start")
        stack = GatewayStack(
            config_path=conf_copy,
            gateway_base=f"http://127.0.0.1:{gateway_port}",
            worker_base=f"http://127.0.0.1:{worker_port}",
            process=gw,
            env=env,
            api_key=api_key,
        )
        yield stack
    finally:
        process = stack.process if "stack" in locals() else gw
        process.terminate()
        process.wait(timeout=5)
        srv.should_exit = True
        thread.join(timeout=5)


async def test_gateway_requires_api_key(stack):
    """无 key → 401，Worker 不直接暴露给未认证调用方。"""
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{stack.gateway_base}/agents/codex/health")
        assert r.status_code == 401


async def test_hermes_delegates_via_gateway(stack, monkeypatch):
    """Hermes 只访问 gateway：for_agent + Bearer key → 任务完成。"""
    monkeypatch.setenv("AGENT_GATEWAY_URL", stack.gateway_base)
    monkeypatch.setenv("LAS_GATEWAY_API_KEY", stack.api_key)
    client = A2aClient.for_agent("codex", "http://127.0.0.1:9/unused",
                                 timeout=30)

    card = await client.get_agent_card()
    assert card["name"] == "fake-worker"

    task = await client.send_and_wait(
        "gateway acceptance task",
        idempotency_key="T-P5-GW:1",
    )
    assert task["status"]["state"] == "completed"


async def test_acl_blocks_disabled_agent(stack):
    """§Phase 5 验收：禁用某 Agent 权限后，gateway 阻止请求（403）。

    把 key 元数据里的 agents 列表改为只含 kimi（热加载生效），
    再访问 codex 路由应被 authorization 规则拒绝。
    """
    conf = stack.config_path
    original = conf.read_text(encoding="utf-8")
    assert "agents: codex,kimi" in original
    key = stack.api_key
    try:
        conf.write_text(original.replace("agents: codex,kimi", "agents: kimi"),
                        encoding="utf-8")
        denied = None
        for _ in range(50):  # 等热加载
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{stack.gateway_base}/agents/codex/health",
                                headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 403:
                denied = r.status_code
                break
            time.sleep(0.2)
        assert denied == 403, "ACL 禁用 codex 后请求未被拦截"
    finally:
        conf.write_text(original, encoding="utf-8")
        for _ in range(50):
            async with httpx.AsyncClient(timeout=5) as client:
                restored = await client.get(
                    f"{stack.gateway_base}/agents/codex/health",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if restored.status_code == 200:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("ACL 配置恢复后 codex 路由未重新放行")


async def test_direct_bypass_still_possible_but_unauthenticated(stack):
    """直连模式（无 gateway env）不带 key —— 仅证明 for_agent 回退行为。"""
    os.environ.pop("AGENT_GATEWAY_URL", None)
    client = A2aClient.for_agent("codex",
                                 stack.worker_base,
                                 timeout=5)
    # 直连 adapter 本身无鉴权（Phase 1-4 显式接受的风险，
    # 见 §3.4 风险声明）；生产部署应只暴露 gateway 端口。
    assert client.auth_token is None
    assert client.base_url == stack.worker_base


async def test_route_rate_limit_rejects_a_runaway_loop(stack):
    """kimi 使用独立限流桶；后端离线也应在突发超额后由 gateway 返回 429。"""
    key = stack.api_key
    statuses: list[int] = []
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(35):
            response = await client.get(
                f"{stack.gateway_base}/agents/kimi/health",
                headers={"Authorization": f"Bearer {key}"},
            )
            statuses.append(response.status_code)

        # kimi 的桶耗尽不能拖累 codex 的独立桶。
        codex = await client.get(
            f"{stack.gateway_base}/agents/codex/health",
            headers={"Authorization": f"Bearer {key}"},
        )

    assert 429 in statuses
    assert codex.status_code == 200


async def test_gateway_restart_preserves_idempotent_a2a_task(stack, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", stack.gateway_base)
    monkeypatch.setenv("LAS_GATEWAY_API_KEY", stack.api_key)
    client = A2aClient.for_agent("codex", "http://unused", timeout=30)
    task_id = "T-GATEWAY-RESTART-1"
    first = await client.send_and_wait(
        "gateway restart idempotency",
        idempotency_key="gateway-restart:once",
        task_id=task_id,
    )
    assert first["status"]["state"] == "completed"

    stack.restart()
    replay = await client.send_message(
        "gateway restart idempotency",
        idempotency_key="gateway-restart:once",
        task_id=task_id,
    )
    assert replay["id"] == first["id"]
    assert replay["status"]["state"] == "completed"
