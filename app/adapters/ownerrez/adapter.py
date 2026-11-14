"""Adaptorul PMS OwnerRez (API v2). Prima versiune, NETESTATĂ live.

Verificat în documentația oficială (noiembrie 2026):
- baze: `https://apistage.ownerrez.com` (Stage) și `https://api.ownerrez.com` (producție);
- autentificare: OAuth `Authorization: Bearer <token>` sau Basic cu email + personal access
  token; antetul `User-Agent` e obligatoriu la fiecare apel;
- `GET /v2/properties`, `GET /v2/bookings` (cere `property_ids` sau `since_utc`), paginare prin
  `next_page_url`; `POST /v2/messages` cu `{thread_id, body}`; `PATCH /v2/spotrates`;
- mesageria cere token OAuth: cu personal access token API-ul răspunde 402;
- webhook: `{id, user_id, action, entity_type, entity_id, categories, entity}`, mesajele au
  `entity_type = "thread_message"`;
- `GET /v2/bookings` include toate tipurile care țin nopțile ocupate; `from` filtrează
  plecarea, `to` sosirea (inclusiv); `GET /v2/spotrates` expune override-uri rare pe noapte.
  Surse: https://api.ownerrez.com/openapi/v2.json și
  https://www.ownerrez.com/support/articles/api-availability-sync

NECONFIRMAT (de verificat în Stage):
- numele câmpului de ID (`id`) și cheia listei de proprietăți (`items`);
- `GET /v2/bookings/{id}` pentru a afla `thread_ids` înainte de trimiterea unui mesaj;
- valorile lui `from_role` din webhook-ul de mesaje (presupus: conține „guest” pentru oaspete);
- calendarul folosește rezervări/blocări și override-uri `GET /v2/spotrates`; nopțile fără
  override folosesc `base_price` al proprietății sau 0. Nu calculează regulile sezoniere;
- dacă citirea metadatelor eșuează, moneda și prețul de bază vin din ultima listare
  reușită; fără o listare reușită anterioară, fallback-ul este EUR și 0;
- disponibilitatea rămâne stub: API-ul poate crea blocări, dar nu le poate anula/șterge,
  deci nu poate implementa simetric `available=True`. Vezi documentația oficială:
  https://www.ownerrez.com/support/articles/api-quote-bookings
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any

import httpx

from app.core.contracts.models import BookingStatus, CalendarDay, SourceChannel
from app.core.id_mapper import PROPERTY, RESERVATION, IdMapper
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

logger = logging.getLogger("staypilot.ownerrez")

PROVIDER = "ownerrez"
PAGE_SIZE = 100
MAX_PAGES = 50
MESSAGE_ENTITY = "thread_message"


def _channel(listing_site: Any) -> SourceChannel:
    name = str(listing_site or "").lower().replace(".", "").replace("_", "").replace(" ", "")
    if "airbnb" in name:
        return SourceChannel.AIRBNB
    if "booking" in name:
        return SourceChannel.BOOKING_COM
    if "vrbo" in name or "homeaway" in name:
        return SourceChannel.VRBO
    return SourceChannel.DIRECT


def _status(value: Any) -> BookingStatus:
    name = str(value or "").lower()
    if name.startswith("cancel"):  # API-ul scrie „canceled”
        return BookingStatus.CANCELLED
    if name == "pending":
        return BookingStatus.PENDING
    return BookingStatus.CONFIRMED


def _price(value: Any) -> float | None:
    """Prețurile absente sau invalide nu trebuie să ajungă în calendar."""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


class OwnerRezAdapter:
    provider = PROVIDER

    def __init__(
        self,
        *,
        base_url: str,
        ids: IdMapper,
        access_token: str | None = None,
        username: str | None = None,
        personal_token: str | None = None,
        user_agent: str = "StayPilot AI",
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": user_agent}
        auth: httpx.Auth | None = None
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        elif username and personal_token:
            auth = httpx.BasicAuth(username, personal_token)
        else:
            raise ValueError("OwnerRez needs an OAuth access token or username + personal token")
        self.capabilities = PmsCapabilities(
            list_properties=True,
            list_reservations=True,
            send_messages=bool(access_token),  # mesageria nu merge cu personal access token
            receive_webhooks=bool(access_token),
            read_calendar=True,
            write_prices=True,
        )
        self._ids = ids
        self._property_pricing: dict[str, tuple[str, float]] = {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    # --- HTTP ----------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise PmsError(f"OwnerRez {method} {url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise PmsError(
                f"OwnerRez {method} {url} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    async def _pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = path
        query: dict[str, Any] | None = {**params, "limit": PAGE_SIZE}
        for _ in range(MAX_PAGES):
            if url is None:
                break
            body = await self._request("GET", url, params=query)
            page = body.get("items") if isinstance(body, dict) else body
            items.extend(item for item in page or [] if isinstance(item, dict))
            # `next_page_url` conține deja toți parametrii paginii următoare.
            url = body.get("next_page_url") if isinstance(body, dict) else None
            query = None
        if url is not None:
            raise PmsError(f"OwnerRez pagination exceeded {MAX_PAGES} pages for {path}")
        return items

    def _external(self, entity_type: str, internal_id: str) -> str:
        external_id = self._ids.to_external(PROVIDER, entity_type, internal_id)
        if external_id is None:
            raise UnmappedEntity(f"{entity_type} {internal_id} is not mapped on OwnerRez")
        return external_id

    # --- PropertiesPort / ReservationsPort -----------------------------------------

    async def list_properties(self) -> list[PmsProperty]:
        properties = []
        for item in await self._pages("/v2/properties", {}):
            if item.get("id") is None:
                continue
            address = item.get("address") or {}
            properties.append(
                PmsProperty(
                    external_id=str(item["id"]),
                    name=str(item.get("name") or item.get("external_name") or "Unnamed property"),
                    city=str(address.get("city") or ""),
                    address=", ".join(
                        str(part) for part in (address.get("street1"), address.get("city")) if part
                    ),
                    bedrooms=int(item.get("bedrooms") or 1),
                    max_guests=int(item.get("max_guests") or 2),
                    amenities=[],  # API-ul de proprietăți nu expune facilitățile
                    base_price=_price(item.get("base_price")) or 0.0,
                    currency=str(item.get("currency_code") or "EUR"),
                )
            )
        self._property_pricing = {
            prop.external_id: (prop.currency, prop.base_price) for prop in properties
        }
        return properties

    async def list_reservations(self) -> list[PmsReservation]:
        property_ids = [prop.external_id for prop in await self.list_properties()]
        if not property_ids:
            return []
        params = {"property_ids": ",".join(property_ids), "include_guest": "true"}
        reservations = []
        for item in await self._pages("/v2/bookings", params):
            if str(item.get("type") or "booking").lower() != "booking":
                continue  # blocări, rezervări ale proprietarului etc.
            try:
                guest = item.get("guest") or {}
                name = " ".join(
                    str(part) for part in (guest.get("first_name"), guest.get("last_name")) if part
                )
                guests = sum(int(item.get(key) or 0) for key in ("adults", "children", "infants"))
                reservations.append(
                    PmsReservation(
                        external_id=str(item["id"]),
                        property_external_id=str(item["property_id"]),
                        guest_name=name or f"Guest {item.get('guest_id') or ''}".strip(),
                        guest_count=max(1, guests),
                        check_in=str(item["arrival"])[:10],
                        check_out=str(item["departure"])[:10],
                        status=_status(item.get("status")),
                        source_channel=_channel(item.get("listing_site")),
                        total_price=item.get("total_amount"),
                        currency=str(item.get("currency_code") or "EUR"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping OwnerRez booking %s: %s", item.get("id"), exc)
        return reservations

    # --- MessagingPort -------------------------------------------------------------

    async def send_message(self, booking_id: str, body: str) -> SentMessage:
        if not self.capabilities.send_messages:
            raise CapabilityNotSupported("OwnerRez messaging needs an OAuth access token")
        external_id = self._external(RESERVATION, booking_id)
        # Mesajele aparțin unui thread, nu rezervării; rezervarea își expune `thread_ids`.
        booking = await self._request("GET", f"/v2/bookings/{external_id}")
        thread_ids = booking.get("thread_ids") if isinstance(booking, dict) else None
        if not thread_ids:
            raise PmsError(f"OwnerRez booking {external_id} has no message thread yet")
        await self._request("POST", "/v2/messages", json={"thread_id": thread_ids[0], "body": body})
        return SentMessage()

    def is_reservation_webhook(self, payload: dict[str, Any]) -> bool:
        return (
            self.capabilities.receive_webhooks
            and payload.get("entity_type") in ("booking", "reservation")
            and payload.get("action") in ("entity_create", "entity_update", "entity_delete")
        )

    def parse_webhook(self, payload: dict[str, Any]) -> InboundMessage | None:
        if payload.get("entity_type") != MESSAGE_ENTITY or payload.get("action") != "entity_create":
            return None
        entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
        if entity.get("is_draft"):
            return None
        thread = entity.get("thread") if isinstance(entity.get("thread"), dict) else {}
        message_id = payload.get("entity_id") or payload.get("id")
        if message_id is None:
            return None
        role = str(entity.get("from_role") or "").lower()
        booking_id = thread.get("booking_id")
        return InboundMessage(
            external_message_id=str(message_id),
            external_reservation_id=str(booking_id) if booking_id is not None else None,
            sender_type="guest" if "guest" in role else (role or "unknown"),
            body=str(entity.get("body") or ""),
            sent_at=entity.get("date_utc") if isinstance(entity.get("date_utc"), str) else None,
        )

    # --- CalendarPort --------------------------------------------------------------

    async def get_days(self, property_id: str, start: date, end: date) -> list[CalendarDay]:
        external_id = self._external(PROPERTY, property_id)
        if end < start:
            raise ValueError("calendar end must not be before start")
        if end == start:
            return []

        # OwnerRez folosește limite inclusive; contractul nostru exclude noaptea `end`.
        params = {
            "property_ids": external_id,
            "from": start.isoformat(),
            "to": (end - timedelta(days=1)).isoformat(),
        }
        bookings = await self._pages("/v2/bookings", params)
        occupied: set[date] = set()
        for item in bookings:
            if str(item.get("property_id", external_id)) != external_id:
                continue
            if _status(item.get("status")) == BookingStatus.CANCELLED:
                continue
            if item.get("type", "booking") not in (
                "booking", "block", "owner", "quote_hold", "linked_availability"
            ):
                continue
            try:
                arrival = date.fromisoformat(str(item["arrival"])[:10])
                departure = date.fromisoformat(str(item["departure"])[:10])
                if departure <= arrival:
                    raise ValueError("departure must follow arrival")
            except (KeyError, TypeError, ValueError) as exc:
                # Nu declarăm o noapte liberă dacă datele ocupării sunt incomplete.
                raise PmsError("OwnerRez returned a calendar entry with invalid dates") from exc
            day = max(start, arrival)
            while day < min(end, departure):
                occupied.add(day)
                day += timedelta(days=1)

        try:
            await self.list_properties()
        except PmsError:
            logger.warning(
                "OwnerRez property pricing unavailable; using cached pricing or EUR/zero defaults",
                exc_info=True,
            )
        currency, base_price = self._property_pricing.get(external_id, ("EUR", 0.0))

        rates: dict[date, float] = {}
        try:
            for item in await self._pages("/v2/spotrates", params):
                if str(item.get("property_id", external_id)) != external_id:
                    continue
                try:
                    day = date.fromisoformat(str(item["date"])[:10])
                except (KeyError, TypeError, ValueError):
                    continue
                price = _price(item.get("amount"))
                if start <= day < end and price is not None:
                    rates[day] = price
        except PmsError:
            logger.warning("OwnerRez spot rates unavailable; using the property base price", exc_info=True)

        return [
            CalendarDay(
                property_id=property_id,
                day=day,
                available=day not in occupied,
                price=rates.get(day, base_price),
                currency=currency,
            )
            for offset in range((end - start).days)
            for day in (start + timedelta(days=offset),)
        ]

    async def set_price(self, property_id: str, start: date, end: date, price: float) -> None:
        external_id = int(self._external(PROPERTY, property_id))
        nights = (end - start).days
        rates = [
            {"property_id": external_id, "date": (start + timedelta(days=i)).isoformat(), "amount": price}
            for i in range(nights)
        ]
        if rates:
            await self._request("PATCH", "/v2/spotrates", json=rates)

    async def set_availability(
        self, property_id: str, start: date, end: date, available: bool
    ) -> None:
        # POST type=block există, dar API-ul nu poate anula/șterge o blocare. Nu oferim
        # o capabilitate care poate închide nopți fără să le poată redeschide în siguranță.
        raise CapabilityNotSupported(
            "OwnerRez availability writes are disabled: blocks cannot be cancelled or deleted "
            "through the API; manage availability in OwnerRez"
        )

    async def aclose(self) -> None:
        await self._http.aclose()
