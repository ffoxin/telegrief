CREATE TABLE channels (
    id INTEGER PRIMARY KEY,
    username TEXT,
    title TEXT NOT NULL,
    subscribers INTEGER NOT NULL DEFAULT 0,
    reactions_enabled INTEGER NOT NULL DEFAULT 1,
    noforwards INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE targets (
    folder TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE posts (
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    grouped_id INTEGER,
    date TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    forwards INTEGER NOT NULL DEFAULT 0,
    reactions INTEGER NOT NULL DEFAULT 0,
    replies INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    noforwards INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, msg_id)
);

CREATE TABLE digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    target INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    total INTEGER NOT NULL,
    title_msg_id INTEGER,
    footer_msg_id INTEGER,
    footer_text TEXT,
    status TEXT NOT NULL,
    last_batch_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX digests_target_status ON digests (target, status);

CREATE TABLE digest_items (
    digest_id INTEGER NOT NULL REFERENCES digests (id),
    rank INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    msg_ids TEXT NOT NULL,
    engagement INTEGER NOT NULL,
    views INTEGER NOT NULL,
    score REAL NOT NULL,
    noforwards INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    delivery TEXT,
    sent_msg_id INTEGER,
    sent_at TEXT,
    PRIMARY KEY (digest_id, rank)
);
