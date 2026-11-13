-- F1: jurnalul payload-urilor brute și cheile de deduplicare.

CREATE TABLE IF NOT EXISTS f1_webhook_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL,
    source              TEXT NOT NULL,
    payload             TEXT NOT NULL,
    outcome             TEXT NOT NULL DEFAULT 'received',
    detail              TEXT,
    external_message_id TEXT,
    event_id            TEXT,
    received_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS f1_seen_messages (
    provider            TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    PRIMARY KEY (provider, external_message_id)
);
