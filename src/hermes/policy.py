"""Hermes 审批策略引擎 — Evolution v3 §6.2 / §6.2.1。

三态决策：
  auto      只读/查询类，直接放行（auto_approve 关键词）
  granted   写操作但命中未撤销的常驻授权（approval_grants）
  ask       需要用户批准（对话内或 Web UI）

never_grant 类（删除/外部发布等）永远不允许常驻授权自动放行，
只能由用户逐次批准。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_PERMISSIONS = Path(__file__).resolve().parents[2] / "config" / "permissions.yaml"

# 内置兜底（permissions.yaml 缺失时）
FALLBACK = {
    "auto_approve": ["查询", "读取", "分析", "检索", "总结", "列出", "状态"],
    "require_user": ["修改", "写入", "创建文件", "部署", "重启", "安装",
                     "删除文件", "提交", "推送"],
    "never_grant": ["删除", "发布", "对外", "生产"],
}


@dataclass
class Decision:
    action: str          # "auto" | "granted" | "ask"
    risk: str            # "read" | "write" | "critical"
    reason: str
    grant_id: int | None = None


class ApprovalPolicy:
    def __init__(self, permissions_path: Path | None = None):
        path = permissions_path or DEFAULT_PERMISSIONS
        if path.exists():
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            cfg = {}
        self.auto_approve = cfg.get("auto_approve") or FALLBACK["auto_approve"]
        self.require_user = cfg.get("require_user") or FALLBACK["require_user"]
        self.never_grant = cfg.get("never_grant") or FALLBACK["never_grant"]

    def classify(self, objective: str) -> str:
        """按关键词粗分风险等级。critical 优先于 write。"""
        if any(k in objective for k in self.never_grant):
            return "critical"
        if any(k in objective for k in self.require_user):
            return "write"
        return "read"

    def decide(self, conn: sqlite3.Connection, objective: str) -> Decision:
        risk = self.classify(objective)
        if risk == "read":
            return Decision("auto", risk, "只读/查询类，自动批准")
        grant = self._match_grant(conn, objective)
        if grant and risk != "critical":
            return Decision("granted", risk,
                            f"命中常驻授权 #{grant['id']}: {grant['pattern']}",
                            grant_id=grant["id"])
        if risk == "critical":
            return Decision("ask", risk,
                            "高危操作（never_grant），必须用户逐次批准")
        return Decision("ask", risk, "写操作，等待用户批准")

    # ---------- grants ----------

    def _match_grant(self, conn: sqlite3.Connection,
                     objective: str) -> sqlite3.Row | None:
        rows = conn.execute(
            "SELECT * FROM approval_grants WHERE revoked_at IS NULL"
            " ORDER BY id;").fetchall()
        for row in rows:
            if row["pattern"] and row["pattern"] in objective:
                return row
        return None

    @staticmethod
    def grant(conn: sqlite3.Connection, pattern: str,
              granted_by: str = "user", note: str = "") -> int:
        cur = conn.execute(
            "INSERT INTO approval_grants (pattern, granted_by, note, created_at)"
            " VALUES (?, ?, ?, ?);",
            (pattern, granted_by, note,
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def revoke(conn: sqlite3.Connection, grant_id: int) -> bool:
        cur = conn.execute(
            "UPDATE approval_grants SET revoked_at = ?"
            " WHERE id = ? AND revoked_at IS NULL;",
            (datetime.now(timezone.utc).isoformat(), grant_id))
        conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def list_grants(conn: sqlite3.Connection,
                    active_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM approval_grants"
        if active_only:
            sql += " WHERE revoked_at IS NULL"
        return conn.execute(sql + " ORDER BY id;").fetchall()
