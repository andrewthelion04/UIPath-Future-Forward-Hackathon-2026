"""Dashboardul agentului: indicatorii, lista fluxurilor, povestea unui flux și jurnalul deciziilor.

Totul se reconstituie din jurnalele nucleului (`events`, `agent_runs`, `agent_steps`,
`approvals`, `notifications`, `messages`), deci testele pornesc fluxuri reale pe mock și
verifică ce povestește dashboardul despre ele.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.config import ROOT_DIR
from app.ports.llm import ChatMessage, ChatRequest, ChatResponse, ToolCall, Usage
from conftest import app_client, approve, booking_by_guest, property_by_name
from ui import api_client

PAGE = ROOT_DIR / "ui" / "views" / "agent_dashboard.py"


def say(client, guest: str, text: str) -> dict:
    booking = booking_by_guest(client, guest)
    body = {"booking_id": booking["id"], "text": text}
    assert client.post("/sim/guest-message", params={"wait": True}, json=body).json()["completed"]
    return booking


@pytest.fixture
def busy(settings, monkeypatch):
    """O întrebare simplă, o cerere cu aprobare și un incident cu ofertă acceptată din chat."""
    monkeypatch.setenv("F2_SIM_GUEST_ANSWER", "off")
    with app_client(settings) as client:
        say(client, "Emma Johnson", "What time is check-in?")
        say(client, "Emma Johnson", "Could we check in early, around noon?")
        apartment = property_by_name(client, "Calea Victoriei Apartment")
        body = {"property_id": apartment["id"], "description": "The air conditioning is not working",
                "staff_id": "staff_maria"}  # fmt: skip
        client.post("/sim/staff-report", params={"wait": True}, json=body)
        (incident,) = client.get("/incidents").json()
        approve(client, incident["approval_id"], "relocate")
        say(client, "Lukas Mueller", "Yes, ok, let's move.")
        yield client


def flow_of(client, kind: str, text: str = "") -> dict:
    flows = client.get("/agent/flows").json()
    return next(f for f in flows if f["kind"] == kind and text in f["title"])


def test_an_empty_world_has_nothing_to_show(client):
    overview = client.get("/agent/overview").json()

    assert overview["flows"] == 0 and overview["agent_runs"] == 0 and overview["median_reply_s"] is None
    assert client.get("/agent/flows").json() == [] and client.get("/agent/decisions").json() == []
    assert client.get("/agent/flows/evt_missing").status_code == 404


def test_overview_separates_autonomous_replies_from_host_approved_actions(busy):
    overview = busy.get("/agent/overview").json()

    assert overview["flows"] == 4
    assert overview["guest_messages"] == 3
    # Răspunsul informativ, mesajul „am trimis cererea la host” și confirmarea de primire a ofertei.
    assert overview["replies_autonomous"] == 3
    # Oferta de relocare și confirmarea mutării: texte aprobate de host.
    assert overview["messages_host_approved"] == 2
    assert overview["approvals"] == {"pending": 2, "approved": 1, "rejected": 0, "withdrawn": 0}
    assert overview["agent_runs"] == overview["agent_runs_finished"] > 0
    assert overview["tool_calls"] > 0 and overview["corrected_by_code"] == 0
    assert overview["handler_problems"] == 0 and overview["median_reply_s"] is not None


def test_flows_have_readable_titles_and_a_status(busy):
    flows = busy.get("/agent/flows").json()

    assert [f["last_at"] for f in flows] == sorted((f["last_at"] for f in flows), reverse=True)
    incident = flow_of(busy, "incident")
    assert incident["title"] == "Incident · Calea Victoriei Apartment · “The air conditioning is not working”"
    assert (incident["guest_name"], incident["property_name"]) == ("Lukas Mueller", "Calea Victoriei Apartment")
    assert incident["status"] == "waiting_for_host"  # închiderea așteaptă hostul
    assert (incident["approvals"], incident["approvals_pending"], incident["guest_messages_sent"]) == (2, 1, 2)
    question = flow_of(busy, "conversation", "What time is check-in?")
    assert question["status"] == "done" and question["approvals"] == 0 and question["agent_runs"] == 1
    assert flow_of(busy, "conversation", "early")["status"] == "waiting_for_host"


def test_the_story_of_a_flow_explains_every_step(busy):
    incident = flow_of(busy, "incident")
    story = busy.get(f"/agent/flows/{incident['correlation_id']}").json()
    items = story["items"]

    assert [item["at"] for item in items] == sorted(item["at"] for item in items)
    first = items[0]
    assert (first["actor"], first["minor"]) == ("staff", False)
    assert first["title"] == "Maria Ionescu reported a problem at Calea Victoriei Apartment"

    runs = [item for item in items if item["kind"] == "agent_run"]
    assert [run["agent"].split(".")[-1] for run in runs][:4] == ["staff_liaison", "sim_staff", "staff_liaison", "proposal"]
    # Fiecare decizie a agentului are motivul notat când a fost luată.
    assert all(step["reason"] for run in runs for step in run["steps"])
    assert "send_message_to_staff" in [step["tool"] for step in runs[0]["steps"]]

    asked = next(item for item in items if item["kind"] == "approval_requested")
    assert asked["actor"] == "agent" and "Recommendation: Offer relocation" in asked["detail"]
    assert [o["key"] for o in asked["options"]] == ["relocate", "remediate"]
    assert (asked["status"], asked["chosen_option"]) == ("approved", "relocate")
    decided = next(item for item in items if item["kind"] == "approval_decided")
    assert (decided["actor"], decided["title"]) == ("host", "Host approved · relocate")

    to_guest = [item for item in items if item.get("event") == "GuestMessageSent"]
    assert [item["actor"] for item in to_guest] == ["host", "host"]  # ambele texte aprobate de host
    internal = [item for item in items if item["minor"]]
    assert {"Estimate ready", "Staff replied"} <= {item["title"] for item in internal}

    # Graful de cauzalitate: fiecare eveniment, cu handlerii lui.
    assert len(story["graph"]) == story["events"]
    assert all(h["status"] == "ok" for event in story["graph"] for h in event["handlers"])


def test_a_chat_answer_links_the_conversation_to_the_incident(busy):
    incident = flow_of(busy, "incident")
    chat = flow_of(busy, "conversation", "let's move")

    incident_story = busy.get(f"/agent/flows/{incident['correlation_id']}").json()
    chat_story = busy.get(f"/agent/flows/{chat['correlation_id']}").json()

    (cause,) = incident_story["linked"]
    assert (cause["correlation_id"], cause["relation"]) == (chat["correlation_id"], "caused_by")
    (effect,) = chat_story["linked"]
    assert (effect["correlation_id"], effect["relation"]) == (incident["correlation_id"], "led_to")
    answered = next(i for i in chat_story["items"] if i.get("event") == "GuestOfferAnswered")
    assert answered["actor"] == "guest" and answered["title"].endswith("answered the offer: accept")


class PromisingLLM:
    """Model care încearcă întâi o promisiune neaprobată, apoi se corectează."""

    name = "promising"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        done = [c.name for m in request.messages if m.role == "assistant" for c in m.tool_calls]
        if "get_booking_context" not in done:
            call = ToolCall.make("get_booking_context", reason="Read the booking first.")
        elif done.count("send_guest_reply") == 0:
            call = ToolCall.make("send_guest_reply", text="Your dog can join you during the stay.",
                                 reason="Tell the guest the dog is fine.")  # fmt: skip
        elif done.count("send_guest_reply") == 1:
            call = ToolCall.make("send_guest_reply", host_follow_up=True, reason="State the rule instead.",
                                 text="Pets need the host's decision. The host will follow up.")  # fmt: skip
        else:
            call = ToolCall.make("finish", result="Answered without a promise.", evidence=[], reason="Done.")
        return ChatResponse(message=ChatMessage(role="assistant", tool_calls=[call]), usage=Usage(), model=self.name)


def test_a_call_rejected_by_a_code_rule_is_shown_as_a_correction(settings):
    with app_client(settings, llm=PromisingLLM()) as client:
        emma = say(client, "Emma Johnson", "Can my dog come?")

        overview = client.get("/agent/overview").json()
        assert (overview["tool_calls"], overview["corrected_by_code"]) == (3, 1)
        (correction,) = [d for d in client.get("/agent/decisions").json() if d["actor"] == "code"]
        assert correction["what"] == "Corrected: Send guest reply"
        assert "a commitment requires the exact approved request" in correction["why"]
        assert correction["agent_intent"] == "Tell the guest the dog is fine."
        assert correction["guest_name"] == "Emma Johnson"
        # Promisiunea nu a ajuns la oaspete.
        sent = [m["body"] for m in client.get(f"/bookings/{emma['id']}/messages").json() if m["direction"] == "outbound"]
        assert sent == ["Pets need the host's decision. The host will follow up."]


def test_the_decision_log_mixes_agent_and_host_decisions_newest_first(busy):
    decisions = busy.get("/agent/decisions", params={"limit": 200}).json()

    assert [d["at"] for d in decisions] == sorted((d["at"] for d in decisions), reverse=True)
    assert {d["actor"] for d in decisions} == {"agent", "host"}
    host = next(d for d in decisions if d["actor"] == "host")
    assert host["what"] == "Approved: Incident at Calea Victoriei Apartment · relocate"
    assert all(d["why"] for d in decisions if d["actor"] == "agent")


# --- Pagina ---------------------------------------------------------------------------------


@pytest.fixture
def page_api(busy, monkeypatch):
    def request(method, path, *, params=None, json=None):
        query = {key: value for key, value in (params or {}).items() if value is not None}
        response = busy.request(method, path, params=query, json=json)
        if response.status_code >= 400:
            raise api_client.ApiError(f"{response.status_code}: {response.json().get('detail')}")
        return response.json()

    monkeypatch.setattr(api_client, "request", request)
    return busy


def run_page(path: Path) -> AppTest:
    page = AppTest.from_file(str(path), default_timeout=60).run()
    assert not page.exception, [e.value for e in page.exception]
    return page


def test_the_page_tells_the_story_of_the_selected_flow(page_api):
    incident = flow_of(page_api, "incident")
    page = run_page(PAGE)

    labels = [metric.label for metric in page.metric]
    assert labels[:4] == ["Flows handled", "Replies sent autonomously", "Messages approved by the host",
                          "Waiting for the host"]  # fmt: skip
    assert [metric.value for metric in page.metric[:4]] == ["4", "3", "2", "2"]

    page.selectbox(key="dashboard_flow").set_value(incident["correlation_id"]).run()
    assert not page.exception
    shown = "\n".join(str(block.value) for block in page.markdown)
    assert incident["title"] in [h.value for h in page.subheader]
    assert "**Maria Ionescu reported a problem at Calea Victoriei Apartment**" in shown
    assert "**Asked the host: Incident at Calea Victoriei Apartment**" in shown
    assert "**Host approved · relocate**" in shown
    assert "Estimate ready" not in shown  # pașii interni sunt ascunși implicit

    page.toggle(key="dashboard_internal").set_value(True).run()
    assert "**Estimate ready**" in "\n".join(str(block.value) for block in page.markdown)


def test_the_page_filters_flows_by_guest(page_api):
    page = run_page(PAGE)

    page.selectbox(key="dashboard_guest").set_value("Emma Johnson").run()

    assert not page.exception
    options = page.selectbox(key="dashboard_flow").options
    assert len(options) == 2 and all("Emma Johnson" in option for option in options)
