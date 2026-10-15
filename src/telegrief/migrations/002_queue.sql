CREATE TABLE folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    min_age_h INTEGER NOT NULL,
    period_h INTEGER NOT NULL,
    batch_size INTEGER NOT NULL,
    scoring TEXT NOT NULL,
    min_denominator INTEGER NOT NULL,
    max_items INTEGER NOT NULL,
    max_age_d INTEGER NOT NULL,
    next_run_at TEXT NOT NULL,
    last_fetch_at TEXT,
    last_batch_at TEXT,
    last_error TEXT,
    fetch_msg_id INTEGER,
    fetch_added INTEGER NOT NULL DEFAULT 0,
    button_msg_id INTEGER,
    button_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE queue (
    folder_id INTEGER NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    msg_ids TEXT NOT NULL,
    date TEXT NOT NULL,
    engagement INTEGER NOT NULL,
    views INTEGER NOT NULL,
    score REAL NOT NULL,
    noforwards INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    delivery TEXT,
    sent_msg_id INTEGER,
    sent_at TEXT,
    queued_at TEXT NOT NULL,
    PRIMARY KEY (folder_id, channel_id, msg_id)
);

CREATE INDEX queue_folder_state_score ON queue (folder_id, state, score);

DROP TABLE digest_items;
DROP TABLE digests;
