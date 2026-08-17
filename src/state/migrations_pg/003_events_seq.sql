-- Migration 003 (PostgreSQL): events.seq 单调游标（Evolution v3 §4）
ALTER TABLE events ADD COLUMN IF NOT EXISTS seq INTEGER;
-- 回填历史行（按 created_at + id 定序）
UPDATE events e SET seq = sub.rn FROM (
    SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn FROM events
) sub WHERE e.id = sub.id;
CREATE UNIQUE INDEX IF NOT EXISTS events_seq_idx ON events(seq);
