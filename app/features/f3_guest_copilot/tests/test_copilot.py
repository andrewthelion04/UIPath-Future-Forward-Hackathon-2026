"""F3: mesaj de la oaspete -> bucla agentului cu unelte care citesc datele -> răspuns trimis."""

from __future__ import annotations

import pytest

from app.core.agent.tools import RunContext
from app.features.f3_guest_copilot.mock_llm import classify
from app.features.f3_guest_copilot.tools import MAX_REPLIES_PER_RUN, build_tools
from app.ports.llm import ChatRequest, ChatResponse, LLMError
from conftest import app_client, approve, booking_by_guest, events_of, reject


def guest_says(client, guest_name: str, text: str) -> dict:
    booking = booking_by_guest(client, guest_name)
    result = client.post(
        "/sim/guest-message", params={"wait": True}, json={"booking_id": booking["id"], "text": text}
    ).json()
    assert result["outcome"] == "published" and result["completed"] is True
    return booking


def conversation(client, booking: dict) -> list[tuple[str, str]]:
    messages = client.get(f"/bookings/{booking['id']}/messages").json()
    return [(m["sender"], m["body"]) for m in messages]


def last_run(client) -> dict:
    run_id = client.get("/agent/runs", params={"feature": "f3_guest_copilot"}).json()[0]["id"]
    return client.get(f"/agent/runs/{run_id}").json()


def test_question_is_answered_from_our_data(client):
    emma = guest_says(client, "Emma Johnson", "Hi! What time is check-in, and is there wifi?")

    (_, question), (sender, reply) = conversation(client, emma)
    assert sender == "copilot"
    # Cifrele vin din unelte: ora din regulile hostului, data din rezervare, adresa din proprietate.
    assert "15:00" in reply and emma["check_in"] in reply and "Strada Lipscani 12" in reply
    assert "Wi-Fi" in reply

    run = last_run(client)
    assert (run["status"], run["booking_id"]) == ("finished", emma["id"])
    tools = [s["tool_name"] for s in run["steps_detail"] if s["kind"] in ("tool", "finish")]
    assert tools == ["get_booking_context", "send_guest_reply", "finish"]
    assert all(s["reason"] for s in run["steps_detail"] if s["kind"] in ("tool", "finish"))
    assert run["evidence"] and run["prompt_tokens"] > 0
    assert client.get("/approvals").json() == []  # un răspuns informativ nu cere aprobare


def test_early_check_in_needs_the_hosts_approval(client):
    emma = guest_says(client, "Emma Johnson", "Could we check in early, around noon?")

    # Agentul a verificat noaptea dinainte, a cerut aprobarea și NU a promis nimic.
    tools = [s["tool_name"] for s in last_run(client)["steps_detail"] if s["kind"] == "tool"]
    assert tools == ["get_booking_context", "check_calendar", "request_host_approval", "send_guest_reply"]
    holding = conversation(client, emma)[-1][1]
    assert "passed your request" in holding and "confirmed" not in holding

    (approval,) = client.get("/approvals").json()
    assert (approval["feature"], approval["kind"]) == ("f3_guest_copilot", "guest_request")
    assert "The night of 2026-06-02 is free" in approval["summary"]
    assert approval["booking_id"] == emma["id"]

    approve(client, approval["id"])

    sender, confirmation = conversation(client, emma)[-1]
    assert sender == "copilot" and "confirmed an early check-in" in confirmation and "12:00" in confirmation
    assert any("approved" in n["title"] for n in client.get("/notifications").json())


def test_declined_request_is_communicated_politely(client):
    lukas = guest_says(client, "Lukas Mueller", "Any chance of a late check-out on our last day?")
    (approval,) = client.get("/approvals").json()
    assert "late check-out" in approval["summary"]

    reject(client, approval["id"])

    assert "cannot accommodate" in conversation(client, lukas)[-1][1]
    assert len(conversation(client, lukas)) == 3  # întrebare, răspuns de așteptare, refuz


def test_reported_problem_reaches_the_incident_flow_through_an_event(client):
    """F3 nu cunoaște F2: emite `IssueReported`, iar F2 îl preia."""
    lukas = guest_says(client, "Lukas Mueller", "The air conditioning is broken and it is very hot.")

    (event,) = events_of(client, "IssueReported")
    assert event["source"] == "feature:f3_guest_copilot"
    assert event["payload"]["reported_via"] == "guest" and event["payload"]["booking_id"] == lukas["id"]
    assert [h["handler"] for h in event["handlers"]] == ["f2_incidents.on_issue_reported"]

    (incident,) = client.get("/incidents").json()
    assert incident["booking_id"] == lukas["id"] and "air conditioning" in incident["description"]
    assert "informed the host" in conversation(client, lukas)[-1][1]
    # Tot fluxul împarte aceeași corelare, de la mesaj până la cererea de aprobare.
    (approval,) = client.get("/approvals").json()
    assert approval["correlation_id"] == event["correlation_id"]


class FailingLLM:
    name = "failing"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        raise LLMError("model unavailable")


def test_llm_failure_falls_back_to_the_host_and_does_not_break_other_features(settings):
    with app_client(settings, llm=FailingLLM()) as client:
        emma = guest_says(client, "Emma Johnson", "What time is check-in?")

        assert conversation(client, emma) == [("guest", "What time is check-in?")]  # niciun răspuns inventat
        (answer,) = client.get("/answers").json()
        assert answer["question"] == "What time is check-in?"
        assert "model unavailable" in answer["context_note"]
        assert last_run(client)["status"] == "error"

        # Fluxurile fără LLM merg mai departe.
        studio_id = emma["property_id"]
        client.post("/sim/staff-report", params={"wait": True},
                    json={"property_id": studio_id, "description": "Broken lamp"})  # fmt: skip
        assert len(client.get("/incidents").json()) == 1


async def test_reply_and_approval_limits_are_enforced_in_code(container):
    ctx = container._make_feature_context("f3_guest_copilot")
    tools = {tool.name: tool for tool in build_tools(ctx)}
    emma = next(b for b in container.world.list_bookings() if b.guest_name == "Emma Johnson")
    run = RunContext("run_test", "test", {"booking_id": emma.id, "property_id": emma.property_id})

    send = tools["send_guest_reply"]
    for _ in range(MAX_REPLIES_PER_RUN):
        await send.handler(send.args_model(text="Hello", reason="test"), run)
    with pytest.raises(RuntimeError, match="reply limit"):
        await send.handler(send.args_model(text="Again", reason="test"), run)
    assert len(container.conversations.history(emma.id)) == MAX_REPLIES_PER_RUN

    ask = tools["request_host_approval"]
    args = ask.args_model(request={"request_type": "early_check_in"}, reason="test")
    await ask.handler(args, run)
    with pytest.raises(RuntimeError, match="already requested"):
        await ask.handler(args, run)

    calendar = tools["check_calendar"]
    with pytest.raises(ValueError, match="range too large"):
        await calendar.handler(
            calendar.args_model(start_date="2026-06-01", end_date="2026-08-01", reason="test"), run
        )


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("What time is check-in?", "info"),
        ("Can we arrive early on Friday?", "early_check_in"),
        ("Is a late check-out possible?", "late_check_out"),
        ("The shower is not working", "issue"),
        ("There is a leak in the bathroom and we would like an early check-in next time", "issue"),
    ],
)
def test_mock_policy_intents(text, intent):
    assert classify(text) == intent
