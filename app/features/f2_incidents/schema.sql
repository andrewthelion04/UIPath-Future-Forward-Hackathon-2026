-- F2: incidentele, rapoartele atașate, conversația cu personalul și sarcinile.
-- Coloanele adăugate după schelet sunt completate și pe o bază existentă de `ensure_columns`
-- (repo.py), deci nu e nevoie de `python manage.py seed` după actualizare.

CREATE TABLE IF NOT EXISTS f2_incidents (
    id                     TEXT PRIMARY KEY,
    property_id            TEXT NOT NULL,
    booking_id             TEXT,
    description            TEXT NOT NULL,
    reported_via           TEXT NOT NULL,
    reporter               TEXT NOT NULL DEFAULT '',
    severity               TEXT NOT NULL DEFAULT 'medium',
    status                 TEXT NOT NULL,
    relocation_property_id TEXT,
    approval_id            TEXT,
    resolution             TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    correlation_id         TEXT,
    report_count           INTEGER NOT NULL DEFAULT 1,
    contact_staff_id       TEXT,
    contact_staff_name     TEXT,
    staff_questions        INTEGER NOT NULL DEFAULT 0,
    staff_deadline         TEXT,
    eta_hours              REAL,
    eta_source             TEXT,
    eta_quote              TEXT,
    eta_confidence         TEXT,
    in_house               INTEGER,
    hours_until_check_in   REAL,
    recommendation         TEXT,
    recommendation_reason  TEXT,
    drafts                 TEXT,
    drafts_source          TEXT,
    proposal_round         INTEGER NOT NULL DEFAULT 0,
    decided_option         TEXT,
    guest_answer           TEXT,
    guest_deadline         TEXT,
    closure_round          INTEGER NOT NULL DEFAULT 0,
    closure_approval_id    TEXT,
    closure_message        TEXT,
    guest_note             TEXT    -- ce i se poate spune oaspetelui cât timp nimic nu e hotărât
);

CREATE TABLE IF NOT EXISTS f2_incident_reports (
    id           TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL,
    description  TEXT NOT NULL,
    reported_via TEXT NOT NULL,
    reporter     TEXT NOT NULL DEFAULT '',
    severity     TEXT NOT NULL DEFAULT 'medium',
    booking_id   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS f2_staff_messages (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    staff_id    TEXT,
    staff_name  TEXT NOT NULL DEFAULT '',
    direction   TEXT NOT NULL,          -- to_staff | from_staff
    body        TEXT NOT NULL,
    simulated   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS f2_tasks (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,   -- ex. T-12, ușor de citat într-un mesaj
    incident_id     TEXT NOT NULL,
    staff_id        TEXT,
    staff_name      TEXT,
    description     TEXT NOT NULL,
    status          TEXT NOT NULL,          -- open | done
    completion_note TEXT,
    completed_by    TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);
