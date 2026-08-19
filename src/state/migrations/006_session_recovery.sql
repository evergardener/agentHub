-- Migration 006: durable adapter/native session recovery metadata

ALTER TABLE agent_session_bindings ADD COLUMN adapter_session_id TEXT;
ALTER TABLE agent_session_bindings ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_session_bindings ADD COLUMN recovery_state TEXT NOT NULL DEFAULT 'none';
ALTER TABLE agent_session_bindings ADD COLUMN replacement_of_id TEXT;
ALTER TABLE agent_session_bindings ADD COLUMN last_error TEXT;

CREATE INDEX agent_session_adapter_idx
    ON agent_session_bindings(adapter_session_id, is_current);
CREATE INDEX agent_session_recovery_idx
    ON agent_session_bindings(recovery_state, status);
