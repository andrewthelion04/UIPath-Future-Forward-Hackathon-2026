"""Citirea și scrierea datelor interne despre host, proprietăți, rezervări și personal.

Datele importate sunt oglinda internă a PMS-ului. Editările hostului sunt păstrate
separat și aplicate la citirea proprietăților; sincronizarea nu le suprascrie.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.clock import utcnow
from app.core.contracts.models import Booking, BookingStatus, Host, HostRules, Property, StaffMember
from app.core.db import Database, from_json, to_json


def _property(row: dict[str, Any]) -> Property:
    return Property(
        id=row["id"],
        name=row["name"],
        city=row["city"],
        address=row["address"],
        bedrooms=row["bedrooms"],
        max_guests=row["max_guests"],
        amenities=from_json(row["amenities"], []),
        base_price=row["base_price"],
        currency=row["currency"],
        pms_provider=row["pms_provider"],
        listing_url=row.get("listing_url"),
    )


def _booking(row: dict[str, Any]) -> Booking:
    return Booking(
        id=row["id"],
        property_id=row["property_id"],
        guest_name=row["guest_name"],
        guest_count=row["guest_count"],
        check_in=row["check_in"],
        check_out=row["check_out"],
        status=row["status"],
        pms_provider=row["pms_provider"],
        source_channel=row["source_channel"],
        total_price=row["total_price"],
        currency=row["currency"],
    )


class WorldRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- Host ----------------------------------------------------------------------

    def get_host(self) -> Host | None:
        row = self._db.query_one("SELECT * FROM hosts ORDER BY rowid LIMIT 1")
        if row is None:
            return None
        return Host(id=row["id"], name=row["name"], rules=HostRules(**from_json(row["rules"], {})))

    def host_rules(self) -> HostRules:
        host = self.get_host()
        return host.rules if host else HostRules()

    def upsert_host(self, host: Host) -> None:
        self._db.execute(
            "INSERT INTO hosts (id, name, rules) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, rules = excluded.rules",
            (host.id, host.name, to_json(host.rules.model_dump())),
        )

    # --- Proprietăți ---------------------------------------------------------------

    def list_properties(self) -> list[Property]:
        return sorted([self._with_overrides(_property(r)) for r in self._db.query("SELECT * FROM properties ORDER BY name")], key=lambda p: p.name)

    def get_property(self, property_id: str) -> Property | None:
        row = self._db.query_one("SELECT * FROM properties WHERE id = ?", (property_id,))
        return self._with_overrides(_property(row)) if row else None

    def _with_overrides(self, prop: Property) -> Property:
        row = self._db.query_one("SELECT fields FROM property_overrides WHERE property_id=?", (prop.id,))
        return Property.model_validate({**prop.model_dump(), **from_json(row["fields"], {})}) if row else prop

    def edit_property(self, property_id: str, fields: dict[str, Any]) -> Property:
        with self._db.transaction():
            current = self.get_property(property_id)
            if current is None:
                raise LookupError("Property not found")
            row = self._db.query_one("SELECT fields FROM property_overrides WHERE property_id=?", (property_id,))
            merged = {**(from_json(row["fields"], {}) if row else {}), **fields}
            self._db.execute(
                "INSERT INTO property_overrides VALUES (?, ?, ?) ON CONFLICT(property_id) "
                "DO UPDATE SET fields=excluded.fields, updated_at=excluded.updated_at",
                (property_id, to_json(merged), utcnow().isoformat()),
            )
        return self.get_property(property_id)

    def clear_property_overrides(self, property_id: str) -> Property:
        if self.get_property(property_id) is None:
            raise LookupError("Property not found")
        self._db.execute("DELETE FROM property_overrides WHERE property_id=?", (property_id,))
        return self.get_property(property_id)

    def upsert_property(self, prop: Property) -> None:
        self._db.execute(
            "INSERT INTO properties (id, name, city, address, bedrooms, max_guests, amenities, "
            "base_price, currency, pms_provider, listing_url, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, city = excluded.city, "
            "address = excluded.address, bedrooms = excluded.bedrooms, "
            "max_guests = excluded.max_guests, amenities = excluded.amenities, "
            "base_price = excluded.base_price, currency = excluded.currency, "
            "pms_provider = excluded.pms_provider, listing_url = excluded.listing_url, "
            "updated_at = excluded.updated_at",
            (
                prop.id,
                prop.name,
                prop.city,
                prop.address,
                prop.bedrooms,
                prop.max_guests,
                to_json(prop.amenities),
                prop.base_price,
                prop.currency,
                prop.pms_provider.value,
                prop.listing_url,
                utcnow().isoformat(),
            ),
        )

    # --- Rezervări -----------------------------------------------------------------

    def list_bookings(
        self, *, property_id: str | None = None, status: str | None = None
    ) -> list[Booking]:
        clauses, params = [], []
        if property_id:
            clauses.append("property_id = ?")
            params.append(property_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(f"SELECT * FROM bookings {where} ORDER BY check_in, id", params)
        return [_booking(row) for row in rows]

    def get_booking(self, booking_id: str) -> Booking | None:
        row = self._db.query_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        return _booking(row) if row else None

    def upsert_booking(self, booking: Booking) -> None:
        self._db.execute(
            "INSERT INTO bookings (id, property_id, guest_name, guest_count, check_in, check_out, "
            "status, pms_provider, source_channel, total_price, currency, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET property_id = excluded.property_id, "
            "guest_name = excluded.guest_name, guest_count = excluded.guest_count, "
            "check_in = excluded.check_in, check_out = excluded.check_out, "
            "status = excluded.status, pms_provider = excluded.pms_provider, "
            "source_channel = excluded.source_channel, total_price = excluded.total_price, "
            "currency = excluded.currency, updated_at = excluded.updated_at",
            (
                booking.id,
                booking.property_id,
                booking.guest_name,
                booking.guest_count,
                booking.check_in.isoformat(),
                booking.check_out.isoformat(),
                booking.status.value,
                booking.pms_provider.value,
                booking.source_channel.value,
                booking.total_price,
                booking.currency,
                utcnow().isoformat(),
            ),
        )

    def active_booking_for_property(self, property_id: str, today: date) -> Booking | None:
        """Rezervarea confirmată în curs sau, dacă nu există, următoarea care începe."""
        rows = self._db.query(
            "SELECT * FROM bookings WHERE property_id = ? AND status = ? AND check_out > ? "
            "ORDER BY check_in LIMIT 1",
            (property_id, BookingStatus.CONFIRMED.value, today.isoformat()),
        )
        return _booking(rows[0]) if rows else None

    # --- Personal ------------------------------------------------------------------

    def list_staff(self) -> list[StaffMember]:
        rows = self._db.query("SELECT * FROM staff ORDER BY name")
        return [
            StaffMember(
                id=row["id"],
                name=row["name"],
                role=row["role"],
                property_ids=from_json(row["property_ids"], []),
            )
            for row in rows
        ]

    def upsert_staff(self, member: StaffMember) -> None:
        self._db.execute(
            "INSERT INTO staff (id, name, role, property_ids) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, role = excluded.role, "
            "property_ids = excluded.property_ids",
            (member.id, member.name, member.role, to_json(member.property_ids)),
        )
