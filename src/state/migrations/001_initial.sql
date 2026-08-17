-- 001_initial.sql — 设计文档 §6
-- 系统当前状态的唯一事实源。写入规则见 §22.3（单一写者原则）。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    endpoint TEXT,
    protocol TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    skills_json TEXT,
    max_concurrent_tasks INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    parent_id TEXT,
    root_id TEXT,
    project TEXT,
    created_by TEXT,
    assigned_to TEXT,
    status TEXT NOT NULL,
    priority TEXT,
    objective TEXT NOT NULL,
    depends_on_json TEXT,
    constraints_json TEXT,
    timeout_seconds INTEGER,
    max_retries INTEGER NOT NULL DEFAULT 2,
    idempotency_key TEXT UNIQUE,
    result_summary TEXT,
    error_message TEXT,
    review_json TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    type TEXT,
    name TEXT,
    path TEXT,
    sha256 TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT,
    trace_id TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    task_id TEXT,
    agent_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
