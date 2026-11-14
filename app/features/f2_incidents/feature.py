"""F2 — Incidente. Punctul de intrare descoperit automat."""

from __future__ import annotations

from app.core.contracts.events import GuestMessageReceived, IssueReported
from app.core.feature_context import FeatureContext
from app.features.f2_incidents.events import (
    EstimateReady,
    GuestAnsweredRelocation,
    IncidentTaskCompleted,
    IncidentTimeout,
    SimGuestAnswerDue,
    SimStaffReplyDue,
    SimTaskDue,
    StaffContactRequested,
    StaffReplied,
)
from app.features.f2_incidents.mock_llm import POLICIES
from app.features.f2_incidents.repo import ensure_columns
from app.features.f2_incidents.router import build_router
from app.features.f2_incidents.service import APPROVAL_KIND, CLOSURE_KIND, IncidentService
from app.features.f2_incidents.settings import F2Settings


def register(ctx: FeatureContext) -> None:
    ensure_columns(ctx.db)  # o bază locală veche primește coloanele noi, fără reseed
    service = IncidentService(ctx, ctx.load_settings(F2Settings))

    # `IssueReported` are un singur abonat F2, cu numele `on_issue_reported` (îl verifică testul F3).
    ctx.subscribe(IssueReported, service.on_issue_reported)
    ctx.subscribe(StaffContactRequested, service.on_staff_contact)
    ctx.subscribe(SimStaffReplyDue, service.on_sim_staff_reply)
    ctx.subscribe(StaffReplied, service.on_staff_replied)
    ctx.subscribe(EstimateReady, service.on_estimate_ready)
    ctx.subscribe(SimGuestAnswerDue, service.on_sim_guest_answer)
    ctx.subscribe(GuestAnsweredRelocation, service.on_guest_answer)
    ctx.subscribe(SimTaskDue, service.on_sim_task)
    ctx.subscribe(IncidentTaskCompleted, service.on_task_completed)
    ctx.subscribe(IncidentTimeout, service.on_timeout)
    # Conversația cu oaspetele aparține copilotului; F2 află doar răspunsul la oferta lui
    # și faptul că oaspetele e activ (termenul ofertei se prelungește).
    ctx.on_guest_offer_answered(service.on_guest_offer_answered)
    ctx.subscribe(GuestMessageReceived, service.on_guest_message)
    ctx.on_decision(APPROVAL_KIND, service.on_decision)
    ctx.on_decision(CLOSURE_KIND, service.on_closure_decision)

    for purpose, policy in POLICIES.items():
        ctx.register_mock_llm_policy(purpose, policy)
    ctx.include_router(build_router(service))
    ctx.on_quiesce(service.stop_background)
