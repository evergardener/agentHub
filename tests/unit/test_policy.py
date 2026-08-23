"""审批策略引擎（Evolution v3 §6.2/§6.2.1）。"""

from __future__ import annotations

import pytest

from hermes.policy import ApprovalPolicy
from state.db import init_db


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "state.db")


@pytest.fixture
def policy():
    return ApprovalPolicy()  # 用仓库内 config/permissions.yaml


def test_classify_read_write_critical(policy):
    assert policy.classify("调研一下 SQLite 的优缺点") == "read"
    assert policy.classify("重启 nginx 服务") == "write"
    assert policy.classify("删除生产数据库") == "critical"
    assert policy.classify("adjust the runtime") == "unknown"


def test_read_auto_approved(policy, conn):
    d = policy.decide(conn, "查询当前任务状态")
    assert d.action == "auto" and d.risk == "read"


def test_english_read_only_objective_is_auto_approved(policy, conn):
    d = policy.decide(
        conn, "Strictly read-only inspection: check Git status and report it; "
              "do not modify any files")
    assert d.action == "auto" and d.risk == "read"


def test_chinese_read_only_command_plan_with_negated_writes_is_auto_approved(
    policy, conn,
):
    objective = (
        "只读检查任务工作区：执行 pwd；执行 git status --short；"
        "执行 rg -n needle .。不得修改、创建、删除任何文件；"
        "仅报告命令输出以及是否发生写入。"
    )
    d = policy.decide(conn, objective)
    assert d.action == "auto" and d.risk == "read"


def test_chinese_non_negated_write_and_critical_terms_still_require_approval(
    policy, conn,
):
    assert policy.classify("创建文件并写入报告") == "write"
    assert policy.classify("删除生产数据库") == "critical"


def test_english_write_requires_approval(policy, conn):
    d = policy.decide(conn, "Modify the runtime")
    assert d.action == "ask" and d.risk == "write"


def test_write_needs_approval_without_grant(policy, conn):
    d = policy.decide(conn, "重启 nginx")
    assert d.action == "ask" and d.risk == "write"


def test_unknown_operation_fails_closed(policy, conn):
    d = policy.decide(conn, "adjust the runtime")
    assert d.action == "ask" and d.risk == "unknown"
    assert "fail-closed" in d.reason


def test_grant_enables_auto(policy, conn):
    gid = ApprovalPolicy.grant(conn, "重启", note="用户授权")
    d = policy.decide(conn, "重启 redis")
    assert d.action == "granted" and d.grant_id == gid


def test_revoke_disables_grant(policy, conn):
    gid = ApprovalPolicy.grant(conn, "重启")
    assert ApprovalPolicy.revoke(conn, gid)
    d = policy.decide(conn, "重启 nginx")
    assert d.action == "ask"


def test_critical_never_granted(policy, conn):
    ApprovalPolicy.grant(conn, "删除日志")  # 即使授权了相近 pattern
    d = policy.decide(conn, "删除生产数据")
    assert d.action == "ask" and d.risk == "critical"


def test_grant_list_active_only(policy, conn):
    g1 = ApprovalPolicy.grant(conn, "重启")
    g2 = ApprovalPolicy.grant(conn, "部署")
    ApprovalPolicy.revoke(conn, g1)
    active = ApprovalPolicy.list_grants(conn, active_only=True)
    assert [r["id"] for r in active] == [g2]
    assert len(ApprovalPolicy.list_grants(conn, active_only=False)) == 2
