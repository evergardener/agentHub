-- Migration 003: events.seq 单调游标（Evolution v3 §4）
-- 替代 SQLite 专有 rowid，供 agentctl events --follow 跨后端使用。
ALTER TABLE events ADD COLUMN seq INTEGER;
-- 回填历史行（SQLite 用内建 rowid 定序）
UPDATE events SET seq = (
    SELECT COUNT(*) FROM events e2 WHERE e2.rowid <= events.rowid);
CREATE UNIQUE INDEX IF NOT EXISTS events_seq_idx ON events(seq);
