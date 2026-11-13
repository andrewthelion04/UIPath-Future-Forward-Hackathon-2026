"""Portul prin care simulatorul acționează asupra lumii simulate (PMS-ul mock)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class SimulationError(RuntimeError):
    pass


@runtime_checkable
class SimulationPort(Protocol):
    async def guest_message_payload(
        self, booking_id: str, text: str, sender_type: str = "guest"
    ) -> dict[str, Any]:
        """Oaspetele scrie în PMS-ul simulat; întoarce payload-ul de webhook rezultat."""
        ...

    async def cancel_reservation(self, booking_id: str) -> None:
        """Anulează rezervarea în PMS-ul simulat și eliberează nopțile în calendar."""
        ...
