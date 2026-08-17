"""Agent Registry — 设计文档 §4 / §11 registry.py。

加载 config/agents.yaml，按 skill 过滤候选 Agent，路由时做容量检查
（max_concurrent_tasks vs SQLite 中该 Agent 的在途任务数）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from common import config as cfg

DEFAULT_CONFIG = cfg.workspace() / "config" / "agents.yaml"


@dataclass
class AgentInfo:
    id: str
    role: str
    enabled: bool
    endpoint: str
    protocol: str = "a2a"
    max_concurrent_tasks: int = 1
    on_lease_expired: str = "requeue"
    skills: list[str] = field(default_factory=list)


class Registry:
    def __init__(self, config_path: str | Path = DEFAULT_CONFIG):
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self._agents: dict[str, AgentInfo] = {}
        for agent_id, spec in (data.get("agents") or {}).items():
            self._agents[agent_id] = AgentInfo(
                id=agent_id,
                role=spec.get("role", "worker"),
                enabled=bool(spec.get("enabled", False)),
                endpoint=spec.get("endpoint", ""),
                protocol=spec.get("protocol", "a2a"),
                max_concurrent_tasks=int(spec.get("max_concurrent_tasks", 1)),
                on_lease_expired=spec.get("on_lease_expired", "requeue"),
                skills=list(spec.get("skills", [])),
            )

    def get(self, agent_id: str) -> AgentInfo | None:
        return self._agents.get(agent_id)

    def list_agents(self, enabled_only: bool = False) -> list[AgentInfo]:
        agents = list(self._agents.values())
        if enabled_only:
            agents = [a for a in agents if a.enabled]
        return agents

    def in_flight(self, conn: sqlite3.Connection, agent_id: str) -> int:
        """该 Agent 当前在途任务数（assigned/working/blocked）。"""
        return conn.execute(
            "SELECT COUNT(*) FROM tasks"
            " WHERE assigned_to = ? AND status IN ('assigned','working','blocked');",
            (agent_id,),
        ).fetchone()[0]

    def find_agent_by_skill(
        self, skill: str, conn: sqlite3.Connection | None = None
    ) -> list[AgentInfo]:
        """按技能找候选 Agent；给定连接时过滤容量已满者（§12 Capacity Check）。"""
        candidates = [
            a for a in self._agents.values()
            if a.enabled and skill in a.skills
        ]
        if conn is not None:
            candidates = [
                a for a in candidates
                if self.in_flight(conn, a.id) < a.max_concurrent_tasks
            ]
        return candidates
