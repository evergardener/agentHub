-- Migration 006 (PostgreSQL): durable adapter/native session recovery metadata

ALTER TABLE agent_session_bindings ADD COLUMN IF NOT EXISTS adapter_session_id TEXT;
ALTER TABLE agent_session_bindings ADD COLUMN IF NOT EXISTS capabilities_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_session_bindings ADD COLUMN IF NOT EXISTS recovery_state TEXT NOT NULL DEFAULT 'none';
ALTER TABLE agent_session_bindings ADD COLUMN IF NOT EXISTS replacement_of_id TEXT;
ALTER TABLE agent_session_bindings ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE INDEX IF NOT EXISTS agent_session_adapter_idx
    ON agent_session_bindings(adapter_session_id, is_current);
CREATE INDEX IF NOT EXISTS agent_session_recovery_idx
    ON agent_session_bindings(recovery_state, status);
