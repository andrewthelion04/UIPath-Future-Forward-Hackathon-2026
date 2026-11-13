-- Lumea simulată: ce „vede” PMS-ul mock, sursa de recenzii mock și piața mock.
-- Cheile de aici sunt ID-uri EXTERNE (ex. MOCK-PROP-1); aplicația le traduce prin id_map.

CREATE TABLE IF NOT EXISTS mock_properties (
    external_id TEXT PRIMARY KEY,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_reservations (
    external_id          TEXT PRIMARY KEY,
    property_external_id TEXT NOT NULL,
    check_in             TEXT NOT NULL,
    check_out            TEXT NOT NULL,
    status               TEXT NOT NULL,
    data                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_calendar_days (
    property_external_id TEXT NOT NULL,
    day                  TEXT NOT NULL,
    available            INTEGER NOT NULL,
    price                REAL NOT NULL,
    PRIMARY KEY (property_external_id, day)
);

CREATE TABLE IF NOT EXISTS mock_messages (
    external_id             TEXT PRIMARY KEY,
    reservation_external_id TEXT NOT NULL,
    sender_type             TEXT NOT NULL,
    body                    TEXT NOT NULL,
    sent_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_reviews (
    external_id          TEXT PRIMARY KEY,
    property_external_id TEXT NOT NULL,
    data                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_comparables (
    ref  TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
