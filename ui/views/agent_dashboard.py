"""Dashboardul agentului: ce a făcut singur, ce a cerut hostului și de ce, flux cu flux.

Totul vine din `GET /agent/overview`, `/agent/flows`, `/agent/flows/{id}` și `/agent/decisions`,
adică din jurnalele reale ale rulării (evenimente, pașii agenților cu motivul lor, aprobări).
Pagina nu calculează și nu inventează nimic: doar așază povestea.
"""

from __future__ import annotations

import streamlit as st

from ui.components import load, multiline, short_time

ACTORS = {
    "guest": ("Guest", ":material/person:", "blue"),
    "staff": ("Staff", ":material/engineering:", "blue"),
    "agent": ("Agent", ":material/smart_toy:", "violet"),
    "code": ("Code rule", ":material/rule:", "gray"),
    "host": ("Host", ":material/verified_user:", "green"),
}
FLOW_STATUS = {
    "done": ("Done", ":material/check_circle:", "green"),
    "waiting_for_host": ("Waiting for the host", ":material/pending_actions:", "orange"),
    "running": ("Running", ":material/sync:", "blue"),
    "needs_attention": ("Needs attention", ":material/error:", "red"),
}
KIND_ICONS = {
    "incident": ":material/build:",
    "conversation": ":material/chat:",
    "cancellation": ":material/event_busy:",
    "reviews": ":material/reviews:",
    "other": ":material/bolt:",
}


def actor_badge(actor: str) -> None:
    label, icon, color = ACTORS.get(actor, (actor.title(), ":material/bolt:", "gray"))
    st.badge(label, icon=icon, color=color)


def money(value: float) -> str:
    return f"${value:,.4f}" if value and value < 1 else f"${value:,.2f}"


def seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} s" if value < 90 else f"{value / 60:.1f} min"


st.title("Agent dashboard")
st.caption(
    "What the agent did on its own, what it asked the host to decide, and the reason behind "
    "every step. Rebuilt from the run journals: events, agent steps, approvals and messages."
)
live = st.sidebar.toggle("Live refresh", value=False, help="Reload the numbers every 5 seconds.")


# --- Indicatori ---------------------------------------------------------------------------


@st.fragment(run_every=5 if live else None)
def headline_numbers() -> None:
    o = load("/agent/overview")
    if not o["flows"]:
        st.info("Nothing has run yet. Trigger a flow from the Simulator, then come back.")
        return
    approvals = o["approvals"]
    first = st.columns(4)
    first[0].metric("Flows handled", o["flows"], help="A trigger and everything that followed it.")
    first[1].metric(
        "Replies sent autonomously", o["replies_autonomous"],
        help="Informational answers to guests. They need no approval because they promise nothing.",
    )  # fmt: skip
    first[2].metric(
        "Messages approved by the host", o["messages_host_approved"],
        help="Offers, confirmations and apologies: the host approved the exact text first.",
    )  # fmt: skip
    first[3].metric(
        "Waiting for the host", approvals["pending"],
        help=f"Decided so far: {approvals['approved']} approved, {approvals['rejected']} rejected, "
        f"{approvals['withdrawn']} withdrawn because they were no longer true.",
    )  # fmt: skip
    second = st.columns(4)
    second[0].metric(
        "Agent decisions", o["tool_calls"],
        help=f"Tool calls chosen by the model across {o['agent_runs']} agent runs, each with a stated reason.",
    )  # fmt: skip
    second[1].metric(
        "Corrected by code rules", o["corrected_by_code"],
        help="Tool calls the code rejected (a promise without approval, an invented quote, a broken "
        "limit). The error goes back to the model, which has to change course.",
    )  # fmt: skip
    second[2].metric(
        "Median reply time", seconds(o["median_reply_s"]),
        help="From a guest's message to the first reply on that booking.",
    )  # fmt: skip
    second[3].metric(
        "LLM cost", money(o["cost"]),
        help=f"{o['tokens_in']:,} tokens in, {o['tokens_out']:,} out. Model: {', '.join(o['models']) or 'n/a'}.",
    )  # fmt: skip
    if o["handler_problems"]:
        st.warning(f"{o['handler_problems']} handler run(s) failed or timed out. See Events for details.")


headline_numbers()

flows = load("/agent/flows", limit=80)
if not flows:
    st.stop()

story_tab, log_tab = st.tabs(["Flow story", "Decision log"])


# --- Povestea unui flux ---------------------------------------------------------------------


def flow_label(flow: dict) -> str:
    status = FLOW_STATUS.get(flow["status"], (flow["status"],))[0]
    return f"{short_time(flow['started_at'])} · {flow['title']} · {status}"


def render_agent_run(item: dict) -> None:
    steps = item["steps"]
    corrected = sum(1 for step in steps if step["is_error"])
    facts = [f"{len(steps)} decision(s)"]
    if corrected:
        facts.append(f"{corrected} corrected by code")
    if item.get("duration_s") is not None:
        facts.append(seconds(item["duration_s"]))
    if item.get("cost"):
        facts.append(money(item["cost"]))
    st.caption(" · ".join(facts) + (f" · {item['model']}" if item.get("model") else ""))
    for step in steps:
        if step["is_error"]:
            st.markdown(f":material/rule: **Corrected by code: {step['label']}**")
            st.markdown(f"The agent wanted to: {step['reason'] or 'no reason given'}")
            st.error(multiline(step["result"] or "Rejected by a code rule."), icon=":material/block:")
        else:
            icon = ":material/flag:" if step["tool"] == "finish" else ":material/arrow_right_alt:"
            st.markdown(f"{icon} **{step['label']}** · {step['reason'] or 'no reason given'}")
    if item.get("detail"):
        st.markdown(f"**Outcome.** {item['detail']}")
    with st.expander("Arguments and results"):
        for step in steps:
            st.markdown(f"**{step['label']}**")
            if step["arguments"]:
                st.code(step["arguments"], language=None, wrap_lines=True)
            if step["result"]:
                st.caption(step["result"])


def render_item(item: dict) -> None:
    with st.container(border=True):
        header, when = st.columns([5, 1])
        with header:
            actor_badge(item["actor"])
            st.markdown(f"**{item['title']}**")
        when.caption(short_time(item["at"]))
        kind = item["kind"]
        if kind == "agent_run":
            render_agent_run(item)
        elif kind == "approval_requested":
            st.markdown(multiline(item.get("detail") or ""))
            for option in item.get("options", []):
                chosen = " · chosen by the host" if option["key"] == item.get("chosen_option") else ""
                st.markdown(f"**Option: {option['label']}**{chosen}")
                if option["description"]:
                    st.caption(multiline(option["description"]))
            if item.get("status") == "pending":
                st.badge("Waiting for the host", icon=":material/pending_actions:", color="orange")
        elif kind == "problem":
            st.error(item.get("detail") or "The handler did not finish.", icon=":material/error:")
        elif item.get("detail"):
            st.markdown(multiline(item["detail"]))


def flow_map(graph: list[dict]) -> str:
    """Graful evenimentelor fluxului: fiecare săgeată e „a dus la”, din lanțul de cauzalitate."""
    try:
        dark = st.context.theme.type == "dark"
    except Exception:  # tema nu e cunoscută în afara unui browser
        dark = False
    ink, line = ("#fafafa", "#9aa0a6") if dark else ("#262730", "#6b7280")
    known = {event["id"] for event in graph}
    lines = [
        "digraph flow {",
        '  rankdir=LR; bgcolor="transparent";',
        f'  node [shape=box, style="rounded", fontname="Helvetica", fontsize=10, color="{line}", fontcolor="{ink}"];',
        f'  edge [color="{line}", arrowsize=0.7];',
    ]
    for event in graph:
        label = "".join(" " + c if c.isupper() and i else c for i, c in enumerate(event["type"]))
        handlers = [h for h in event["handlers"] if h["status"] not in ("ok", "running")]
        if handlers:
            label += "\\n(handler " + ", ".join(h["status"] for h in handlers) + ")"
        width = 2 if event["causation_id"] not in known else 1
        lines.append(f'  "{event["id"]}" [label="{label}", penwidth={width}];')
        if event["causation_id"] in known:
            lines.append(f'  "{event["causation_id"]}" -> "{event["id"]}";')
    return "\n".join([*lines, "}"])


with story_tab:
    guests = sorted({flow["guest_name"] for flow in flows if flow["guest_name"]})
    picker, filter_column = st.columns([3, 1])
    guest = filter_column.selectbox("Guest", ["All guests", *guests], key="dashboard_guest")
    shown = [flow for flow in flows if guest == "All guests" or flow["guest_name"] == guest]
    selected = picker.selectbox(
        "Flow", [flow["correlation_id"] for flow in shown],
        format_func=lambda flow_id: flow_label(next(f for f in shown if f["correlation_id"] == flow_id)),
        key="dashboard_flow",
    )  # fmt: skip
    story = load(f"/agent/flows/{selected}") if selected else None

    if story:
        label, icon, color = FLOW_STATUS.get(story["status"], (story["status"], ":material/bolt:", "gray"))
        st.subheader(story["title"])
        st.badge(label, icon=icon, color=color)
        numbers = st.columns(5)
        numbers[0].metric("Agent decisions", story["tool_calls"])
        numbers[1].metric("Corrected by code", story["corrected_by_code"])
        numbers[2].metric("Host approvals", story["approvals"])
        numbers[3].metric("Messages to the guest", story["guest_messages_sent"])
        numbers[4].metric("Duration", seconds(story["duration_s"]), help=f"LLM cost {money(story['cost'])}")

        for link in story["linked"]:
            relation = "Continued from" if link["relation"] == "caused_by" else "Led to"
            st.caption(f":material/link: {relation} another flow ({link['via']}): {link['title']}")

        internal = st.toggle(
            "Show internal steps", value=False, key="dashboard_internal",
            help="Events a feature sends to itself and routine notifications.",
        )  # fmt: skip
        for item in story["items"]:
            if internal or not item["minor"]:
                render_item(item)

        with st.expander("Flow map: how one event led to the next"):
            st.caption(
                "Features talk only through events. A thick border marks where this flow started "
                "or continued from another flow."
            )
            st.graphviz_chart(flow_map(story["graph"]))


# --- Jurnalul deciziilor ----------------------------------------------------------------------

with log_tab:
    st.caption("The latest decisions across all flows, newest first, each with the reason recorded when it was made.")
    who = st.segmented_control(
        "Decided by", ["Agent", "Code rule", "Host"], selection_mode="multi", key="dashboard_actors"
    )
    wanted = {name for name, (label, _, _) in ACTORS.items() if label in (who or [])}
    for decision in load("/agent/decisions", limit=60):
        if wanted and decision["actor"] not in wanted:
            continue
        with st.container(border=True):
            left, right = st.columns([5, 1])
            with left:
                actor_badge(decision["actor"])
                st.markdown(f"**{decision['what']}**")
                if decision["why"]:
                    st.markdown(f"Why: {decision['why']}")
                if decision["agent_intent"]:
                    st.caption(f"The agent wanted to: {decision['agent_intent']}")
            about = " · ".join(x for x in (decision["guest_name"], decision["property_name"]) if x)
            right.caption(short_time(decision["at"]) + (f"  \n{about}" if about else ""))
