-- Migration 012: durable Hermes supervision watches and notification outbox.

CREATE TABLE IF NOT EXISTS supervision_watches (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    peer TEXT NOT NULL,
    context_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (peer, task_id)
);

CREATE TABLE IF NOT EXISTS supervision_outbox (
    id TEXT PRIMARY KEY,
    watch_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    internal_status TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    UNIQUE (watch_id, dedupe_key),
    FOREIGN KEY (watch_id) REFERENCES supervision_watches(id)
);

CREATE INDEX IF NOT EXISTS supervision_watches_peer_status_idx
    ON supervision_watches(peer, status, updated_at);
CREATE INDEX IF NOT EXISTS supervision_outbox_delivery_idx
    ON supervision_outbox(status, available_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS supervision_outbox_watch_idx
    ON supervision_outbox(watch_id, status, created_at);
