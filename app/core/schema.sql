-- Tabelele nucleului comun. Tabelele unui feature stau în `app/features/<feature>/schema.sql`.

CREATE TABLE IF NOT EXISTS hosts (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    rules TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS properties (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    city         TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL DEFAULT '',
    bedrooms     INTEGER NOT NULL DEFAULT 1,
    max_guests   INTEGER NOT NULL DEFAULT 2,
    amenities    TEXT NOT NULL DEFAULT '[]',
    base_price   REAL NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'EUR',
    pms_provider TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    listing_url  TEXT                -- pe bazele vechi îl adaugă `Container.init_schema`
);

CREATE TABLE IF NOT EXISTS bookings (
    id             TEXT PRIMARY KEY,
    property_id    TEXT NOT NULL,
    guest_name     TEXT NOT NULL,
    guest_count    INTEGER NOT NULL DEFAULT 1,
    check_in       TEXT NOT NULL,
    check_out      TEXT NOT NULL,
    status         TEXT NOT NULL,
    pms_provider   TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    total_price    REAL,
    currency       TEXT NOT NULL DEFAULT 'EUR',
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_property ON bookings (property_id, check_in);

CREATE TABLE IF NOT EXISTS staff (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL,
    property_ids TEXT NOT NULL DEFAULT '[]'
);

-- Tabela de corespondențe: singurul loc unde ID-urile externe întâlnesc ID-urile interne.
CREATE TABLE IF NOT EXISTS id_map (
    connection  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    internal_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (connection, entity_type, external_id)
);
CREATE INDEX IF NOT EXISTS idx_id_map_internal ON id_map (connection, entity_type, internal_id);

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    booking_id   TEXT NOT NULL,
    direction    TEXT NOT NULL,
    sender       TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL,
    pms_provider TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_booking ON messages (booking_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    source         TEXT NOT NULL,
    correlation_id TEXT,
    causation_id   TEXT,
    occurred_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events (correlation_id);

CREATE TABLE IF NOT EXISTS event_handler_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    handler     TEXT NOT NULL,
    feature     TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    started_at  TEXT NOT NULL,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_handler_runs_event ON event_handler_runs (event_id);

CREATE TABLE IF NOT EXISTS notifications (
    id             TEXT PRIMARY KEY,
    feature        TEXT NOT NULL,
    level          TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    booking_id     TEXT,
    property_id    TEXT,
    approval_id    TEXT,
    correlation_id TEXT,
    read_at        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    feature        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    title          TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    options        TEXT NOT NULL,
    payload        TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,
    chosen_option  TEXT,
    decided_by     TEXT,
    decision_note  TEXT,
    booking_id     TEXT,
    property_id    TEXT,
    correlation_id TEXT,
    dedupe_key     TEXT,
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_dedupe
    ON approvals (dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_runs (
    id                TEXT PRIMARY KEY,
    agent             TEXT NOT NULL,
    feature           TEXT NOT NULL,
    objective         TEXT NOT NULL,
    status            TEXT NOT NULL,
    result            TEXT,
    evidence          TEXT NOT NULL DEFAULT '[]',
    error             TEXT,
    booking_id        TEXT,
    property_id       TEXT,
    correlation_id    TEXT,
    model             TEXT,
    steps             INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost              REAL NOT NULL DEFAULT 0,
    started_at        TEXT NOT NULL,
    finished_at       TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    idx               INTEGER NOT NULL,
    kind              TEXT NOT NULL,
    tool_name         TEXT,
    reason            TEXT,
    arguments         TEXT,
    result            TEXT,
    is_error          INTEGER NOT NULL DEFAULT 0,
    messages          TEXT,
    content           TEXT,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost              REAL NOT NULL DEFAULT 0,
    duration_ms       INTEGER,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps (run_id, idx);

-- Subiectele deschise cu oaspetele unei rezervări (ex. un incident în curs). Le scrie
-- feature-ul care gestionează subiectul; le citește cine vorbește cu oaspetele.
CREATE TABLE IF NOT EXISTS guest_topics (
    id           TEXT PRIMARY KEY,
    booking_id   TEXT NOT NULL,
    feature      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    facts        TEXT NOT NULL DEFAULT '{}',   -- doar ce are voie să afle oaspetele
    offer        TEXT,                         -- JSON: question, details, expires_at
    offer_status TEXT,                         -- pending | accepted | declined | withdrawn
    status       TEXT NOT NULL,                -- open | closed
    valid_until  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guest_topics_booking ON guest_topics (booking_id, status);
