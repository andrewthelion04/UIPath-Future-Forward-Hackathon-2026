-- F2: incidentele și starea lor.

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
    updated_at             TEXT NOT NULL
);
