-- Migration 004: 持久协作会话（ADR-0004）

ALTER TABLE tasks ADD COLUMN collaboration_id TEXT;

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    project TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    next_message_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE collaborations (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    phase TEXT NOT NULL DEFAULT 'planning',
    controller TEXT NOT NULL DEFAULT 'hermes',
    context_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE conversation_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    collaboration_id TEXT,
    task_id TEXT,
    agent_id TEXT,
    sender_type TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    recipient_type TEXT,
    recipient_id TEXT,
    message_type TEXT NOT NULL DEFAULT 'message',
    content_json TEXT NOT NULL,
    parent_message_id TEXT,
    based_on_revision INTEGER,
    sequence INTEGER NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'persisted',
    visibility TEXT NOT NULL DEFAULT 'participants',
    redaction_status TEXT NOT NULL DEFAULT 'none',
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id),
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id),
    FOREIGN KEY (parent_message_id) REFERENCES conversation_messages(id),
    UNIQUE (conversation_id, sequence)
);

CREATE TABLE agent_session_bindings (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    native_session_id TEXT,
    adapter_instance_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    resume_capability TEXT NOT NULL DEFAULT 'unknown',
    context_revision INTEGER NOT NULL,
    last_message_seq INTEGER NOT NULL DEFAULT 0,
    context_snapshot_json TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id)
);

CREATE TABLE action_intents (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_binding_id TEXT,
    requested_by_agent_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expected_effects_json TEXT NOT NULL,
    rollback_plan TEXT,
    risk TEXT NOT NULL,
    based_on_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    policy_route TEXT,
    policy_reason TEXT,
    decided_by TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id),
    FOREIGN KEY (session_binding_id) REFERENCES agent_session_bindings(id)
);

CREATE INDEX conversations_project_idx ON conversations(project, updated_at);
CREATE INDEX collaborations_conversation_idx ON collaborations(conversation_id, updated_at);
CREATE INDEX messages_collaboration_idx ON conversation_messages(collaboration_id, sequence);
CREATE INDEX messages_task_idx ON conversation_messages(task_id, sequence);
CREATE UNIQUE INDEX current_agent_session_idx
    ON agent_session_bindings(task_id, agent_id) WHERE is_current = 1;
CREATE INDEX action_intents_status_idx ON action_intents(status, created_at);
CREATE INDEX tasks_collaboration_idx ON tasks(collaboration_id);
