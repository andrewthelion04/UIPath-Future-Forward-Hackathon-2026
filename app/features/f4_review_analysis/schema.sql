-- F4: snapshot-urile de recenzii (cu sursă și dată) și recomandările rezultate.

CREATE TABLE IF NOT EXISTS f4_review_snapshots (
    id          TEXT PRIMARY KEY,
    property_id TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    reviews     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_f4_snapshots_property ON f4_review_snapshots (property_id, fetched_at);

CREATE TABLE IF NOT EXISTS f4_insights (
    id              TEXT PRIMARY KEY,
    property_id     TEXT NOT NULL,
    snapshot_id     TEXT NOT NULL,
    used_fallback   INTEGER NOT NULL DEFAULT 0,
    review_count    INTEGER NOT NULL DEFAULT 0,
    average_rating  REAL,
    recommendations TEXT NOT NULL,
    evidence        TEXT NOT NULL DEFAULT '[]',
    agent_run_id    TEXT,
    created_at      TEXT NOT NULL
);
