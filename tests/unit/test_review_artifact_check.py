"""review_task 产物核验（防谎报）单元测试 — 2026-08-17 T-20260817-0020 事故修复。

规则：目标声明创建文件，但 artifacts 清单中除运行日志/汇报外没有任何
产出文件时，approved=true 被服务端强制驳回（veto → 返工 working）。
"""

from __future__ import annotations

import pytest

from hermes.policy import ApprovalPolicy
from hermes.tools import HermesTools
from orchestrator import state_store
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio


@pytest.fixture
def tools(tmp_path):
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    return tm, HermesTools(tm, ApprovalPolicy(), tmp_path / "no-agents.yaml")


def _completed_task(tm, objective: str) -> str:
    tid = tm.create_task(objective)
    tm.conn.execute("UPDATE tasks SET status='completed' WHERE id=?;", (tid,))
    tm.conn.commit()
    return tid


def _add_artifact(tm, tid: str, name: str) -> None:
    state_store.add_artifact(tm.conn, task_id=tid, agent_id="codex",
                             name=name, path=f"/x/{name}", sha256="0" * 64)


async def test_veto_when_claimed_file_missing(tools):
    tm, t = tools
    tid = _completed_task(tm, "在任务工作区创建 report.md 文件，写入分析摘要")
    _add_artifact(tm, tid, "codex.log")
    _add_artifact(tm, tid, "last-message.md")

    r = await t.dispatch("review_task",
                         {"task_id": tid, "approved": True, "notes": "ok"})
    assert r["status"] == "working"          # 强制返工
    assert "veto" in r and "谎报" in r["veto"]
    assert state_store.get_task(tm.conn, tid)["status"] == "working"


async def test_approve_passes_with_produced_file(tools):
    tm, t = tools
    tid = _completed_task(tm, "在任务工作区创建 report.md 文件，写入分析摘要")
    _add_artifact(tm, tid, "codex.log")
    _add_artifact(tm, tid, "workspace/report.md")

    r = await t.dispatch("review_task", {"task_id": tid, "approved": True})
    assert r["status"] == "accepted"
    assert "veto" not in r


async def test_no_veto_for_non_file_objective(tools):
    tm, t = tools
    tid = _completed_task(tm, "分析三种部署拓扑并在对话中汇报要点")
    _add_artifact(tm, tid, "codex.log")  # 纯分析任务只有日志也正常

    r = await t.dispatch("review_task", {"task_id": tid, "approved": True})
    assert r["status"] == "accepted"


async def test_report_artifact_counts_as_product(tools):
    tm, t = tools
    # kimi 类：目标提到生成文件，产物是 report 类型的 analysis.md
    tid = _completed_task(tm, "生成 analysis.md 文件保存调研结论")
    _add_artifact(tm, tid, "analysis.md")

    r = await t.dispatch("review_task", {"task_id": tid, "approved": True})
    assert r["status"] == "accepted"


async def test_get_task_artifacts(tools):
    tm, t = tools
    tid = _completed_task(tm, "探查工作区")
    _add_artifact(tm, tid, "codex.log")
    _add_artifact(tm, tid, "workspace/hello.txt")

    r = await t.dispatch("get_task_artifacts", {"task_id": tid})
    names = [a["name"] for a in r["artifacts"]]
    assert names == ["codex.log", "workspace/hello.txt"]


async def test_reject_still_works(tools):
    tm, t = tools
    tid = _completed_task(tm, "在任务工作区创建 x.md 文件")
    _add_artifact(tm, tid, "workspace/x.md")
    r = await t.dispatch("review_task",
                         {"task_id": tid, "approved": False, "notes": "不合格"})
    assert r["status"] == "working"
    assert "veto" not in r
