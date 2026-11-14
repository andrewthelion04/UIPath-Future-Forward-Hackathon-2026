"""Subiectele deschise cu oaspetele unei rezervări, comune tuturor feature-urilor.

Un feature nu citește tabelele altuia. Când un feature gestionează ceva ce îl privește pe
oaspete (ex. un incident), publică aici starea pe care oaspetele are voie să o afle. Cine
vorbește cu oaspetele o citește și poate răspunde la întrebări fără să ghicească.

- `publish` scrie starea curentă a subiectului (înlocuiește ce era);
- `facts` conține doar informații care pot ajunge la oaspete: nimic neaprobat de host;
- o ofertă (`GuestOffer`) poate primi un singur răspuns: `answer_offer` face tranziția atomic.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from app.core.clock import Clock, utcnow
from app.core.contracts.models import GuestOffer, GuestTopic
from app.core.db import Database, from_json, to_json

OFFER_STATUS = {"accept": "accepted", "decline": "declined"}


def _to_model(row: dict[str, Any]) -> GuestTopic:
    offer = from_json(row["offer"])
    return GuestTopic(
        id=row["id"],
        booking_id=row["booking_id"],
        feature=row["feature"],
        kind=row["kind"],
        title=row["title"],
        facts=from_json(row["facts"], {}),
        offer=GuestOffer(**offer, status=row["offer_status"] or "pending") if offer else None,
        status=row["status"],
        valid_until=row["valid_until"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class GuestTopics:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def publish(
        self,
        topic_id: str,
        *,
        booking_id: str,
        feature: str,
        kind: str,
        title: str,
        facts: dict[str, Any],
        offer: GuestOffer | None = None,
        valid_until: date | None = None,
    ) -> GuestTopic:
        """Scrie starea curentă a subiectului și îl (re)deschide."""
        now = utcnow().isoformat()
        offer_data = (
            to_json(offer.model_dump(mode="json", exclude={"status"})) if offer is not None else None
        )
        self._db.execute(
            "INSERT INTO guest_topics (id, booking_id, feature, kind, title, facts, offer, "
            "offer_status, status, valid_until, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET booking_id = excluded.booking_id, "
            "kind = excluded.kind, title = excluded.title, facts = excluded.facts, "
            "offer = excluded.offer, offer_status = excluded.offer_status, status = 'open', "
            "valid_until = excluded.valid_until, updated_at = excluded.updated_at",
            (
                topic_id,
                booking_id,
                feature,
                kind,
                title,
                to_json(facts),
                offer_data,
                offer.status if offer is not None else None,
                valid_until.isoformat() if valid_until else None,
                now,
                now,
            ),
        )
        return self.get(topic_id)  # type: ignore[return-value]

    def close(self, topic_id: str) -> bool:
        """Subiectul s-a încheiat: nu mai e arătat. O ofertă rămasă fără răspuns e retrasă."""
        cursor = self._db.execute(
            "UPDATE guest_topics SET status = 'closed', updated_at = ?, "
            "offer_status = CASE WHEN offer_status = 'pending' THEN 'withdrawn' ELSE offer_status END "
            "WHERE id = ? AND status = 'open'",
            (utcnow().isoformat(), topic_id),
        )
        return cursor.rowcount > 0

    def answer_offer(self, topic_id: str, choice: Literal["accept", "decline"]) -> bool:
        """Înregistrează răspunsul oaspetelui. False dacă oferta nu mai așteaptă un răspuns."""
        cursor = self._db.execute(
            "UPDATE guest_topics SET offer_status = ?, updated_at = ? "
            "WHERE id = ? AND status = 'open' AND offer_status = 'pending'",
            (OFFER_STATUS[choice], utcnow().isoformat(), topic_id),
        )
        return cursor.rowcount > 0

    def get(self, topic_id: str) -> GuestTopic | None:
        row = self._db.query_one("SELECT * FROM guest_topics WHERE id = ?", (topic_id,))
        return _to_model(row) if row else None

    def for_booking(self, booking_id: str) -> list[GuestTopic]:
        """Subiectele deschise ale rezervării, încă valabile, cele mai vechi primele."""
        rows = self._db.query(
            "SELECT * FROM guest_topics WHERE booking_id = ? AND status = 'open' "
            "AND (valid_until IS NULL OR valid_until >= ?) ORDER BY created_at, rowid",
            (booking_id, self._clock.today().isoformat()),
        )
        return [_to_model(row) for row in rows]
