"""Seed-ul lumii simulate: încărcare, validare și populare.

Lumea mock (PMS, recenzii, piață) se încarcă în tabelele `mock_*`, apoi proprietățile și
rezervările ajung în datele interne prin `SyncService.sync("mock")` — exact drumul pe care
îl parcurge și un PMS real, cu tot cu maparea ID-urilor.

Rulare:  python manage.py seed
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.adapters.mock.world import MockWorldSeed
from app.core.contracts.models import Host, HostRules, StaffMember
from app.core.id_mapper import PROPERTY

if TYPE_CHECKING:
    from app.container import Container


class SeedError(ValueError):
    pass


class SeedHost(BaseModel):
    id: str
    name: str
    rules: HostRules = Field(default_factory=HostRules)


class SeedStaff(BaseModel):
    id: str
    name: str
    role: Literal["cleaner", "maintenance", "manager"]
    property_external_ids: list[str] = Field(default_factory=list)


class WorldSeed(MockWorldSeed):
    """Seed-ul complet: datele interne (host, personal) plus lumea externă simulată."""

    host: SeedHost
    staff: list[SeedStaff] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_staff(self) -> WorldSeed:
        known = {prop.external_id for prop in self.properties}
        problems = [
            f"staff {member.id}: unknown property {ref}"
            for member in self.staff
            for ref in member.property_external_ids
            if ref not in known
        ]
        ids = [member.id for member in self.staff]
        problems += [f"duplicate staff id: {i}" for i in {i for i in ids if ids.count(i) > 1}]
        if problems:
            raise ValueError("incoherent seed: " + "; ".join(problems))
        return self


def parse_seed(data: Any) -> WorldSeed:
    try:
        return WorldSeed.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(str(p) for p in err['loc']) or 'seed'}: "
            f"{err['msg'].removeprefix('Value error, ')}"
            for err in exc.errors()
        ]
        raise SeedError("Invalid seed:\n" + "\n".join(lines)) from exc


def load_seed(path: Path) -> WorldSeed:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError(f"Cannot read seed file {path}: {exc}") from exc
    return parse_seed(data)


async def seed_world(container: Container, *, reset: bool) -> dict[str, Any]:
    """Populează lumea. Cu `reset=True` șterge întâi toate datele (inclusiv ale feature-urilor)."""
    seed = load_seed(container.settings.seed_path)  # validăm înainte să ștergem ceva
    if reset:
        container.db.drop_all_tables()
        container.init_schema()

    container.world.upsert_host(Host(id=seed.host.id, name=seed.host.name, rules=seed.host.rules))
    container.mock_world.load(seed, container.clock.today())
    report = await container.sync.sync("mock", publish_events=False)

    for member in seed.staff:
        property_ids = [
            container.ids.to_internal("mock", PROPERTY, ref) for ref in member.property_external_ids
        ]
        container.world.upsert_staff(
            StaffMember(
                id=member.id,
                name=member.name,
                role=member.role,
                property_ids=[pid for pid in property_ids if pid],
            )
        )
    return {
        "host": seed.host.name,
        "properties": len(seed.properties),
        "bookings": len(seed.reservations),
        "staff": len(seed.staff),
        "reviews": len(seed.reviews),
        "comparables": len(seed.comparables),
        "sync": report.model_dump(),
    }


def main() -> int:
    from app.config import ConfigError, load_settings
    from app.container import Container

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    async def run() -> dict[str, Any]:
        container = Container(settings)
        try:
            return await seed_world(container, reset=True)
        finally:
            await container.shutdown()

    try:
        summary = asyncio.run(run())
    except SeedError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"World seeded in {settings.database_path}")
    for key in ("host", "properties", "bookings", "staff", "reviews", "comparables"):
        print(f"  {key}: {summary[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
