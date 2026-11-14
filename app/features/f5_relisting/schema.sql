-- F5: propunerile de relistare după anulare.

CREATE TABLE IF NOT EXISTS f5_relist_proposals (
    id                 TEXT PRIMARY KEY,
    booking_id         TEXT NOT NULL,
    property_id        TEXT NOT NULL,
    start_date         TEXT NOT NULL,
    end_date           TEXT NOT NULL,
    base_price         REAL NOT NULL,
    floor_price        REAL NOT NULL,
    comparable_ref     TEXT,
    comparable_title   TEXT,
    comparable_price   REAL,
    proposed_price     REAL,
    nightly_prices     TEXT NOT NULL DEFAULT '[]',
    expires_at         TEXT,
    currency           TEXT NOT NULL DEFAULT 'EUR',
    market_source      TEXT,
    market_attribution TEXT,
    status             TEXT NOT NULL,
    note               TEXT,
    approval_id        TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
