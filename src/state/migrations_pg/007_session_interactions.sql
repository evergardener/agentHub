-- Migration 007 (PostgreSQL): durable native agent interaction audit

CREATE TABLE IF NOT EXISTS agent_session_interactions (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL REFERENCES collaborations(id),
    task_id TEXT NOT NULL,
    session_binding_id TEXT NOT NULL REFERENCES agent_session_bindings(id),
    agent_id TEXT NOT NULL,
    adapter_interaction_id TEXT NOT NULL,
    native_request_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    action_intent_id TEXT REFERENCES action_intents(id),
    response_json TEXT,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    last_error TEXT,
    UNIQUE (session_binding_id, adapter_interaction_id)
);

CREATE INDEX IF NOT EXISTS agent_session_interactions_pending_idx
    ON agent_session_interactions(status, requested_at);
CREATE INDEX IF NOT EXISTS agent_session_interactions_task_idx
    ON agent_session_interactions(task_id, requested_at);
