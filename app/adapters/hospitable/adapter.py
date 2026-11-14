"""Adaptorul PMS Hospitable (Public API v2). Prima versiune, NETESTATĂ live.

Confirmat de echipă (CLAUDE.md §4):
- baza `https://public.api.hospitable.com/v2`, `Authorization: Bearer <token>`;
- `POST /reservations/{uuid}/messages` cu `{"body": ...}` -> 201;
- webhook-ul `message.created` vine pentru ambele părți, deci F1 filtrează `sender_type`.

NECONFIRMAT (documentația nu a putut fi citită automat; de verificat la primul apel real):
- forma răspunsurilor de la `GET /properties` și `GET /reservations` (câmpuri, paginare,
  parametrul `properties[]`, `include`);
- structura exactă a payload-ului de webhook, de aceea `parse_webhook` e permisiv, iar F1
  salvează payload-urile brute;
- citirea și scrierea calendarului: rămân stub (capabilități dezactivate).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from typing import Any

import httpx

from app.core.contracts.models import BookingStatus, CalendarDay, SourceChannel
from app.core.id_mapper import RESERVATION, IdMapper
from app.ports.pms import (
    CapabilityNotSupported,
    InboundMessage,
    PmsCapabilities,
    PmsError,
    PmsProperty,
    PmsReservation,
    SentMessage,
    UnmappedEntity,
)

logger = logging.getLogger("staypilot.hospitable")

PROVIDER = "hospitable"
MESSAGE_EVENT = "message.created"
PAGE_SIZE = 100
MAX_PAGES = 50
CALENDAR_NOT_CONFIRMED = "Hospitable calendar endpoints are not confirmed yet (see CLAUDE.md §4)"


def _first(data: dict[str, Any], *keys: str) -> Any:
    """Prima valoare nevidă dintre mai multe chei posibile (parsare permisivă)."""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _channel(platform: Any) -> SourceChannel:
    name = str(platform or "").lower()
    if "airbnb" in name:
        return SourceChannel.AIRBNB
    if "booking" in name:
        return SourceChannel.BOOKING_COM
    if "vrbo" in name or "homeaway" in name:
        return SourceChannel.VRBO
    return SourceChannel.DIRECT


def _status(item: dict[str, Any]) -> BookingStatus:
    current = (item.get("reservation_status") or {}).get("current") or {}
    name = str(current.get("category") or item.get("status") or "").lower()
    if any(word in name for word in ("cancel", "declin", "denied", "expired", "withdrawn")):
        return BookingStatus.CANCELLED
    if any(word in name for word in ("accept", "confirm", "booked", "checkpoint")):
        return BookingStatus.CONFIRMED
    return BookingStatus.PENDING


def _amenity(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class HospitableAdapter:
    provider = PROVIDER

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        ids: IdMapper,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.capabilities = PmsCapabilities(
            list_properties=True,
            list_reservations=True,
            send_messages=True,
            receive_webhooks=True,
        )
        self._ids = ids
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    # --- HTTP ----------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise PmsError(f"Hospitable {method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise PmsError(
                f"Hospitable {method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    async def _pages(self, path: str, params: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            body = await self._request(
                "GET", path, params=[*params, ("page", page), ("per_page", PAGE_SIZE)]
            )
            data = body.get("data") or []
            items.extend(item for item in data if isinstance(item, dict))
            last_page = (body.get("meta") or {}).get("last_page")
            has_next = bool((body.get("links") or {}).get("next"))
            if not data or (last_page is not None and page >= last_page) or (
                last_page is None and not has_next
            ):
                break
        return items

    # --- PropertiesPort / ReservationsPort -----------------------------------------

    async def list_properties(self) -> list[PmsProperty]:
        properties = []
        for item in await self._pages("/properties", []):
            if not item.get("id"):
                continue
            address = item.get("address") or {}
            capacity = item.get("capacity") or {}
            properties.append(
                PmsProperty(
                    external_id=str(item["id"]),
                    name=str(_first(item, "public_name", "name") or "Unnamed property"),
                    city=str(address.get("city") or ""),
                    address=str(_first(address, "display", "street") or ""),
                    bedrooms=int(capacity.get("bedrooms") or 1),
                    max_guests=int(capacity.get("max") or 2),
                    amenities=[_amenity(a) for a in item.get("amenities") or [] if isinstance(a, str)],
                    currency=str(item.get("currency") or "EUR"),
                )
            )
        return properties

    async def list_reservations(self) -> list[PmsReservation]:
        property_ids = [prop.external_id for prop in await self.list_properties()]
        if not property_ids:
            return []
        params: list[tuple[str, Any]] = [("properties[]", pid) for pid in property_ids]
        params.append(("include", "guest,properties"))
        reservations = []
        for item in await self._pages("/reservations", params):
            try:
                reservations.append(self._reservation(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping Hospitable reservation %s: %s", item.get("id"), exc)
        return reservations

    @staticmethod
    def _reservation(item: dict[str, Any]) -> PmsReservation:
        linked = item.get("properties") or []
        property_id = (
            (linked[0].get("id") if linked and isinstance(linked[0], dict) else None)
            or item.get("property_id")
            or (item.get("property") or {}).get("id")
        )
        if not property_id:
            raise ValueError("no property reference")
        guest = item.get("guest") or {}
        name = " ".join(
            str(part) for part in (guest.get("first_name"), guest.get("last_name")) if part
        )
        guests = item.get("guests") or {}
        return PmsReservation(
            external_id=str(item["id"]),
            property_external_id=str(property_id),
            guest_name=name or "Guest",
            guest_count=int(guests.get("total") or 1) if isinstance(guests, dict) else 1,
            check_in=str(_first(item, "arrival_date", "check_in"))[:10],
            check_out=str(_first(item, "departure_date", "check_out"))[:10],
            status=_status(item),
            source_channel=_channel(item.get("platform")),
        )

    # --- MessagingPort -------------------------------------------------------------

    async def move_reservation(self, booking_id: str, property_id: str) -> None:
        raise CapabilityNotSupported("Reservation moves are not implemented for Hospitable")

    async def send_message(self, booking_id: str, body: str) -> SentMessage:
        reservation_uuid = self._ids.to_external(PROVIDER, RESERVATION, booking_id)
        if reservation_uuid is None:
            raise UnmappedEntity(f"booking {booking_id} is not mapped to a Hospitable reservation")
        response = await self._request(
            "POST", f"/reservations/{reservation_uuid}/messages", json={"body": body}
        )
        data = response.get("data") if isinstance(response, dict) else None
        message_id = data.get("id") if isinstance(data, dict) else None
        return SentMessage(external_message_id=str(message_id) if message_id else None)

    def is_reservation_webhook(self, payload: dict[str, Any]) -> bool:
        return any(
            isinstance(payload.get(key), str)
            and payload[key].startswith(("reservation.", "booking."))
            for key in ("action", "event", "type")
        )

    def parse_webhook(self, payload: dict[str, Any]) -> InboundMessage | None:
        """Parsare permisivă: structura exactă a payload-ului nu e confirmată."""
        action = _first(payload, "action", "event", "type")
        if action is not None and action != MESSAGE_EVENT:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        body = _first(data, "body", "message", "text")
        sender_type = _first(data, "sender_type", "sender_role") or _first(sender, "type", "role")
        if body is None and sender_type is None:
            return None  # nu arată a mesaj
        reservation = data.get("reservation") if isinstance(data.get("reservation"), dict) else {}
        reservation_id = _first(data, "reservation_id", "reservation_uuid") or reservation.get("id")
        sent_at = _first(data, "created_at", "sent_at") or payload.get("created")
        message_id = _first(data, "id", "message_id", "uuid")
        if message_id is None:
            # Fără ID de mesaj, deduplicăm după conținut.
            fingerprint = f"{reservation_id}|{sender_type}|{body}|{sent_at}"
            message_id = "sha1:" + hashlib.sha1(fingerprint.encode()).hexdigest()
        return InboundMessage(
            external_message_id=str(message_id),
            external_reservation_id=str(reservation_id) if reservation_id else None,
            sender_type=str(sender_type or "").lower(),
            body=str(body or ""),
            sent_at=sent_at if isinstance(sent_at, str) else None,
        )

    # --- CalendarPort (stub până la confirmarea endpointurilor) --------------------

    async def get_days(self, property_id: str, start: date, end: date) -> list[CalendarDay]:
        raise CapabilityNotSupported(CALENDAR_NOT_CONFIRMED)

    async def set_price(self, property_id: str, start: date, end: date, price: float) -> None:
        raise CapabilityNotSupported(CALENDAR_NOT_CONFIRMED)

    async def set_availability(
        self, property_id: str, start: date, end: date, available: bool
    ) -> None:
        raise CapabilityNotSupported(CALENDAR_NOT_CONFIRMED)

    async def aclose(self) -> None:
        await self._http.aclose()
