"""Întrebări predate hostului; răspunsul autorizat de el pleacă exact prin PMS."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from app.core.clock import utcnow
from app.core.contracts.models import BookingStatus


class AnswerConflict(ValueError):
    pass


class HostAnswers:
    def __init__(self, db, world, channel):
        self.db, self.world, self.channel = db, world, channel
        self._locks: dict[str, asyncio.Lock] = {}

    def request(self, booking_id: str, source_message_id: str, question: str, note: str = "") -> dict:
        booking = self.world.get_booking(booking_id)
        if booking is None:
            raise LookupError("Booking not found")
        self.db.execute(
            "INSERT INTO host_answers (id, booking_id, property_id, source_message_id, question, context_note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(booking_id, source_message_id) DO NOTHING",
            (f"ans_{uuid4().hex[:12]}", booking_id, booking.property_id, source_message_id,
             question, note, utcnow().isoformat()),
        )
        row = self.db.query_one("SELECT * FROM host_answers WHERE booking_id=? AND source_message_id=?",
                                (booking_id, source_message_id))
        return self._view(row)

    def _view(self, row: dict) -> dict:
        booking = self.world.get_booking(row["booking_id"])
        prop = self.world.get_property(row["property_id"])
        return {**row, "guest_name": booking.guest_name if booking else "Unknown guest",
                "property_name": prop.name if prop else "Unknown property"}

    def list(self, status: str = "pending") -> list[dict]:
        rows = self.db.query("SELECT * FROM host_answers ORDER BY created_at DESC")
        return [self._view(r) for r in rows if status == "all" or r["status"] in ("pending", "failed", "sending")]

    def handled_by_incident(self, booking_id: str, source_message_id: str) -> None:
        self.db.execute(
            "UPDATE host_answers SET status='superseded' WHERE booking_id=? AND source_message_id=? "
            "AND status IN ('pending', 'failed')", (booking_id, source_message_id))

    def suppress_reported_issues(self) -> None:
        """Ascunde și duplicatele istorice, numai când mesajul sursă a emis IssueReported."""
        self.db.execute("""
            UPDATE host_answers SET status='superseded'
            WHERE status IN ('pending', 'failed') AND EXISTS (
                SELECT 1 FROM events guest JOIN events issue
                ON issue.correlation_id=COALESCE(guest.correlation_id, guest.id)
                WHERE guest.type='GuestMessageReceived' AND issue.type='IssueReported'
                AND json_extract(guest.payload, '$.message_id')=host_answers.source_message_id
                AND json_extract(guest.payload, '$.booking_id')=host_answers.booking_id
            )
        """)

    async def reconcile(self, answer_id: str, delivered: bool) -> dict:
        """Hostul verifică PMS-ul înainte să autorizeze reluarea unei livrări incerte."""
        async with self._locks.setdefault(answer_id, asyncio.Lock()):
            row = self.db.query_one("SELECT * FROM host_answers WHERE id=?", (answer_id,))
            if row is None:
                raise LookupError("Answer request not found")
            if row["status"] != "sending":
                raise AnswerConflict("Only an uncertain delivery can be reconciled")
            self.db.execute("UPDATE host_answers SET status=?, error=NULL, answered_at=? WHERE id=?",
                            ("sent" if delivered else "pending", utcnow().isoformat() if delivered else None, answer_id))
            return self._view(self.db.query_one("SELECT * FROM host_answers WHERE id=?", (answer_id,)))

    async def send(self, answer_id: str, text: str) -> dict:
        async with self._locks.setdefault(answer_id, asyncio.Lock()):
            row = self.db.query_one("SELECT * FROM host_answers WHERE id=?", (answer_id,))
            if row is None:
                raise LookupError("Answer request not found")
            if row["status"] == "sent":
                if text != row["reply"]:
                    raise AnswerConflict("This question has already been answered")
                return self._view(row)
            if row["status"] == "sending":
                raise AnswerConflict("Delivery is uncertain. Check the PMS conversation before retrying.")
            if row["status"] not in ("pending", "failed"):
                raise AnswerConflict("This request is already handled through the incident workflow")
            booking = self.world.get_booking(row["booking_id"])
            if booking is None or booking.status == BookingStatus.CANCELLED:
                raise AnswerConflict("The booking is missing or cancelled; review it before replying")
            self.db.execute("UPDATE host_answers SET status='sending', reply=?, error=NULL WHERE id=?", (text, answer_id))
            try:
                message = await self.channel.send_to_guest(row["booking_id"], text, sender="host", feature="host_answers")
            except Exception:
                # Unele PMS-uri pot accepta mesajul înainte de o eroare de rețea.
                # Nu reîncercăm automat: ar putea produce un mesaj duplicat.
                self.db.execute("UPDATE host_answers SET error=? WHERE id=?",
                                ("Delivery could not be confirmed. Check the PMS conversation.", answer_id))
                raise
            self.db.execute("UPDATE host_answers SET status='sent', sent_message_id=?, answered_at=? WHERE id=?",
                            (message.id, utcnow().isoformat(), answer_id))
            return self._view(self.db.query_one("SELECT * FROM host_answers WHERE id=?", (answer_id,)))
