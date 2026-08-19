-- Migration 007: durable native agent approvals/questions and response audit

CREATE TABLE agent_session_interactions (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_binding_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    adapter_interaction_id TEXT NOT NULL,
    native_request_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    action_intent_id TEXT,
    response_json TEXT,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    last_error TEXT,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id),
    FOREIGN KEY (session_binding_id) REFERENCES agent_session_bindings(id),
    FOREIGN KEY (action_intent_id) REFERENCES action_intents(id),
    UNIQUE (session_binding_id, adapter_interaction_id)
);

CREATE INDEX agent_session_interactions_pending_idx
    ON agent_session_interactions(status, requested_at);
CREATE INDEX agent_session_interactions_task_idx
    ON agent_session_interactions(task_id, requested_at);
