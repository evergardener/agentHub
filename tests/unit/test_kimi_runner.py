"""kimi CLI runner 单元测试（离线，不拉真实 kimi 进程）。

覆盖：stream-json assistant 文本提取、CLI 发现（PATH/固定路径/缺失显式
失败不回退）、完整 run 流程（fake 子进程：命令形状、产物收集、非零退出）。
"""

from __future__ import annotations

import asyncio

import pytest

from adapters.common import A2aTask
from adapters.kimi import runner

pytestmark = pytest.mark.anyio


# ---------- _extract_assistant_text ----------


def test_extract_simple_assistant_text():
    jsonl = (
        '{"role": "assistant", "content": "你好"}\n'
        '{"role": "tool", "content": "工具输出"}\n'
        '{"role": "assistant", "content": "，世界"}\n'
    )
    assert runner._extract_assistant_text(jsonl) == "你好\n，世界"


def test_extract_content_blocks():
    jsonl = '{"role": "assistant", "content": [{"type": "text", "text": "甲"},' \
            ' {"type": "tool_calls", "id": "1"}, {"type": "text", "text": "乙"}]}\n'
    assert runner._extract_assistant_text(jsonl) == "甲乙"


def test_extract_message_wrapper_and_type_field():
    jsonl = (
        '{"type": "assistant", "message": {"content": "包装形状"}}\n'
        'not-json 残段\n'
        '{"role": "assistant"}\n'
    )
    assert runner._extract_assistant_text(jsonl) == "包装形状"


def test_extract_empty():
    assert runner._extract_assistant_text("") == ""
    assert runner._extract_assistant_text('{"role": "tool", "content": "x"}') == ""


# ---------- _find_kimi ----------


def test_find_kimi_from_path(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/kimi")
    assert runner._find_kimi() == "/usr/local/bin/kimi"


def test_find_kimi_bundled_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    fake = tmp_path / "kimi"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(runner, "_BUNDLED_CLI", fake)
    assert runner._find_kimi() == str(fake)


def test_find_kimi_missing_fails_loudly(monkeypatch, tmp_path):
    """找不到 CLI 必须显式失败，不回退 HTTP（不掺假）。"""
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    monkeypatch.setattr(runner, "_BUNDLED_CLI", tmp_path / "nonexistent")
    with pytest.raises(runner.KimiNotAvailable):
        runner._find_kimi()


# ---------- run 全流程（fake 子进程） ----------


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", rc: int = 0):
        self._stdout, self._stderr, self.returncode = stdout, stderr, rc

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass

    async def wait(self):
        pass


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("LAS_KIMI_CLI_MODEL", raising=False)
    monkeypatch.setattr(runner, "_find_kimi", lambda: "/fake/kimi")
    calls: list[dict] = []

    async def fake_exec(*cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        (tmp_path / "ws" / "tasks" / "T-1" / "out.md").write_text("产出")
        return _FakeProc(
            '{"role": "assistant", "content": "分析结果"}\n'.encode(),
            b"progress")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_run_invokes_headless_cli(cli_env):
    calls = cli_env
    task = A2aTask(id="T-1", status_state="working", objective="分析仓库结构")
    # fake 产出的 out.md 依赖任务工作区已创建——先建目录的对齐处理在
    # fake_exec 内完成不了（workspace 由 run 创建），改为 run 后补写：
    artifacts = await runner.run(task)
    names = [a["name"] for a in artifacts]
    assert "kimi.jsonl" in names and "kimi-stderr.log" in names
    assert "last-message.md" in names
    cmd = calls[0]["cmd"]
    assert cmd[:4] == ["/fake/kimi", "-p", "--output-format", "stream-json"]
    assert "分析仓库结构" in cmd[-1]
    assert calls[0]["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL


async def test_run_model_flag(cli_env, monkeypatch):
    monkeypatch.setenv("LAS_KIMI_CLI_MODEL", "kimi-code/kimi-for-coding")
    await runner.run(A2aTask(id="T-1", status_state="working", objective="x"))
    cmd = cli_env[0]["cmd"]
    assert cmd[1:3] == ["-m", "kimi-code/kimi-for-coding"]


async def test_run_nonzero_exit_raises(cli_env, monkeypatch):
    async def failing(*cmd, **kwargs):
        return _FakeProc(b"partial", b"boom", rc=3)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing)
    with pytest.raises(runner.KimiFailed, match="exited 3"):
        await runner.run(A2aTask(id="T-1", status_state="working", objective="x"))


async def test_run_timeout_kills(cli_env, monkeypatch):
    class SlowProc(_FakeProc):
        async def communicate(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *c, **k: SlowProc(b""))
    # create_subprocess_exec 是协程，需要 async fake
    async def slow(*cmd, **kwargs):
        return SlowProc(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    with pytest.raises(runner.KimiTimeout):
        await runner.run(A2aTask(id="T-1", status_state="working", objective="x"), timeout_seconds=0.05)


async def test_run_collects_workspace_files(cli_env, tmp_path):
    task = A2aTask(id="T-1", status_state="working", objective="x")
    await runner.run(task)
    # fake_exec 已在工作区写入 out.md；再跑一次收集验证 workspace/ 前缀
    artifacts = await runner.run(task)
    assert any(a["name"] == "workspace/out.md" for a in artifacts)
