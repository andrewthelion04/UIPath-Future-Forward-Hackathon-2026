"""Copilotul poartă discuția despre un incident: întrebări, link, răspuns la ofertă, predare.

Copilotul nu cunoaște feature-ul de incidente: vede doar `open_topics` din contextul rezervării
și emite `GuestOfferAnswered`. Fluxurile cap-coadă trec prin API (sesizare, aprobare, chat).
"""

from __future__ import annotations

import pytest

from app.core.agent.tools import RunContext
from app.core.contracts.events import GuestOfferAnswered
from app.core.contracts.models import GuestOffer
from app.features.f3_guest_copilot.tools import build_tools, offer_acknowledgement
from conftest import app_client, approve, booking_by_guest, events_of, property_by_name

FEATURE = "f3_guest_copilot"
AC = "The air conditioning is not working"


# --- Uneltele, direct ---------------------------------------------------------------------------


@pytest.fixture
def desk(container):
    """Uneltele copilotului pe rezervarea Emmei, cu un subiect publicat de alt feature."""
    ctx = container._make_feature_context(FEATURE)  # noqa: SLF001
    booking = next(b for b in container.world.list_bookings() if b.guest_name == "Emma Johnson")
    tools = {tool.name: tool for tool in build_tools(ctx)}

    async def call(name, run=None, **arguments):
        tool = tools[name]
        state = {"booking_id": booking.id, "property_id": booking.property_id}
        return await tool.handler(
            tool.args_model(reason="test", **arguments), run or RunContext("run_t", FEATURE, state)
        )

    def publish(topic_id="other:incident:1", booking_id=None, **overrides):
        values = {"booking_id": booking_id or booking.id, "feature": "other", "kind": "incident",
                  "title": "Problem at the studio", "facts": {"status": "being assessed"}}  # fmt: skip
        return container.guest_topics.publish(topic_id, **(values | overrides))

    return container, booking, call, publish


OFFER = GuestOffer(question="Move to B?", details={"name": "B", "listing_url": "https://x/1"})


async def test_the_booking_context_lists_open_topics_and_the_listing_link(desk):
    container, booking, call, publish = desk
    assert (await call("get_booking_context"))["open_topics"] == []

    publish(offer=OFFER)
    publish("other:incident:2", booking_id="bk_someone_else")
    context = await call("get_booking_context")

    (topic,) = context["open_topics"]
    assert topic["topic_id"] == "other:incident:1" and topic["facts"] == {"status": "being assessed"}
    assert topic["offer"] == {"status": "pending", "question": "Move to B?",
                              "details": {"name": "B", "listing_url": "https://x/1"}, "expires_at": None}  # fmt: skip
    assert context["property"]["listing_url"].startswith("https://www.airbnb.com/rooms/")


async def test_answer_offer_acknowledges_first_then_tells_the_owner_once(desk):
    container, booking, call, publish = desk
    publish(offer=OFFER)
    run = RunContext("run_t", FEATURE, {"booking_id": booking.id, "property_id": booking.property_id})

    result = await call("answer_offer", run=run, topic_id="other:incident:1", choice="accept")
    await container.bus.join(timeout=5)

    assert result["recorded"] is True and result["acknowledgement_sent"] is True
    assert run.state["offer_answered"] is True and run.state["replies_sent"] == 1
    (message,) = container.conversations.history(booking.id)
    assert message.body == offer_acknowledgement(booking.guest_name, "accept")
    assert "confirm" not in message.body.lower()  # nu afirmă rezultatul
    events = [e for e in container.bus.list_events() if e["type"] == "GuestOfferAnswered"]
    assert [(e["payload"]["feature"], e["payload"]["choice"]) for e in events] == [("other", "accept")]

    with pytest.raises(ValueError, match="no offer waiting"):
        await call("answer_offer", topic_id="other:incident:1", choice="decline")
    assert len([e for e in container.bus.list_events() if e["type"] == "GuestOfferAnswered"]) == 1


async def test_answer_offer_refuses_topics_it_must_not_touch(desk):
    container, booking, call, publish = desk
    publish("other:incident:mine")  # fără ofertă
    publish("other:incident:theirs", booking_id="bk_someone_else", offer=OFFER)

    with pytest.raises(LookupError):
        await call("answer_offer", topic_id="other:incident:missing", choice="accept")
    with pytest.raises(LookupError):  # oferta altei rezervări
        await call("answer_offer", topic_id="other:incident:theirs", choice="accept")
    with pytest.raises(ValueError, match="no offer waiting"):
        await call("answer_offer", topic_id="other:incident:mine", choice="accept")

    assert container.conversations.history(booking.id) == []
    assert [e for e in container.bus.list_events() if e["type"] == GuestOfferAnswered.event_type()] == []


# --- Garda pe text liber: informația trece, promisiunile nu ------------------------------------


@pytest.mark.parametrize("text", [
    "You can see photos and details of Herastrau Family Flat here: https://www.airbnb.com/rooms/1063624740877318380.",
    "Parking is listed among its amenities. I don't have further details on how the parking works.",
    "Of course! The flat has 2 bedrooms and sleeps 5, at no extra cost.",
    "You're welcome! Feel free to ask if anything else comes up.",
    "The repair is scheduled with our maintenance team and should be done in about 30 hours.",
    "The host offered you a move to Herastrau Family Flat. Would you like to move there?",
])
async def test_informational_replies_about_a_topic_are_delivered(desk, text):
    _, _, call, _ = desk
    assert (await call("send_guest_reply", text=text))["sent"] is True


@pytest.mark.parametrize("text", [
    "Your dog can join you during the stay.",  # parafraza din audit (D06)
    "Your move to Herastrau Family Flat is confirmed.",  # rezultatul îl confirmă echipa hostului
    "We guarantee a full refund for the trouble.",
    "No problem, you can leave at 14:00.",
    "Of course, a discount is fine.",
])
async def test_promises_are_still_blocked_whatever_the_wording(desk, text):
    container, booking, call, _ = desk
    with pytest.raises(ValueError):
        await call("send_guest_reply", text=text)
    assert container.conversations.history(booking.id) == []


async def test_a_handover_tells_the_host_what_the_guest_wants(desk):
    container, booking, call, _ = desk
    await call("send_guest_reply", text="The host will contact you directly about this.",
               host_follow_up=True, follow_up_note="Guest asks for a refund instead of moving.")  # fmt: skip

    answer = container.host_answers.list()[0]
    assert "Guest asks for a refund instead of moving." in answer["context_note"]


# --- Conversația cap-coadă, pe politica mock ----------------------------------------------------


@pytest.fixture
def offered(settings, monkeypatch):
    """Lukas are o ofertă de relocare aprobată de host și încă niciun răspuns."""
    monkeypatch.setenv("F2_SIM_GUEST_ANSWER", "off")
    with app_client(settings) as client:
        apartment = property_by_name(client, "Calea Victoriei Apartment")
        body = {"property_id": apartment["id"], "description": AC, "staff_id": "staff_maria"}
        assert client.post("/sim/staff-report", params={"wait": True}, json=body).json()["completed"]
        (incident,) = client.get("/incidents").json()
        approve(client, incident["approval_id"], "relocate")
        yield client, booking_by_guest(client, "Lukas Mueller"), incident["id"]


def say(client, booking: dict, text: str) -> str:
    """Oaspetele scrie; întoarce ultimul mesaj primit de el."""
    body = {"booking_id": booking["id"], "text": text}
    assert client.post("/sim/guest-message", params={"wait": True}, json=body).json()["completed"]
    return client.get(f"/bookings/{booking['id']}/messages").json()[-1]["body"]


def status(client, incident_id: str) -> str:
    return client.get(f"/incidents/{incident_id}").json()["status"]


def last_tools(client) -> list[str]:
    run = client.get("/agent/runs", params={"feature": FEATURE, "limit": 1}).json()[0]
    steps = client.get(f"/agent/runs/{run['id']}").json()["steps_detail"]
    return [s["tool_name"] for s in steps if s["kind"] == "tool"]


def test_questions_about_the_offer_are_answered_with_the_link_and_decide_nothing(offered):
    client, lukas, incident_id = offered
    target = property_by_name(client, "Herastrau Family Flat")

    reply = say(client, lukas, "Do you have photos of the other apartment? Does it have parking?")

    assert target["listing_url"] in reply and target["address"] in reply and "parking" in reply
    assert "no extra cost" in reply
    assert last_tools(client) == ["get_booking_context", "send_guest_reply"]
    assert status(client, incident_id) == "awaiting_guest"  # o întrebare nu e un răspuns


def test_a_question_that_mentions_staying_is_not_taken_as_a_refusal(offered):
    client, lukas, incident_id = offered

    reply = say(client, lukas, "When will the AC be fixed if we stay?")

    assert "about 30 hours" in reply and status(client, incident_id) == "awaiting_guest"
    assert len(client.get("/incidents").json()) == 1
    assert len(events_of(client, "IssueReported")) == 1  # problema deschisă nu e raportată din nou


def test_accepting_in_the_chat_relocates_the_guest(offered):
    client, lukas, incident_id = offered
    target = property_by_name(client, "Herastrau Family Flat")

    say(client, lukas, "Yes, I accept the relocation. Please move us to the offered apartment.")

    assert status(client, incident_id) == "relocated"
    assert last_tools(client) == ["get_booking_context", "answer_offer"]
    history = client.get(f"/bookings/{lukas['id']}/messages").json()
    acknowledgement, confirmation = history[-2], history[-1]
    assert acknowledgement["sender"] == "copilot" and "passed your answer" in acknowledgement["body"]
    # Rezultatul vine separat, cu textul aprobat de host, după confirmarea de primire.
    assert confirmation["sender"] == "host" and target["address"] in confirmation["body"]
    assert len(events_of(client, "GuestOfferAnswered")) == 1
    assert not any("reply manually" in n["title"] for n in client.get("/notifications").json())


def test_declining_in_the_chat_keeps_the_guest_and_sends_the_approved_apology(offered):
    client, lukas, incident_id = offered

    say(client, lukas, "No thanks, we would rather stay here.")

    incident = client.get(f"/incidents/{incident_id}").json()
    assert (incident["status"], incident["guest_answer"]) == ("remediation_scheduled", "stay")
    history = client.get(f"/bookings/{lukas['id']}/messages").json()
    assert "prefer to stay" in history[-2]["body"] and history[-1]["sender"] == "host"


def test_an_unhappy_guest_is_handed_over_to_the_host_without_promises(offered):
    client, lukas, incident_id = offered

    reply = say(client, lukas, "I am not happy with this. I want a refund instead.")

    assert "host, who can reply here" in reply and "refund" not in reply
    assert status(client, incident_id) == "awaiting_guest"
    follow_up = client.get("/answers").json()[0]
    assert "I want a refund instead" in follow_up["question"] and follow_up["booking_id"] == lukas["id"]


def test_after_the_move_the_guest_is_told_the_new_address(offered):
    client, lukas, incident_id = offered
    target = property_by_name(client, "Herastrau Family Flat")
    say(client, lukas, "Ok, let's move.")
    assert status(client, incident_id) == "relocated"

    reply = say(client, lukas, "What is the address of my apartment?")

    assert target["address"] in reply and "Calea Victoriei 88" not in reply


def test_without_an_open_topic_the_copilot_behaves_as_before(client):
    emma = booking_by_guest(client, "Emma Johnson")

    reply = say(client, emma, "Yes, ok. What time is check-in?")

    assert "15:00" in reply and last_tools(client) == ["get_booking_context", "send_guest_reply"]
    assert events_of(client, "GuestOfferAnswered") == []


def test_a_polite_ok_is_not_taken_as_accepting_the_move(offered):
    client, lukas, incident_id = offered

    say(client, lukas, "Ok, thanks for the info.")

    assert status(client, incident_id) == "awaiting_guest"
    assert events_of(client, "GuestOfferAnswered") == []
