-- Migration 002: 常驻授权（Evolution v3 §6.2.1 standing grants）
CREATE TABLE IF NOT EXISTS approval_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,            -- 操作类型关键词（包含匹配）
    granted_by TEXT NOT NULL DEFAULT 'user',
    note TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT                   -- 非空即已撤销
);
