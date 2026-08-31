-- Persist the authoritative worker process generation for stale-heartbeat fencing.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS adapter_instance_id TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS adapter_started_at TEXT;
