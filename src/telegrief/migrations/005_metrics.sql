-- Monotonic event counters behind the metrics file; they survive restarts.
-- labels is a JSON object with sorted keys.
CREATE TABLE counters (
    name TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '{}',
    value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (name, labels)
);

-- When the owner's read marker passed a delivered post. Posts delivered before
-- this column existed count as read, so the first marker does not report them all.
ALTER TABLE queue ADD COLUMN read_at TEXT;
UPDATE queue SET read_at = sent_at WHERE state = 'sent';
