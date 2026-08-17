"""M1-B 验收：Hermes Brain 三态审批 + 工具调用循环（Evolution v3 §6）。

真实组件：uvicorn 跑 fake adapter（HTTP）+ 真实 nats-server + State Writer
durable consumer + TaskManager + HermesTools；LLM 用 ScriptedLLM 替身
（按已见 tool result 数量推进剧本，断言每个工具结果）。

场景：
  A. 只读任务自动批准流：create → delegate(approval=auto) → wait → review → accepted
  B. 对话审批流：重启 → needs_approval（任务不动）→ 用户"批准" →
     approve_and_delegate（记录 task.approved 事件）→ 完成验收
  C. 常驻授权流（直接驱动 HermesTools）：grant → delegate(approval=granted，
     记录 task.auto_approved) → revoke → 回到 needs_approval
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml

pytestmark = pytest.mark.anyio

NATS_BIN = shutil.which("nats-server")
NATS_PORT = 14225
NATS_URL = f"nats://127.0.0.1:{NATS_PORT}"
ADAPTER_PORT = 8297
ADAPTER_URL = f"http://127.0.0.1:{ADAPTER_PORT}"

requires_nats = pytest.mark.skipif(not NATS_BIN, reason="nats-server not installed")


# ---------- 测试设施 ----------


def _start_nats(store_dir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [NATS_BIN, "-js", "-p", str(NATS_PORT), "--store_dir", str(store_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", NATS_PORT), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("nats-server did not start")


def _start_adapter():
    from adapters.fake.server import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1",
                            port=ADAPTER_PORT, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"{ADAPTER_URL}/health", timeout=1).status_code == 200:
                return srv, thread
        except httpx.TransportError:
            time.sleep(0.1)
    raise RuntimeError("adapter did not start")


class ScriptedLLM:
    """按已消费的 tool result 数量推进剧本；handler(n, results, messages)。

    handler 返回 (tool_name, args) 发起工具调用，返回 str 作为终答文本。
    """

    def __init__(self, handler):
        self.handler = handler
        self.results: list[dict] = []

    async def chat(self, messages, tools=None):
        from hermes.llm import LLMReply, ToolCall

        self.results = [json.loads(m["content"]) for m in messages
                        if m.get("role") == "tool"]
        out = self.handler(len(self.results), self.results, messages)
        if isinstance(out, str):  # 纯文本终答
            return LLMReply(content=out,
                            raw_message={"role": "assistant", "content": out})
        name, args = out
        call_id = f"call_{len(self.results)}"
        return LLMReply(
            tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
            raw_message={"role": "assistant", "content": "",
                         "tool_calls": [{
                             "id": call_id, "type": "function",
                             "function": {"name": name,
                                          "arguments": json.dumps(
                                              args, ensure_ascii=False)},
                         }]},
        )


def _make_harness(tmp_path, monkeypatch):
    """起 NATS + fake adapter + StateWriter；返回 (tm, agents_path, cleanup)。"""
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", NATS_URL)
    db_path = tmp_path / "agent-state.db"

    agents_path = tmp_path / "agents.yaml"
    agents_path.write_text(yaml.safe_dump({"agents": {
        "codex": {"endpoint": ADAPTER_URL, "skills": ["coding", "devops"]},
        "kimi": {"endpoint": ADAPTER_URL, "skills": ["research"]},
    }}), encoding="utf-8")

    nats_proc = _start_nats(tmp_path / "jetstream")
    adapter_srv, adapter_thread = _start_adapter()

    from orchestrator.task_manager import TaskManager

    tm = TaskManager(db_path=db_path, workspace=ws)
    return tm, agents_path, nats_proc, adapter_srv, adapter_thread, db_path


async def _stop_harness(nats_proc, adapter_srv, adapter_thread,
                        writer_stop, writer_task):
    writer_stop.set()
    if writer_task:
        await asyncio.wait_for(writer_task, timeout=5)
    adapter_srv.should_exit = True
    adapter_thread.join(timeout=5)
    nats_proc.terminate()
    nats_proc.wait(timeout=10)


async def _start_writer(db_path):
    from orchestrator.nats_client import durable_consume, ensure_stream
    from state.writer import StateWriter

    await ensure_stream(NATS_URL)
    writer = StateWriter(db_path)

    async def _apply(event: dict) -> None:  # durable_consume 要求可 await
        writer.apply(event)

    stop = asyncio.Event()
    task = asyncio.create_task(
        durable_consume("state-writer", _apply, NATS_URL, stop_event=stop))
    return stop, task


# ---------- 场景 A：只读任务自动批准 ----------


@requires_nats
async def test_brain_readonly_auto_approve(tmp_path, monkeypatch):
    tm, agents_path, nats_proc, srv, thread, db_path = _make_harness(
        tmp_path, monkeypatch)
    writer_stop, writer_task = await _start_writer(db_path)
    try:
        from hermes.brain import Hermes

        box: dict = {}

        def handler(n, results, messages):
            tid = box.get("tid")
            if n == 0:
                return ("create_task", {
                    "objective": "调研本地镜像加速方案", "project": "las"})
            if n == 1:
                box["tid"] = results[0]["task_id"]
                return ("delegate_task", {"task_id": results[0]["task_id"],
                                          "agent_id": "codex"})
            if n == 2:
                return ("wait_task", {"task_id": tid, "timeout_seconds": 30})
            if n == 3:
                return ("review_task", {"task_id": tid, "approved": True})
            return "调研完成，结果已验收。"

        llm = ScriptedLLM(handler)
        hermes = Hermes(tm, llm=llm, agents_path=agents_path)
        reply = await hermes.chat("帮我调研一下本地镜像加速方案")

        assert reply == "调研完成，结果已验收。"
        # create → delegate(auto) → wait(completed) → review(accepted)
        assert llm.results[1]["approval"] == "auto"
        assert llm.results[1]["status"] == "delegated"
        assert llm.results[2]["status"] == "completed"
        assert llm.results[3]["status"] == "accepted"
        row = tm.conn.execute("SELECT status FROM tasks WHERE id = ?;",
                              (box["tid"],)).fetchone()
        assert row["status"] == "accepted"
    finally:
        await _stop_harness(nats_proc, srv, thread, writer_stop, writer_task)


# ---------- 场景 B：对话审批流 ----------


@requires_nats
async def test_brain_write_needs_dialog_approval(tmp_path, monkeypatch):
    tm, agents_path, nats_proc, srv, thread, db_path = _make_harness(
        tmp_path, monkeypatch)
    writer_stop, writer_task = await _start_writer(db_path)
    try:
        from hermes.brain import Hermes

        box: dict = {}

        def handler(n, results, messages):
            tid = box.get("tid")
            last_user = next(
                (m["content"] for m in reversed(messages)
                 if m.get("role") == "user"), "")
            approved = last_user == "批准"
            if n == 0:
                return ("create_task", {"objective": "重启 nginx 服务"})
            if n == 1:
                box["tid"] = results[0]["task_id"]
                return ("delegate_task", {"task_id": results[0]["task_id"],
                                          "agent_id": "codex"})
            if n == 2:
                if not approved:
                    return "该操作涉及重启服务，需要您批准。是否执行？"
                return ("approve_and_delegate",
                        {"task_id": tid, "agent_id": "codex",
                         "note": "用户对话内批准"})
            if n == 3:
                return ("wait_task", {"task_id": tid, "timeout_seconds": 30})
            if n == 4:
                return ("review_task", {"task_id": tid, "approved": True})
            return "已重启完成并验收。"

        llm = ScriptedLLM(handler)
        hermes = Hermes(tm, llm=llm, agents_path=agents_path)

        ask = await hermes.chat("帮我重启 nginx 服务")
        assert "批准" in ask
        assert llm.results[1]["status"] == "needs_approval"
        # 未批准前任务不得下发
        row = tm.conn.execute("SELECT status, assigned_to FROM tasks"
                              " WHERE id = ?;", (box["tid"],)).fetchone()
        assert row["status"] == "queued"
        assert row["assigned_to"] is None

        done = await hermes.chat("批准")
        assert done == "已重启完成并验收。"
        assert llm.results[2]["approval"] == "user"
        # 对话即审批：task.approved 事件落库
        ev = tm.conn.execute(
            "SELECT event_type FROM events WHERE task_id = ?"
            " AND event_type = 'task.approved';", (box["tid"],)).fetchone()
        assert ev is not None
        row = tm.conn.execute("SELECT status FROM tasks WHERE id = ?;",
                              (box["tid"],)).fetchone()
        assert row["status"] == "accepted"
    finally:
        await _stop_harness(nats_proc, srv, thread, writer_stop, writer_task)


# ---------- 场景 C：常驻授权（grant → granted → revoke → ask） ----------


@requires_nats
async def test_tools_standing_grant_flow(tmp_path, monkeypatch):
    tm, agents_path, nats_proc, srv, thread, db_path = _make_harness(
        tmp_path, monkeypatch)
    writer_stop, writer_task = await _start_writer(db_path)
    try:
        from hermes.policy import ApprovalPolicy
        from hermes.tools import HermesTools

        tools = HermesTools(tm, ApprovalPolicy(), agents_path)

        # 1. 常驻授权"重启"
        g = await tools.dispatch("grant_operation",
                                 {"pattern": "重启", "note": "常规运维"})
        assert g["status"] == "granted"

        # 2. 命中授权：自动放行 + task.auto_approved 事件
        t1 = await tools.dispatch(
            "create_task", {"objective": "重启 redis 服务"})
        d1 = await tools.dispatch(
            "delegate_task", {"task_id": t1["task_id"], "agent_id": "codex"})
        assert d1["status"] == "delegated"
        assert d1["approval"] == "granted"
        ev = tm.conn.execute(
            "SELECT event_type FROM events WHERE task_id = ?"
            " AND event_type = 'task.auto_approved';",
            (t1["task_id"],)).fetchone()
        assert ev is not None
        w1 = await tools.dispatch(
            "wait_task", {"task_id": t1["task_id"], "timeout_seconds": 30})
        assert w1["status"] == "completed"

        # 3. 撤销后回到 ask
        r = await tools.dispatch("revoke_grant", {"grant_id": g["grant_id"]})
        print("DEBUG revoke ->", r)
        assert r["revoked"] is True
        t2 = await tools.dispatch(
            "create_task", {"objective": "重启 redis 服务"})
        d2 = await tools.dispatch(
            "delegate_task", {"task_id": t2["task_id"], "agent_id": "codex"})
        assert d2["status"] == "needs_approval"

        # 4. never_grant 类不允许常驻授权
        bad = await tools.dispatch("grant_operation", {"pattern": "删除"})
        assert "error" in bad
    finally:
        await _stop_harness(nats_proc, srv, thread, writer_stop, writer_task)


# ---------- 场景 D：动态发现注册（v3 M2） ----------


@requires_nats
async def test_dynamic_discovery_via_heartbeat(tmp_path, monkeypatch):
    """fake adapter 心跳自注册（LAS_AGENT_ENDPOINT + skills）→ hermes
    发现一个静态 yaml 里不存在的 agent 并成功委派。"""
    monkeypatch.setenv("LAS_AGENT_ENDPOINT", ADAPTER_URL)
    # 首条心跳可能早于 stream/consumer 就绪被 spool，缩小间隔快速补发
    monkeypatch.setenv("LAS_HEARTBEAT_INTERVAL", "0.5")
    tm, agents_path, nats_proc, srv, thread, db_path = _make_harness(
        tmp_path, monkeypatch)
    writer_stop, writer_task = await _start_writer(db_path)
    try:
        from hermes.policy import ApprovalPolicy
        from hermes.tools import HermesTools

        tools = HermesTools(tm, ApprovalPolicy(), agents_path)

        # 等心跳经 NATS → StateWriter 落库（首次心跳立即发出）
        deadline = time.monotonic() + 15
        registered = None
        while time.monotonic() < deadline:
            registered = tm.conn.execute(
                "SELECT * FROM agents WHERE id = 'fake';").fetchone()
            if registered and registered["endpoint"]:
                break
            await asyncio.sleep(0.3)
        assert registered and registered["endpoint"] == ADAPTER_URL
        assert "echo" in json.loads(registered["skills_json"])

        # list_agents：fake 在线；codex/kimi 为静态种子
        out = await tools.dispatch("list_agents", {})
        by_id = {a["id"]: a for a in out["agents"]}
        assert by_id["fake"]["status"] == "online"
        assert by_id["codex"]["status"] == "static"

        # 委派给动态发现的 fake（静态 yaml 中没有它）
        t = await tools.dispatch(
            "create_task", {"objective": "分析注册链路连通性"})
        d = await tools.dispatch(
            "delegate_task", {"task_id": t["task_id"], "agent_id": "fake"})
        assert d["status"] == "delegated", d
        w = await tools.dispatch(
            "wait_task", {"task_id": t["task_id"], "timeout_seconds": 30})
        assert w["status"] == "completed"
    finally:
        await _stop_harness(nats_proc, srv, thread, writer_stop, writer_task)
