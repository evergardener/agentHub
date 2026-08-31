-- Persist the authoritative worker process generation for stale-heartbeat fencing.

ALTER TABLE agents ADD COLUMN adapter_instance_id TEXT;
ALTER TABLE agents ADD COLUMN adapter_started_at TEXT;
