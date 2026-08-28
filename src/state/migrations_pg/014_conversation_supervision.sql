-- Migration 014: route durable WebUI messages back to the originating Hermes.

ALTER TABLE supervision_outbox ADD COLUMN IF NOT EXISTS message_id TEXT;

CREATE TABLE IF NOT EXISTS supervision_conversation_routes (
    collaboration_id TEXT PRIMARY KEY,
    peer TEXT NOT NULL,
    context_id TEXT NOT NULL,
    watch_id TEXT NOT NULL REFERENCES supervision_watches(id),
    created_at TEXT NOT NULL,
    UNIQUE (peer, context_id)
);

CREATE INDEX IF NOT EXISTS supervision_outbox_message_idx
    ON supervision_outbox(message_id, status);
