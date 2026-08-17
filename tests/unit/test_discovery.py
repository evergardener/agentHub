"""agent 动态发现注册（v3 M2）单元测试。

覆盖：
  - update_heartbeat 携带 endpoint/skills 落库
  - HermesTools._resolve_agents 合并视图（在线覆盖静态 / 离线标记）
  - delegate 门控：offline 拒绝、unknown 拒绝、static 兜底允许（到策略层）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from state.db import CST, init_db


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "state.db")


def _heartbeat(conn, agent_id, endpoint=None, skills=None, lease_seconds=90):
    from orchestrator import state_store

    state_store.update_heartbeat(conn, agent_id,
                                 lease_ttl_seconds=lease_seconds,
                                 endpoint=endpoint, skills=skills)


def _tools(conn, tmp_path, static: dict | None = None):
    import yaml

    from hermes.policy import ApprovalPolicy
    from hermes.tools import HermesTools

    agents_path = tmp_path / "agents.yaml"
    agents_path.write_text(yaml.safe_dump({"agents": static or {}}),
                           encoding="utf-8")
    tm = SimpleNamespace(conn=conn)
    return HermesTools(tm, ApprovalPolicy(), agents_path)


def test_heartbeat_registers_endpoint_and_skills(conn):
    _heartbeat(conn, "codex", endpoint="http://127.0.0.1:8201",
               skills=["coding", "devops"])
    row = conn.execute("SELECT * FROM agents WHERE id = 'codex';").fetchone()
    assert row["status"] == "online"
    assert row["endpoint"] == "http://127.0.0.1:8201"
    assert json.loads(row["skills_json"]) == ["coding", "devops"]
    assert row["lease_expires_at"] > datetime.now(CST).isoformat(
        timespec="seconds")


def test_resolve_merges_online_over_static(conn, tmp_path):
    tools = _tools(conn, tmp_path, static={
        "codex": {"endpoint": "http://static:8201", "skills": ["coding"]}})
    _heartbeat(conn, "codex", endpoint="http://live:8201",
               skills=["coding", "devops"])
    agents = tools._resolve_agents()
    assert agents["codex"]["online"] is True
    assert agents["codex"]["endpoint"] == "http://live:8201"
    assert agents["codex"]["skills"] == ["coding", "devops"]


def test_resolve_marks_offline_and_unknown(conn, tmp_path):
    tools = _tools(conn, tmp_path, static={
        "codex": {"endpoint": "http://static:8201", "skills": []}})
    # 租约已过期的注册：直接写一条过期记录
    from orchestrator import state_store

    state_store.update_heartbeat(conn, "kimi", lease_ttl_seconds=1)
    conn.execute("UPDATE agents SET lease_expires_at = ? WHERE id = 'kimi';",
                 ((datetime.now(CST) - timedelta(seconds=10))
                  .isoformat(timespec="seconds"),))
    conn.commit()
    agents = tools._resolve_agents()
    assert agents["kimi"]["online"] is False
    assert agents["codex"]["online"] is None  # 仅静态配置


def test_delegate_gate_offline_and_unknown(conn, tmp_path):
    tools = _tools(conn, tmp_path, static={
        "codex": {"endpoint": "http://static:8201", "skills": []}})
    from orchestrator import state_store

    state_store.update_heartbeat(conn, "kimi", lease_ttl_seconds=1)
    conn.execute("UPDATE agents SET lease_expires_at = ? WHERE id = 'kimi';",
                 ((datetime.now(CST) - timedelta(seconds=10))
                  .isoformat(timespec="seconds"),))
    conn.commit()

    assert "offline" in tools._agent_or_error("kimi")["error"]
    err = tools._agent_or_error("pi")  # 未安装未注册
    assert "unknown agent" in err["error"]
    assert "codex" in err["known"]  # 静态种子的已知列表
    ok = tools._agent_or_error("codex")  # static 兜底：可用
    assert ok["endpoint"] == "http://static:8201"


def test_list_agents_status_view(conn, tmp_path):
    tools = _tools(conn, tmp_path, static={
        "codex": {"endpoint": "http://static:8201", "skills": ["coding"]}})
    _heartbeat(conn, "kimi", endpoint="http://live:8202", skills=["research"])
    import asyncio

    out = asyncio.run(tools._tool_list_agents())
    by_id = {a["id"]: a for a in out["agents"]}
    assert by_id["kimi"]["status"] == "online"
    assert by_id["kimi"]["endpoint"] == "http://live:8202"
    assert by_id["codex"]["status"] == "static"
