-- Search index of delivered posts. Kept apart from the queue so entries outlive their folder.
CREATE TABLE post_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    msg_ids TEXT NOT NULL,
    date TEXT NOT NULL,
    target INTEGER NOT NULL,
    sent_msg_id INTEGER,
    text TEXT NOT NULL DEFAULT '',
    media_text TEXT NOT NULL DEFAULT '',
    speech_text TEXT NOT NULL DEFAULT '',
    -- text, media_text and speech_text folded for matching (see indexer.fold)
    search TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (channel_id, msg_id)
);

CREATE INDEX post_index_state ON post_index (state, id);
