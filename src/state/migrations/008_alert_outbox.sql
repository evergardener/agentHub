-- Migration 008: durable alert inbox/outbox and delivery audit.

CREATE TABLE alerts (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    task_id TEXT,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    delivered_at TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    next_delivery_at TEXT NOT NULL,
    last_delivery_error TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    acknowledgement_note TEXT
);

CREATE INDEX alerts_status_delivery_idx
    ON alerts(status, delivered_at, next_delivery_at);
CREATE INDEX alerts_task_idx ON alerts(task_id, last_seen_at);
