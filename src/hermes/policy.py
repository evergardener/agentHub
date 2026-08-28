"""Hermes 审批策略引擎 — Evolution v3 §6.2 / §6.2.1。

三态决策：
  auto      明确命中只读/查询类，直接放行（auto_approve 关键词）
  granted   写操作但命中未撤销的常驻授权（approval_grants）
  ask       需要用户批准（对话内或 Web UI）

never_grant 类（删除/外部发布等）永远不允许常驻授权自动放行，
只能由用户逐次批准。未命中任何规则的操作分类为 unknown，并按
fail-closed 原则要求批准，不能默认视为只读。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_PERMISSIONS = Path(__file__).resolve().parents[2] / "config" / "permissions.yaml"

# 内置兜底（permissions.yaml 缺失时）
FALLBACK = {
    "auto_approve": [
        "查询", "读取", "分析", "检索", "总结", "列出", "状态",
        "只读", "检查",
        "read-only", "read only", "inspect", "check", "report", "list",
        "status", "summarize", "analyse", "analyze", "query", "review",
    ],
    "require_user": ["修改", "写入", "创建文件", "部署", "重启", "安装",
                     "删除文件", "提交", "推送", "modify", "write",
                     "create file", "deploy", "restart", "install", "pull",
                     "commit", "push"],
    "never_grant": ["删除", "发布", "对外", "生产", "delete", "publish",
                    "external"],
}

_NEGATED_ENGLISH_WRITE = re.compile(
    r"\b(?:do not|don't|must not|never|without)\s+(?:directly\s+)?"
    r"(?:modify|modifying|write|writing|create|creating|delete|deleting|"
    r"deploy|deploying|restart|restarting|install|installing|pull|pulling|"
    r"commit|committing|push|pushing|publish|publishing)\b",
)
# A Chinese objective commonly puts the command language after the negation,
# for example ``不得执行 docker restart/start/stop``.  Keep this parser
# deliberately bounded: it only removes an allow-listed Docker lifecycle or
# write term when it is inside a Chinese negated list.  A positive command is
# therefore left in the effective objective for the risk classifier below.
_CHINESE_NEGATED_ENGLISH_OPERATIONS = (
    "docker compose up", "docker compose down",
    "docker restart", "docker start", "docker stop", "docker rm",
    "docker kill", "docker run", "docker exec", "docker pause",
    "docker unpause", "docker build", "docker pull",
    "git reset", "git checkout", "git commit", "git push",
    "sed -i", "touch", "mkdir", "mv", "cp", "tee", "chmod", "chown",
    "compose up", "compose down", "up", "down",
    "restart", "restarting", "start", "starting", "stop", "stopping",
    "rm", "kill", "run", "exec", "pause", "unpause",
    "modify", "modifying", "write", "writing", "create", "creating",
    "delete", "deleting", "remove", "removing", "deploy", "deploying",
    "install", "installing", "pull", "pulling", "commit", "committing",
    "push", "pushing", "publish", "publishing",
)
_CHINESE_NEGATED_ENGLISH_OPERATION_PATTERN = re.compile(
    r"(?:" + "|".join(
        re.escape(term) for term in sorted(
            _CHINESE_NEGATED_ENGLISH_OPERATIONS, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)
_CHINESE_NEGATED_ENGLISH_SEPARATOR = re.compile(
    r"(?:\s*(?:、|，|,|/)\s*(?:或|or|and)?\s*"
    r"|\s+(?:或|or|and)\s+)",
    re.IGNORECASE,
)
_DOCKER_MUTATING_COMMAND = re.compile(
    r"\bdocker\s+(?:"
    r"(?:compose\s+)?(?:restart|start|stop|rm|kill|run|exec|pause|"
    r"unpause|up|down)\b|"
    r"(?:container|image|volume|network)\s+rm\b|"
    r"system\s+prune\b)",
    re.IGNORECASE,
)
_MUTATING_DELETE_COMMAND = re.compile(
    r"(?<![\w-])(?:rm|rmdir|unlink|git\s+(?:reset|checkout)|"
    r"docker\s+(?:container\s+)?(?:rm|kill)|docker\s+system\s+prune)\b",
    re.IGNORECASE,
)
_MUTATING_WRITE_COMMAND = re.compile(
    r"(?<![\w-])(?:touch|mkdir|mv|cp|tee|chmod|chown|"
    r"sed\s+-i|git\s+(?:commit|push)|docker\s+(?:build|pull|run)|"
    r"kubectl\s+(?:apply|create|patch|replace|set|scale|rollout)|"
    r"helm\s+(?:install|upgrade|rollback))\b",
    re.IGNORECASE,
)
_NEGATED_CHINESE_RISK_PHRASES = (
    "是否发生写入", "是否有写入",
)

_ACCESS_MODES = frozenset({"read"})


def normalize_access_mode(value: str | None) -> str | None:
    """Validate the optional creation-time capability declaration.

    ``read`` is an intent hint for the initial dispatch only.  It never
    grants runtime authority: native ActionIntent policy still decides every
    command or filesystem operation.  Unknown modes are rejected rather than
    interpreted permissively.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("access_mode must be 'read' when provided")
    mode = value.strip().casefold()
    if mode not in _ACCESS_MODES:
        raise ValueError(
            "unsupported access_mode; only 'read' is supported")
    return mode


# Lifecycle words can describe the state being reported rather than an
# operation requested from the worker.  For example, ``标注停止、重启或不健康
# 容器`` is a read-only Docker inventory, not a request to stop or restart a
# container.  Keep this contextual exception narrow: it only removes a
# bounded, enumerated status list following a reporting verb and only when the
# list ends at a known resource noun.  A command such as ``检查后重启容器``
# therefore remains a write/critical request.
_READONLY_STATUS_CONTEXT = re.compile(
    r"(?P<verb>标注|标记|区分|分类|筛选|过滤|统计|汇总|显示|列出|报告|识别|"
    r"检测|发现|记录|指出|说明)\s*"
    r"(?:已|当前|正在)?"
    r"(?:停止|重启|启动|暂停|运行|退出|不健康|健康|异常)"
    r"(?:\s*(?:、|，|,|/|或|和|以及)\s*"
    r"(?:已|当前|正在)?"
    r"(?:停止|重启|启动|暂停|运行|退出|不健康|健康|异常))*"
    r"\s*(?:容器|服务|实例|进程|节点|主机|状态)",
    re.IGNORECASE,
)


def _strip_readonly_status_descriptors(text: str) -> str:
    """Remove lifecycle terms used solely as read-only report labels."""
    return _READONLY_STATUS_CONTEXT.sub(
        lambda match: match.group("verb"), text)


def _strip_negated_chinese_english_operations(text: str) -> tuple[str, bool]:
    """Strip bounded English/Docker operations under Chinese negation.

    This handles both repeated command names and compact lists such as
    ``不得执行 docker restart/start/stop``.  It intentionally reports a
    partially parsed list as ambiguous so an unknown English term cannot be
    silently treated as a read-only constraint.
    """
    operations = _CHINESE_NEGATED_ENGLISH_OPERATION_PATTERN.pattern
    scope = r"(?:(?:任何|任意)\s*)?"
    prefix = (
        r"(?:不得|禁止|不要|不)\s*" + scope +
        r"(?:(?:执行|运行|调用)\s*)?" + scope
    )
    # Targets/options after an operation are retained as ordinary objective
    # text, but bounded so contrastive clauses and list separators terminate
    # the match.  This supports e.g. ``docker restart 容器/docker stop 服务``.
    object_fragment = (
        r"(?:(?!(?:但|但是|必须|需要|需|然而|而且|"
        r"[、，,/;；。！？\n]|或|和|以及))"
        r"[A-Za-z0-9_.:/@+\-=\u4e00-\u9fff \t])*?"
    )
    pattern = re.compile(
        prefix + operations + object_fragment +
        rf"(?:{_CHINESE_NEGATED_ENGLISH_SEPARATOR.pattern}"
        + operations + object_fragment + r")*",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    effective = pattern.sub("", text)
    if not matches:
        return text, False

    # A separator followed by an unrecognised item means that the list was
    # only partially parsed.  Fail closed with a diagnostic instead of
    # claiming that the objective requested a high-risk operation.
    clause_boundary = re.compile(
        r"(?:不得|禁止|不要|不|但|但是|必须|需要|需|然而|而且|"
        r"仅|只|检查|读取|查看|报告|也|并|同时|并且|以及)",
    )
    ambiguous = False
    for match in matches:
        suffix = text[match.end():]
        following = re.match(
            r"\s*(?:、|，|,|/|;|；)\s*(.*)", suffix, re.DOTALL)
        if not following:
            continue
        tail = following.group(1)
        if not tail or clause_boundary.match(tail):
            continue
        if _CHINESE_NEGATED_ENGLISH_OPERATION_PATTERN.match(tail):
            ambiguous = True
            break
        # An ASCII/Chinese word immediately after a list separator is most
        # likely an unrecognised operation; punctuation-only tails are safe.
        if re.match(r"[A-Za-z\u4e00-\u9fff]", tail):
            ambiguous = True
            break
    return effective, ambiguous


def _strip_negated_chinese_operations(
        text: str, operation_terms: list[object]) -> tuple[str, bool]:
    """Remove Chinese negated operation lists before keyword classification.

    A statement such as ``不得重启、停止、删除容器`` describes constraints,
    not requested operations.  Keep the parser deliberately narrow: only
    configured Chinese write/critical terms (plus the common ``停止`` term)
    can be removed, and only when they follow ``不``/``不得``/``不要``/``禁止``.
    Unknown terms therefore remain fail-closed.
    """
    terms = {
        str(term).casefold() for term in operation_terms
        if any("\u4e00" <= char <= "\u9fff" for char in str(term))
        and str(term) != "生产"
    }
    terms.update(("创建", "停止", "对外发布"))
    ordered_terms = sorted(terms, key=len, reverse=True)
    if not ordered_terms:
        return text, False

    operations = "|".join(re.escape(term) for term in ordered_terms)
    # Support Chinese comma, ASCII comma, enumeration mark, slash, and
    # conjunctions.  The optional ``或`` also handles ``、或``/``, 或`` forms.
    separator = (
        r"(?:\s*(?:、|，|,|/|和|以及)\s*(?:或\s*)?"
        r"|\s*或\s*)"
    )
    # A target may appear after each operation in a list, e.g.
    # ``不得重启容器、停止服务、删除容器``.  Keep this fragment narrow so a
    # contrastive positive clause such as ``但必须删除`` is never swallowed.
    object_fragment = (
        r"(?:(?!(?:但|但是|必须|需要|需|然而|而且|"
        r"[、，,/;；。！？\n]|或|和|以及))"
        r"[\u4e00-\u9fffA-Za-z0-9_.:\- \t])*?"
    )
    pattern = re.compile(
        rf"(?:不得|禁止|不要|不)\s*(?:(?:任何|任意)\s*)?"
        rf"(?:(?:执行|运行|调用)\s*)?(?:(?:任何|任意)\s*)?"
        rf"(?:{operations})"
        rf"(?:{object_fragment}{separator}(?:{operations}))*"
    )
    matches = list(pattern.finditer(text))
    effective = pattern.sub("", text)

    # If a known negated operation is followed by a separator and an unknown
    # item, the list was only partially parsed.  Mark it for fail-closed
    # diagnostics instead of leaving a misleading critical classification.
    known_prefix = re.compile(rf"(?:{operations})")
    clause_boundary = re.compile(
        r"(?:不得|禁止|不要|不|但|但是|必须|需要|需|然而|而且|"
        r"仅|只|检查|读取|查看|报告)"
    )
    ambiguous = False
    for match in matches:
        suffix = text[match.end():]
        following = re.match(
            r"\s*(?:、|，|,|/|;|；)\s*(.*)", suffix, re.DOTALL)
        if not following:
            continue
        tail = following.group(1)
        if not tail or clause_boundary.match(tail):
            continue
        if known_prefix.match(tail) or any(
                "\u4e00" <= char <= "\u9fff" for char in tail[:1]):
            ambiguous = True
            break
    return effective, ambiguous


@dataclass
class Decision:
    action: str          # "auto" | "granted" | "ask"
    risk: str            # "read" | "write" | "critical" | "unknown"
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

    def _classify(self, objective: str,
                  access_mode: str | None = None) -> tuple[str, bool]:
        """返回风险等级及中文否定列表是否未完整解析。"""
        access_mode = normalize_access_mode(access_mode)
        normalized = objective.casefold()
        effective = _NEGATED_ENGLISH_WRITE.sub("", normalized)
        effective, english_negation_ambiguous = (
            _strip_negated_chinese_english_operations(effective))
        effective, negation_ambiguous = _strip_negated_chinese_operations(
            effective, [*self.require_user, *self.never_grant])
        negation_ambiguous = (
            english_negation_ambiguous or negation_ambiguous)
        for phrase in _NEGATED_CHINESE_RISK_PHRASES:
            effective = effective.replace(phrase, "")
        effective = _strip_readonly_status_descriptors(effective)
        if _DOCKER_MUTATING_COMMAND.search(effective):
            return "critical", negation_ambiguous
        if _MUTATING_DELETE_COMMAND.search(effective):
            return "critical", negation_ambiguous
        if _MUTATING_WRITE_COMMAND.search(effective):
            return "write", negation_ambiguous
        if any(str(k).casefold() in effective for k in self.never_grant):
            return "critical", negation_ambiguous
        if any(str(k).casefold() in effective for k in self.require_user):
            return "write", negation_ambiguous
        if any(str(k).casefold() in effective for k in self.auto_approve):
            return "read", negation_ambiguous
        # An explicit read capability is authoritative for creation-time
        # dispatch only when no known mutating intent was found above.  The
        # worker's concrete native commands and filesystem operations still
        # pass through ActionIntent policy independently at runtime.
        if access_mode == "read":
            return "read", negation_ambiguous
        return "unknown", negation_ambiguous

    def classify(self, objective: str, *, access_mode: str | None = None) -> str:
        """按关键词粗分风险等级；未知操作 fail-closed。"""
        risk, negation_ambiguous = self._classify(
            objective, access_mode=access_mode)
        if negation_ambiguous:
            return "unknown"
        return risk

    def decide(self, conn: sqlite3.Connection, objective: str, *,
               access_mode: str | None = None,
               require_structured_read: bool = False) -> Decision:
        access_mode = normalize_access_mode(access_mode)
        risk, negation_ambiguous = self._classify(
            objective, access_mode=access_mode)
        if negation_ambiguous:
            return Decision("ask", "unknown", "只读声明无法解析，按 fail-closed 原则等待用户批准")
        if risk == "read":
            if require_structured_read and access_mode != "read":
                return Decision(
                    "ask", "unknown",
                    "自然语言只读分类仅供兼容提示；缺少结构化 access_mode=read，"
                    "按 fail-closed 原则等待用户批准",
                )
            reason = "只读/查询类，自动批准"
            if access_mode == "read":
                reason = "显式 read capability 且未发现写入意图，自动批准初始委派"
            return Decision("auto", risk, reason)
        grant = self._match_grant(conn, objective)
        if grant and risk != "critical":
            return Decision("granted", risk,
                            f"命中常驻授权 #{grant['id']}: {grant['pattern']}",
                            grant_id=grant["id"])
        if risk == "critical":
            return Decision("ask", risk,
                            "高危操作（never_grant），必须用户逐次批准")
        if risk == "unknown":
            if access_mode == "read":
                return Decision(
                    "ask", risk,
                    "显式 read capability 但目标含未知操作，按 fail-closed 原则等待用户批准",
                )
            return Decision("ask", risk,
                            "未识别操作，按 fail-closed 原则等待用户批准")
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
            " VALUES (?, ?, ?, ?) RETURNING id;",
            (pattern, granted_by, note,
             datetime.now(timezone.utc).isoformat()))
        grant_id = cur.fetchone()[0]
        conn.commit()
        return grant_id

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
