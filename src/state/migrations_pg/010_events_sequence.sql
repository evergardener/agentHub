-- Migration 010 (PostgreSQL): concurrency-safe global event cursor.
ALTER TABLE events ALTER COLUMN seq TYPE BIGINT;
CREATE SEQUENCE IF NOT EXISTS events_seq_sequence AS BIGINT;
SELECT setval(
    'events_seq_sequence',
    COALESCE((SELECT MAX(seq) FROM events), 0) + 1,
    false
);
ALTER SEQUENCE events_seq_sequence OWNED BY events.seq;
ALTER TABLE events ALTER COLUMN seq SET DEFAULT nextval('events_seq_sequence');
ALTER TABLE events ALTER COLUMN seq SET NOT NULL;
