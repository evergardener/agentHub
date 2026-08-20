-- Migration 010 (SQLite): PostgreSQL uses a native sequence; SQLite keeps the
-- serialized MAX(seq)+1 writer path and only reasserts the cursor index.
CREATE UNIQUE INDEX IF NOT EXISTS events_seq_idx ON events(seq);
