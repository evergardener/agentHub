-- Migration 017 (PostgreSQL): durable, bounded Hermes response drafts.
--
-- A draft is deliberately separate from conversation_messages.  The latter
-- remains the authoritative transcript and is written only by
-- conversations/respond after the final response is accepted.  This table
-- stores the latest cumulative prefix, not one row per token.

CREATE TABLE IF NOT EXISTS conversation_stream_drafts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    collaboration_id TEXT NOT NULL REFERENCES collaborations(id),
    message_id TEXT NOT NULL REFERENCES conversation_messages(id),
    peer TEXT NOT NULL,
    context_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'streaming'
        CHECK (status IN ('streaming', 'finished', 'aborted')),
    text_prefix TEXT NOT NULL DEFAULT ''
        CHECK (char_length(text_prefix) <= 20000),
    last_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    abort_reason TEXT,
    response_message_id TEXT REFERENCES conversation_messages(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finalized_at TEXT,
    UNIQUE (peer, context_id, message_id)
);

CREATE INDEX IF NOT EXISTS conversation_stream_drafts_context_idx
    ON conversation_stream_drafts(peer, context_id, updated_at);
CREATE INDEX IF NOT EXISTS conversation_stream_drafts_message_idx
    ON conversation_stream_drafts(message_id, status, updated_at);
