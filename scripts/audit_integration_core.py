"""Audit reproductibil, fără schimbări în codul aplicației sau în baza utilizatorului."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import date, timedelta
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from app.adapters.hospitable.adapter import HospitableAdapter
from app.adapters.ownerrez.adapter import OwnerRezAdapter
from app.core.contracts.events import ApprovalDecided, GuestMessageReceived
from app.core.contracts.models import Review, ReviewBatch
from app.core.clock import utcnow
from app.core.id_mapper import IdMapper, PROPERTY, RESERVATION
from app.ports.llm import ChatMessage, ChatResponse, ToolCall
from conftest import app_client, booking_by_guest, drain, make_settings

CASES = []


def case(name, expected):
    def register(fn):
        CASES.append((name, expected, fn))
        return fn
    return register


def messages(client, booking):
    return client.get(f"/bookings/{booking['id']}/messages").json()


def webhook(message_id, reservation="MOCK-RES-1001"):
    return {"event": "message.created", "message": {
        "id": message_id, "reservation_id": reservation, "sender_type": "guest",
        "body": "What time is check-in?",
    }}


@case("all_feature_switch_combinations", "Every enabled/disabled feature combination preserves the common API and route contracts")
def all_feature_switch_combinations(folder):
    names = ["f1_pms_integration", "f2_incidents", "f3_guest_copilot", "f4_review_analysis", "f5_relisting"]
    feature_paths = {names[0]: "/f1/webhook-log", names[1]: "/incidents", names[3]: "/f4/insights", names[4]: "/f5/proposals"}
    failures = []
    for mask in range(32):
        disabled = [name for index, name in enumerate(names) if mask & (1 << index)]
        with app_client(make_settings(folder / str(mask), disabled_features=disabled)) as client:
            health = client.get("/health").json()
            actual = {feature["name"]: feature["loaded"] for feature in health["features"]}
            expected = {name: name not in disabled for name in names}
            if actual != expected:
                failures.append({"mask": mask, "loaded": actual, "expected": expected})
            for path in ("/properties", "/bookings", "/host", "/approvals", "/notifications", "/events"):
                if client.get(path).status_code != 200:
                    failures.append({"mask": mask, "common_path": path})
            for name, path in feature_paths.items():
                status = client.get(path).status_code
                if status != (404 if name in disabled else 200):
                    failures.append({"mask": mask, "feature_path": path, "status": status})
            route_counts = Counter((method, route.path) for route in client.app.routes for method in getattr(route, "methods", ()))
            if any(value > 1 for value in route_counts.values()):
                failures.append({"mask": mask, "duplicate_routes": True})
            if len(client.get("/properties").json()) != 4 or len(client.get("/bookings").json()) != 7:
                failures.append({"mask": mask, "seed_integrity": False})
    return not failures, {"feature_combinations": 32, "failures": failures}


@case("parallel_all_features", "All concurrent flows and repeated decisions remain isolated and idempotent")
def parallel_all_features(folder):
    with app_client(make_settings(folder)) as client:
        c = client.app.state.container
        sofia = booking_by_guest(client, "Sofia Rossi")
        lukas = booking_by_guest(client, "Lukas Mueller")
        emma = booking_by_guest(client, "Emma Johnson")
        requests = [
            ("/webhooks/mock", webhook(f"AUDIT-{index}", "MOCK-RES-1002"))
            for index in range(12) for _ in range(3)
        ]
        requests += [("/sim/guest-message", {"booking_id": sofia["id"], "text": "What time is check-in?"}) for _ in range(6)]
        requests += [
            ("/sim/review-analysis", {"property_id": None}),
            ("/sim/staff-report", {"property_id": lukas["property_id"], "description": "The heating is broken"}),
            ("/sim/cancel-booking", {"booking_id": emma["id"]}),
        ]
        with ThreadPoolExecutor(max_workers=12) as executor:
            responses = list(executor.map(lambda item: client.post(item[0], json=item[1]), requests))
        joined = drain(client, timeout=30)
        pending = client.get("/approvals").json()
        resolution = next(a for a in pending if a["kind"] == "incident_resolution")
        with ThreadPoolExecutor(max_workers=8) as executor:
            decisions = list(executor.map(lambda _: client.post(
                f"/approvals/{resolution['id']}/decision", json={"decision": "approve", "option": "remediate"}
            ).json(), range(12)))
        drain(client, timeout=30)
        bad_handlers = c.db.query("SELECT handler, status FROM event_handler_runs WHERE status <> 'ok'")
        counts = {name: c.db.query_one(f"SELECT COUNT(*) AS n FROM {name}")["n"] for name in (
            "f1_seen_messages", "f2_incidents", "f4_insights", "f5_relist_proposals"
        )}
        applied = sum(result["applied"] for result in decisions)
        guest_events = c.db.query_one("SELECT COUNT(*) AS n FROM events WHERE type = 'GuestMessageReceived'")["n"]
        return (
            all(r.status_code == 200 for r in responses) and joined and not bad_handlers
            and applied == 1 and guest_events == 18 and counts["f4_insights"] == 4
            and counts["f2_incidents"] == 1 and counts["f5_relist_proposals"] == 1,
            {"http_requests": len(responses), "duplicate_decisions": 12, "applied_decisions": applied,
             "guest_events": guest_events, "counts": counts, "handler_errors": bad_handlers},
        )


@case("f1_event_failure_retry", "A retried webhook recovers from one transient publication failure")
def f1_event_failure_retry(folder):
    with app_client(make_settings(folder)) as client:
        c = client.app.state.container
        original = c.bus.publish
        failed = False

        async def fail_once(event):
            nonlocal failed
            if isinstance(event, GuestMessageReceived) and not failed:
                failed = True
                raise RuntimeError("injected transient event persistence failure")
            return await original(event)

        c.bus.publish = fail_once
        first = client.post("/webhooks/mock", json=webhook("AUDIT-PUBLISH")).json()
        second = client.post("/webhooks/mock", json=webhook("AUDIT-PUBLISH")).json()
        replay = client.post("/f1/reprocess-unmapped", params={"wait": True}).json()
        drain(client)
        event_count = c.db.query_one("SELECT COUNT(*) AS n FROM events WHERE type = 'GuestMessageReceived'")["n"]
        return event_count == 1, {"first": first, "retry": second, "replay": replay, "guest_events": event_count}


@case("f1_replay_record_failure", "Reprocessing never creates duplicate conversation records for one external message")
def f1_replay_record_failure(folder):
    with app_client(make_settings(folder)) as client:
        c = client.app.state.container
        booking = booking_by_guest(client, "Emma Johnson")
        first = client.post("/webhooks/mock", json=webhook("AUDIT-REPLAY", "AUDIT-UNKNOWN")).json()
        IdMapper.link(c.ids, "mock", RESERVATION, "AUDIT-UNKNOWN", booking["id"])
        original = c.bus.publish
        failed = False

        async def fail_once(event):
            nonlocal failed
            if isinstance(event, GuestMessageReceived) and not failed:
                failed = True
                raise RuntimeError("injected transient event failure during replay")
            return await original(event)

        c.bus.publish = fail_once
        replay1 = client.post("/f1/reprocess-unmapped", params={"wait": True}).json()
        replay2 = client.post("/f1/reprocess-unmapped", params={"wait": True}).json()
        inbound = [m for m in messages(client, booking) if m["direction"] == "inbound"]
        events = c.db.query_one("SELECT COUNT(*) AS n FROM events WHERE type = 'GuestMessageReceived'")["n"]
        return len(inbound) == 1 and events == 1, {"first": first, "replay1": replay1,
                "replay2": replay2, "inbound_records": len(inbound), "guest_events": events}


@case("approval_event_failure_retry", "A decision retry recovers a transient failure before its event was published")
def approval_event_failure_retry(folder):
    with app_client(make_settings(folder)) as client:
        c = client.app.state.container
        booking = booking_by_guest(client, "Emma Johnson")
        client.post("/sim/guest-message", params={"wait": True}, json={"booking_id": booking["id"], "text": "Could we check in early?"})
        approval = next(a for a in client.get("/approvals").json() if a["kind"] == "guest_request")
        original = c.bus.publish
        failed = False

        async def fail_once(event):
            nonlocal failed
            if isinstance(event, ApprovalDecided) and not failed:
                failed = True
                raise RuntimeError("injected transient decision event failure")
            return await original(event)

        c.bus.publish = fail_once
        try:
            client.post(f"/approvals/{approval['id']}/decision", json={"decision": "approve"})
            first_error = None
        except RuntimeError as exc:
            first_error = str(exc)
        retry = client.post(f"/approvals/{approval['id']}/decision", params={"wait": True}, json={"decision": "approve"}).json()
        event_count = c.db.query_one("SELECT COUNT(*) AS n FROM events WHERE type = 'ApprovalDecided'")["n"]
        confirmations = sum(m["body"] == approval["payload"]["proposed_reply"] for m in messages(client, booking))
        return event_count == 1 and confirmations == 1, {"first_error": first_error,
            "stored_status": retry["approval"]["status"], "retry_applied": retry["applied"],
            "decision_events": event_count, "confirmations": confirmations}


@case("foreign_provider_mapping", "A webhook cannot silently use a booking and reply channel from another PMS connection")
def foreign_provider_mapping(folder):
    calls = []
    with app_client(make_settings(folder)) as client:
        c = client.app.state.container
        booking = booking_by_guest(client, "Emma Johnson")

        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(201, json={"data": {"id": "AUDIT-SENT"}})

        c.pms.register(HospitableAdapter(base_url="https://hospitable.invalid/v2", token="audit-test-token",
                       ids=c.ids, transport=httpx.MockTransport(respond)))
        mapping = client.put("/integrations/mappings", json={"connection": "hospitable", "entity_type": "reservation",
            "external_id": "AUDIT-FOREIGN", "internal_id": booking["id"]})
        result = client.post("/webhooks/hospitable", json={"action": "message.created", "data": {
            "id": "AUDIT-FOREIGN-MESSAGE", "reservation_id": "AUDIT-FOREIGN", "sender_type": "guest", "body": "What time is check-in?"
        }}).json()
        drain(client)
        events = [e for e in client.get("/events").json() if e["type"] == "GuestMessageReceived"]
        provider = events[0]["payload"]["pms_provider"] if events else None
        return not events or provider == "hospitable", {"mapping_http": mapping.status_code, "webhook": result,
            "incoming_provider": "hospitable", "published_provider": provider, "hospitable_send_calls": len(calls),
            "mock_reply_count": sum(m["direction"] == "outbound" for m in messages(client, booking))}


@case("ownerrez_malformed_calendar", "A malformed successful bookings response must not be presented as free nights")
def ownerrez_malformed_calendar(folder):
    with app_client(make_settings(folder)) as client:
        c = client.app.state.container
        booking = booking_by_guest(client, "Emma Johnson")
        IdMapper.link(c.ids, "ownerrez", PROPERTY, "123", booking["property_id"])

        def respond(request):
            if request.url.path == "/v2/bookings":
                return httpx.Response(200, json={"unexpected": "bookings data is absent"})
            if request.url.path == "/v2/properties":
                return httpx.Response(200, json={"items": [{"id": 123, "name": "Audit property", "currency_code": "USD"}]})
            return httpx.Response(200, json={"items": []})

        async def run():
            adapter = OwnerRezAdapter(base_url="https://ownerrez.invalid", ids=c.ids, access_token="audit-test-token",
                                      transport=httpx.MockTransport(respond))
            try:
                return await adapter.get_days(booking["property_id"], date(2026, 6, 3), date(2026, 6, 5))
            finally:
                await adapter.aclose()

        try:
            days = client.portal.call(run)
            return not days or not any(d.available for d in days), {"returned_nights": len(days), "available": [d.available for d in days]}
        except Exception as exc:
            return True, {"rejected": type(exc).__name__}


@case("reset_with_active_review_analysis", "A successful reset cannot be repopulated by work from the previous world")
def reset_with_active_review_analysis(folder):
    started = threading.Event()

    class SlowReviews:
        async def fetch_reviews(self, property_id):
            started.set()
            await asyncio.sleep(11)
            return ReviewBatch(property_id=property_id, source="audit-before-reset", fetched_at=utcnow(), reviews=[
                Review(id="AUDIT-BEFORE-RESET", property_id=property_id, rating=1, text="The wifi is unreliable.", source="audit-before-reset")
            ])

    with app_client(make_settings(folder, handler_timeout_s=20, sim_wait_timeout_s=25), reviews=SlowReviews()) as client:
        c = client.app.state.container
        booking = booking_by_guest(client, "Emma Johnson")
        client.post("/sim/review-analysis", json={"property_id": booking["property_id"]})
        assert started.wait(timeout=3)
        before = time.monotonic()
        reset = client.post("/sim/reset")
        elapsed = time.monotonic() - before
        immediate = c.db.query_one("SELECT COUNT(*) AS n FROM f4_review_snapshots")["n"]
        joined = drain(client, timeout=6)
        snapshots = client.get("/f4/snapshots").json()
        return not snapshots, {"reset_http": reset.status_code, "reset_wait_s": round(elapsed, 2),
            "snapshots_immediately_after_reset": immediate, "snapshots_after_old_task_finished": len(snapshots),
            "old_sources": [s["source"] for s in snapshots], "drained": joined}


@case("direct_container_shutdown", "Container.shutdown drains replay tasks before closing SQLite")
def direct_container_shutdown(folder):
    from app.container import Container

    async def run():
        c = Container(make_settings(folder))
        await c.startup()
        prop = c.world.list_properties()[0]
        c.ids.link("airbnb", "listing", "AUDIT-FAST-SHUTDOWN", prop.id)
        pending = [task for task in asyncio.all_tasks() if task.get_name().startswith("f1-reprocess:")]
        await c.shutdown()
        results = await asyncio.gather(*pending, return_exceptions=True)
        errors = [f"{type(result).__name__}: {result}" for result in results if isinstance(result, BaseException)]
        return not errors, {"scheduled_tasks": len(pending), "errors_after_shutdown": errors}

    return asyncio.run(run())


@case("public_api_without_auth", "The declared general API authentication requirement is implemented")
def public_api_without_auth(folder):
    with app_client(make_settings(folder, webhook_token="audit-webhook-only-token")) as client:
        booking = booking_by_guest(client, "Emma Johnson")
        client.post("/sim/guest-message", params={"wait": True}, json={"booking_id": booking["id"], "text": "Could we check in early?"})
        approval = next(a for a in client.get("/approvals").json() if a["kind"] == "guest_request")
        decision = client.post(f"/approvals/{approval['id']}/decision", params={"wait": True}, json={"decision": "approve"})
        return decision.status_code in (401, 403), {"properties_without_credentials": client.get("/properties").status_code,
            "decision_without_credentials": decision.status_code, "declared_gap": "F4 plan step 5 explicitly not implemented; WEBHOOK_TOKEN protects webhooks only"}


@case("free_text_guard_paraphrase", "An unapproved pet exception must not be confirmed through an informational paraphrase")
def free_text_guard_paraphrase(folder):
    text = "Your dog can join you during the stay."

    class ParaphrasingLLM:
        name = "audit-controlled-paraphrase"

        async def chat(self, request):
            done = [call.name for message in request.messages for call in message.tool_calls]
            if "get_booking_context" not in done:
                call = ToolCall.make("get_booking_context", reason="Read the actual pet rules")
            elif "send_guest_reply" not in done:
                call = ToolCall.make("send_guest_reply", text=text, reason="Controlled audit of the free-text boundary")
            else:
                call = ToolCall.make("finish", result="Reply sent", evidence=[], reason="End the controlled audit")
            return ChatResponse(message=ChatMessage(role="assistant", tool_calls=[call]), model=self.name)

    with app_client(make_settings(folder), llm=ParaphrasingLLM()) as client:
        booking = booking_by_guest(client, "Emma Johnson")
        client.post("/sim/guest-message", params={"wait": True}, json={"booking_id": booking["id"], "text": "Could we bring our dog?"})
        sent = [message["body"] for message in messages(client, booking) if message["direction"] == "outbound"]
        approvals = client.get("/approvals").json()
        return text not in sent, {"pets_allowed": client.get("/host").json()["rules"]["pets_allowed"],
            "approval_count": len(approvals), "delivered": sent,
            "declared_limitation": "F3 explicitly documents the free-text filter as heuristic"}


def main():
    parser = argparse.ArgumentParser(description="Run isolated integration audit probes; no production data is used.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=[item[0] for item in CASES])
    args = parser.parse_args()
    log = logging.getLogger("staypilot")
    log.handlers[:] = [logging.NullHandler()]
    log.setLevel(logging.CRITICAL)
    results = []
    for name, expected, fn in CASES:
        if args.case and args.case != name:
            continue
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="staypilot-core-audit-") as folder:
            try:
                passed, observations = fn(Path(folder))
                result = {"case": name, "expected": expected, "passed": bool(passed), "observations": observations}
            except Exception as exc:
                result = {"case": name, "expected": expected, "passed": False, "probe_error": f"{type(exc).__name__}: {exc}"}
        result["elapsed_s"] = round(time.perf_counter() - started, 3)
        results.append(result)
        print(f"{'PASS' if result['passed'] else 'ISSUE'} {name}")
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(results, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
