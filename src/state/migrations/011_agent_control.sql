-- Persistent desired state for operator-controlled Agent routing.
CREATE TABLE IF NOT EXISTS agent_controls (
    agent_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
