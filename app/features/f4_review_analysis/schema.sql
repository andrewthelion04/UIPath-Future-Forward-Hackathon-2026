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

-- Recomandările structurate ale unei analize, câte una pe rând (temă, prioritate, acțiune, citate).
-- Tabelă nouă, nu coloane noi în f4_insights: o bază de date existentă primește tabela la pornire
-- (CREATE IF NOT EXISTS) fără migrare și fără `manage.py seed`.
CREATE TABLE IF NOT EXISTS f4_recommendations (
    id            TEXT PRIMARY KEY,
    insight_id    TEXT NOT NULL,
    property_id   TEXT NOT NULL,
    position      INTEGER NOT NULL,
    theme         TEXT NOT NULL,
    theme_key     TEXT NOT NULL,
    priority      TEXT NOT NULL,
    priority_rank INTEGER NOT NULL,
    action        TEXT NOT NULL,
    quotes        TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_f4_recommendations_insight ON f4_recommendations (insight_id, priority_rank, position);
