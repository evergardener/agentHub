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


def test_strict_dispatch_does_not_use_keyword_classification_as_authority(
        policy, conn):
    d = policy.decide(
        conn, "检查并清理缓存", require_structured_read=True)

    assert d.action == "ask"
    assert d.risk == "unknown"
    assert "access_mode=read" in d.reason


def test_strict_dispatch_accepts_model_declared_read_capability_but_not_write(
        policy, conn):
    read = policy.decide(
        conn, "核验 TLS 证书有效期", access_mode="read",
        require_structured_read=True)
    write = policy.decide(
        conn, "检查后执行 kubectl apply -f app.yaml", access_mode="read",
        require_structured_read=True)

    assert read.action == "auto" and read.risk == "read"
    assert write.action == "ask" and write.risk == "write"


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


def test_chinese_docker_status_report_does_not_treat_status_labels_as_writes(
        policy, conn,
):
    objective = (
        "只读获取当前 Docker 容器状态。核验 Docker 引擎可用性，列出容器名称、"
        "镜像、运行状态和健康状态，并标注停止、重启或不健康容器。"
        "不得修改配置、数据、容器或服务状态。输出简洁中文报告。"
    )
    d = policy.decide(conn, objective)
    assert d.action == "auto" and d.risk == "read"


def test_read_capability_is_explicit_but_does_not_bypass_write_intent(
        policy, conn,
):
    d = policy.decide(
        conn, "只读检查后重启容器", access_mode="read")
    assert d.action == "ask" and d.risk == "write"


def test_read_capability_dispatches_safe_objective_without_keyword_match(
        policy, conn,
):
    assert policy.classify("核验 TLS 证书有效期") == "unknown"

    d = policy.decide(
        conn, "核验 TLS 证书有效期", access_mode="read")

    assert d.action == "auto" and d.risk == "read"


@pytest.mark.parametrize(
    "command",
    ["git checkout -- app.py", "touch marker", "mkdir reports", "sed -i old new"],
)
def test_read_capability_allows_known_commands_only_when_chinese_negated(
        policy, conn, command):
    d = policy.decide(
        conn, f"核验证书有效期；不得执行 {command}", access_mode="read")
    assert d.action == "auto" and d.risk == "read"


@pytest.mark.parametrize(
    ("objective", "risk"),
    [("只读检查，但执行 rm -rf /tmp/cache", "critical"),
     ("只读检查，但执行 docker build .", "write"),
     ("只读检查，但执行 git commit -am fix", "write")],
)
def test_read_capability_does_not_bypass_known_mutating_commands(
        policy, conn, objective, risk):
    d = policy.decide(conn, objective, access_mode="read")
    assert d.action == "ask" and d.risk == risk


@pytest.mark.parametrize("access_mode", ["write", "readonly", ""])
def test_unknown_access_mode_fails_closed(policy, conn, access_mode):
    with pytest.raises(ValueError, match="access_mode"):
        policy.decide(conn, "查询容器状态", access_mode=access_mode)


@pytest.mark.parametrize(
    ("prefix", "separator"),
    [("不", "、"), ("不得", "，"), ("禁止", ","), ("不得", "/"),
     ("禁止", "或")],
)
def test_chinese_parallel_negation_does_not_trigger_high_risk(
    policy, conn, prefix, separator,
):
    objective = (
        f"严格只读检查容器状态；{prefix}重启{separator}停止{separator}删除"
        "容器或服务；仅报告结果。"
    )
    d = policy.decide(conn, objective)
    assert d.action == "auto" and d.risk == "read"


def test_chinese_parallel_negation_allows_object_bearing_operations(
        policy, conn,
):
    objective = "严格只读检查；不得重启容器、停止服务、删除容器；仅报告结果。"
    d = policy.decide(conn, objective)
    assert d.action == "auto" and d.risk == "read"


def test_chinese_parallel_negation_allows_explicit_any_scope(policy, conn):
    objective = (
        "只读任务。运行一次可审计的 docker ps，仅返回当前正在运行的容器"
        "名称列表及其数量。明确禁止任何写入、重启、停止、删除容器，"
        "禁止任何 docker exec，禁止任何任务验收（tasks/accept）或状态"
        "变更操作。任务约束：只读、无副作用、无状态改变。"
    )
    d = policy.decide(conn, objective)
    assert d.action == "auto" and d.risk == "read"


def test_chinese_mixed_negative_file_constraints_keep_positive_write_risk(
        policy, conn):
    objective = (
        "仅更新 marker.txt 并完成写入验证。"
        "不得读取、创建、修改、移动或删除其他文件；"
        "不得提交、推送、部署、删除、重启或安装依赖；"
        "完成写入后重新读取 marker.txt。"
    )

    decision = policy.decide(conn, objective)

    assert decision.action == "ask"
    assert decision.risk == "write"


def test_chinese_mixed_negative_file_constraints_do_not_hide_positive_delete(
        policy, conn):
    objective = (
        "不得读取、创建、修改、移动或删除其他文件，"
        "但必须删除 marker.txt。"
    )

    decision = policy.decide(conn, objective)

    assert decision.action == "ask"
    assert decision.risk == "critical"


def test_chinese_negation_strips_english_docker_operation_list(
        policy, conn,
):
    objective = (
        "严格只读检查容器状态；不得执行 "
        "docker restart/start/stop/rm/kill/compose up/down；"
        "仅执行 docker ps 并报告结果。"
    )
    d = policy.decide(conn, objective)
    assert d.action == "auto" and d.risk == "read"


@pytest.mark.parametrize(
    "command",
    [
        "docker restart grafana",
        "docker start grafana",
        "docker stop grafana",
        "docker rm grafana",
        "docker compose up",
        "docker compose down",
    ],
)
def test_positive_docker_lifecycle_commands_remain_critical(
        policy, conn, command,
):
    d = policy.decide(conn, f"执行 {command}")
    assert d.action == "ask" and d.risk == "critical"


def test_unparseable_english_docker_negation_fails_closed_with_diagnostic(
        policy, conn,
):
    d = policy.decide(
        conn,
        "严格只读检查；不得执行 docker restart/unknown-operation；"
        "仅报告结果。",
    )
    assert d.action == "ask" and d.risk == "unknown"
    assert "只读声明无法解析" in d.reason


def test_positive_docker_command_after_negation_remains_critical(
        policy, conn,
):
    d = policy.decide(
        conn,
        "只读检查；不得执行 docker restart/start/stop，"
        "但必须执行 docker stop。",
    )
    assert d.action == "ask" and d.risk == "critical"


def test_unparseable_chinese_negation_fails_closed_with_diagnostic(policy, conn):
    d = policy.decide(conn, "严格只读检查；不得重启;停止;删除；仅报告结果。")
    assert d.action == "ask" and d.risk == "unknown"
    assert "只读声明无法解析" in d.reason


def test_contrastive_positive_delete_remains_critical(policy, conn):
    d = policy.decide(conn, "只读检查；不得重启，但必须删除容器。")
    assert d.action == "ask" and d.risk == "critical"


def test_chinese_parallel_non_negated_operations_still_require_approval(
    policy, conn,
):
    d = policy.decide(conn, "检查容器状态后重启、停止或删除容器")
    assert d.action == "ask" and d.risk == "critical"


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
