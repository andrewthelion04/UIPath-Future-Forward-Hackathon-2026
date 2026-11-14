"""Regulile F2 ca funcții pure, plus simularea și migrarea schemei."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.contracts.models import Property
from app.core.db import Database
from app.features.f2_incidents.mock_llm import same_problem
from app.features.f2_incidents.repo import ensure_columns
from app.features.f2_incidents.rules import (
    fix_in_time,
    friendly_hours,
    hours_until_check_in,
    is_compatible,
    parse_triage_result,
    problem_phrase,
    recommend,
    valid_eta,
    valid_guest_message,
)
from app.features.f2_incidents.simulation import category_of, parse_hours, scripted_staff_reply


def test_compatibility_rule():
    original = Property(id="a", name="A", city="Bucharest", bedrooms=1, max_guests=2)
    same_city = Property(id="b", name="B", city="bucharest ", bedrooms=2, max_guests=4)

    assert is_compatible(same_city, original, guest_count=2)
    assert not is_compatible(original, original, guest_count=2)  # aceeași proprietate
    assert not is_compatible(same_city.model_copy(update={"city": "Brasov"}), original, 2)
    assert not is_compatible(same_city.model_copy(update={"max_guests": 1}), original, 2)
    assert not is_compatible(same_city.model_copy(update={"bedrooms": 0}), original, 2)


def test_hours_until_check_in():
    now = datetime(2026, 6, 1, 10, 0)
    assert hours_until_check_in(date(2026, 6, 3), "15:00", now) == 53
    assert hours_until_check_in(date(2026, 5, 31), "15:00", now) < 0  # deja cazat
    assert hours_until_check_in(date(2026, 6, 1), "invalid", now) == 5  # implicit 15:00


@pytest.mark.parametrize(
    ("eta", "hours_until", "in_house", "expected"),
    [
        (None, 50, False, False),  # estimare necunoscută: niciodată „la timp”
        (30, 53, False, True),  # 30 + 2 <= 53
        (52, 53, False, False),  # 52 + 2 > 53
        (3, -10, True, True),  # cazat, sub pragul tolerabil
        (4, -10, True, False),
        (100, None, False, True),  # fără oaspete afectat
    ],
)
def test_fix_in_time(eta, hours_until, in_house, expected):
    assert fix_in_time(eta, hours_until, in_house, margin_hours=2, inhouse_tolerable_hours=3) is expected


def test_recommendation_offers_relocation_only_when_needed_and_possible():
    assert recommend(has_booking=True, in_time=False, relocation_available=True) == "relocate"
    assert recommend(has_booking=True, in_time=True, relocation_available=True) == "remediate"
    assert recommend(has_booking=True, in_time=False, relocation_available=False) == "remediate"
    assert recommend(has_booking=False, in_time=False, relocation_available=True) == "remediate"


def test_guest_message_validation():
    assert valid_guest_message("Hi Lukas, we are sorry for the problem.")
    assert not valid_guest_message("")
    assert not valid_guest_message("Hi [guest name], sorry.")
    assert not valid_guest_message("Hi {name}, sorry.")
    assert not valid_guest_message("x" * 1201)


def test_triage_result_must_name_an_open_incident():
    assert parse_triage_result("attach:inc_1", {"inc_1"}) == "inc_1"
    assert parse_triage_result(" attach : inc_1 ", {"inc_1"}) == "inc_1"
    assert parse_triage_result("attach:inc_9", {"inc_1"}) is None  # ID inventat -> incident nou
    assert parse_triage_result("new", {"inc_1"}) is None
    assert parse_triage_result("I think attach:inc_1", {"inc_1"}) is None
    assert parse_triage_result(None, {"inc_1"}) is None


def test_eta_validation_and_wording():
    assert valid_eta(2) == 2 and valid_eta(None) is None
    assert valid_eta(0) is None and valid_eta(-1) is None and valid_eta(10_000) is None
    assert friendly_hours(0.5) == "about 30 minutes"
    assert friendly_hours(30) == "about 30 hours"
    assert friendly_hours(72) == "about 3 days"
    assert problem_phrase("The AC is broken.") == "the AC is broken"
    assert problem_phrase("AC broken") == "AC broken"


def test_simulated_staff_and_mock_parsing():
    assert category_of("AC broken!") == category_of("No cooling in the bedroom") == "climate"
    assert category_of("Water is leaking under the kitchen sink.") == "leak"
    assert category_of("Something is wrong with the oven") is None
    assert parse_hours(scripted_staff_reply("AC broken", 0)) == 30
    assert parse_hours(scripted_staff_reply("Broken lamp", 0)) == 0.5
    assert parse_hours(scripted_staff_reply("The oven", 0)) is None  # cere o clarificare
    assert parse_hours(scripted_staff_reply("The oven", 1)) == 4
    assert parse_hours("two to three hours") == 3
    assert parse_hours("half an hour") == 0.5
    assert same_problem("AC broken", "no cooling, very hot")
    assert not same_problem("Water leak in the kitchen", "Broken lamp in the bedroom")


def test_an_old_incidents_table_gets_the_new_columns(tmp_path):
    db = Database(tmp_path / "old.db")
    db.script(
        "CREATE TABLE f2_incidents (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, "
        "booking_id TEXT, description TEXT NOT NULL, reported_via TEXT NOT NULL, "
        "reporter TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL DEFAULT 'medium', "
        "status TEXT NOT NULL, relocation_property_id TEXT, approval_id TEXT, resolution TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
    )
    ensure_columns(db)
    columns = {row["name"] for row in db.query("PRAGMA table_info(f2_incidents)")}
    assert {"correlation_id", "eta_hours", "drafts", "report_count", "closure_round"} <= columns
    ensure_columns(db)  # idempotent
