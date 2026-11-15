-- Duplicate detection: what is compared of every delivered post (see dedup.py).
CREATE TABLE signatures (
    folder_id INTEGER NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    words INTEGER NOT NULL,
    -- the smallest word hashes, 4 bytes each, big-endian
    sketch BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (folder_id, channel_id, msg_id)
);

CREATE TABLE signature_media (
    folder_id INTEGER NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    media_id TEXT NOT NULL,
    duration INTEGER,
    -- 64-bit difference hash, hex; NULL when the attachment has no usable picture
    coarse TEXT,
    -- 32x32 grey levels; read only for the posts the coarse hash points at
    fine BLOB,
    PRIMARY KEY (folder_id, channel_id, msg_id, position)
);

ALTER TABLE folders ADD COLUMN dedup INTEGER NOT NULL DEFAULT 1;

-- A queue row in state 'duplicate': the delivered post it repeats, and when it was dropped.
ALTER TABLE queue ADD COLUMN dup_channel_id INTEGER;
ALTER TABLE queue ADD COLUMN dup_msg_id INTEGER;
ALTER TABLE queue ADD COLUMN dup_at TEXT;
