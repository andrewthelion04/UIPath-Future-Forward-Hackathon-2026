"""Timeline-ul agentului: fiecare pas cu motiv, unealtă, argumente, rezultat, tokeni și cost."""

from __future__ import annotations

import streamlit as st

from ui.components import load, multiline, short_time, status_badge

st.title("Agent timeline")
st.caption("What the agent did, in order, and why. The model picks the tools; our code runs them.")

runs = load("/agent/runs", limit=100)
if not runs:
    st.info("No agent run yet. Send a guest message or request a review analysis from the Simulator.")
    st.stop()

features = sorted({run["feature"] for run in runs})
selected_feature = st.segmented_control("Feature", features, key="timeline_feature")
if selected_feature:
    runs = [run for run in runs if run["feature"] == selected_feature]

run_id = st.selectbox(
    "Run",
    [run["id"] for run in runs],
    format_func=lambda rid: next(
        f"{short_time(r['started_at'])} · {r['agent']} · {r['status']} · {r['steps']} steps"
        for r in runs
        if r["id"] == rid
    ),
)
run = load(f"/agent/runs/{run_id}")

status_badge(run["status"])
steps_metric, tokens_metric, cost_metric, model_metric = st.columns(4)
steps_metric.metric("Steps", run["steps"])
tokens_metric.metric("Tokens in / out", f"{run['prompt_tokens']} / {run['completion_tokens']}")
cost_metric.metric("Cost (credits)", f"{run['cost']:.4f}")
model_metric.metric("Model", run["model"] or "n/a")

st.markdown("**Objective**")
st.code(run["objective"], language=None, wrap_lines=True)
if run["result"]:
    st.markdown("**Result**")
    st.markdown(multiline(run["result"]))
if run["evidence"]:
    st.markdown("**Evidence**")
    for item in run["evidence"]:
        st.markdown(f"- {item}")
if run["error"]:
    st.error(run["error"])

st.divider()
st.subheader("Steps")
for step in run["steps_detail"]:
    kind = step["kind"]
    if kind == "llm":
        calls = [call["name"] for call in step["arguments"] or []]
        chose = f"chose {', '.join(calls)}" if calls else "answered with text"
        with st.expander(f"{step['idx']}. Model call · {chose}", icon=":material/psychology:"):
            st.caption(
                f"{step['model']} · {step['prompt_tokens']} tokens in, {step['completion_tokens']} out · "
                f"cost {step['cost']:.4f} · {step['duration_ms']} ms"
            )
            if step["content"]:
                st.markdown(step["content"])
            st.markdown("Messages sent to the model")
            st.json(step["messages"], expanded=False)
    elif kind in ("tool", "finish"):
        icon = ":material/flag:" if kind == "finish" else ":material/build:"
        failed = " · returned an error" if step["is_error"] else ""
        title = f"{step['idx']}. {step['tool_name']}{failed} · {step['reason'] or 'no reason given'}"
        with st.expander(title, icon=icon, expanded=step["is_error"]):
            arguments_column, result_column = st.columns(2)
            arguments_column.markdown("Arguments")
            arguments_column.json(step["arguments"])
            result_column.markdown("Result")
            result_column.json(step["result"])
            st.caption(f"{step['duration_ms']} ms")
    else:  # nudge | error
        with st.expander(f"{step['idx']}. {kind}", icon=":material/report:", expanded=kind == "error"):
            st.markdown(step["content"] or "")
