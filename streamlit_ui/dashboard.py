"""Network Resilience Command Center dashboard.

Presentation and operator-interaction layer only: risk, recommendation, governance, approval,
execution, and verification states are read from the Command Center Backend API.
"""

from __future__ import annotations

import copy
import html
import math
import time
from datetime import datetime
from io import BytesIO
from typing import Any

import pandas as pd
import pydeck as pdk
import streamlit as st

try:
    from streamlit_ui.api_client import (
        REGION_OPTIONS,
        SCENARIO_OPTIONS,
        SEVERITY_OPTIONS,
        WEATHER_EVENT_OPTIONS,
        RunApiClient,
        RunApiError,
        apply_sse_event_to_state,
        build_run_request,
        build_workflow_stage_bar,
        calculate_workflow_progress,
        empty_authoritative_state,
        format_activity_log_row,
        format_bedrock_display_model,
        format_evidence_bundle_summary,
        format_safe_api_error,
        is_approval_actionable,
        load_ui_config,
        normalize_api_run,
        should_accept_sse_event,
    )
except ModuleNotFoundError:
    from api_client import (  # type: ignore[no-redef]
        REGION_OPTIONS,
        SCENARIO_OPTIONS,
        SEVERITY_OPTIONS,
        WEATHER_EVENT_OPTIONS,
        RunApiClient,
        RunApiError,
        apply_sse_event_to_state,
        build_run_request,
        build_workflow_stage_bar,
        calculate_workflow_progress,
        empty_authoritative_state,
        format_activity_log_row,
        format_bedrock_display_model,
        format_evidence_bundle_summary,
        format_safe_api_error,
        is_approval_actionable,
        load_ui_config,
        normalize_api_run,
        should_accept_sse_event,
    )


st.set_page_config(
    page_title="NRC² · Network Resilience",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root {
      --bg:#0a0e17; --surface:#111827; --surface2:#1a2234; --border:#26344d;
      --text:#e8edf7; --muted:#8b9bb8; --accent:#00b4d8; --accent2:#0077b6;
      --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444;
    }
    .stApp { background:radial-gradient(1100px 500px at 8% -10%,rgba(0,180,216,.12),transparent),var(--bg); color:var(--text); font-family:"Segoe UI",Arial,sans-serif; }
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display:none; }
    [data-testid="stToolbar"], [data-testid="stDecoration"], #MainMenu, footer { visibility:hidden; height:0; }
    .block-container { max-width:1680px; padding:1.6rem 2rem 2.5rem; }
    .mono { font-family:"Cascadia Code",Consolas,monospace; }
    .eyebrow { color:var(--muted); font-size:.7rem; font-weight:700; letter-spacing:.14em; text-transform:uppercase; }
    .brand { display:flex; align-items:center; gap:14px; border-bottom:1px solid var(--border); padding-bottom:1.1rem; margin-bottom:1.2rem; }
    .logo { width:52px; height:52px; border-radius:14px; display:grid; place-items:center; background:linear-gradient(135deg,#0b3d91,#0077b6); box-shadow:0 8px 24px rgba(0,119,182,.3); font-size:1.65rem; }
    .brand-title { font-size:1.45rem; font-weight:700; letter-spacing:-.02em; }
    .brand-title span { color:var(--accent); margin-left:5px; }
    .subtle { color:var(--muted); font-size:.85rem; }
    .live { margin-left:auto; padding:7px 12px; border:1px solid var(--border); border-radius:999px; color:var(--muted); font-size:.76rem; }
    .live-dot { display:inline-block; width:8px; height:8px; background:var(--ok); border-radius:50%; margin-right:6px; box-shadow:0 0 0 4px rgba(34,197,94,.12); }
    .live.down { border-color:rgba(239,68,68,.55); color:var(--bad); }
    .live.down .live-dot { background:var(--bad); box-shadow:0 0 0 4px rgba(239,68,68,.15); }
    .metric { background:linear-gradient(145deg,var(--surface),#0f192b); border:1px solid var(--border); border-radius:14px; padding:1rem 1.1rem; box-shadow:0 12px 35px rgba(0,0,0,.16); min-height:102px; }
    .metric-label { color:var(--muted); font-size:.72rem; font-weight:600; letter-spacing:.08em; text-transform:uppercase; }
    .metric-value { font-family:"Cascadia Code",Consolas,monospace; font-size:1.3rem; font-weight:500; margin-top:.55rem; word-break:break-all; }
    .metadata { display:flex; flex-wrap:wrap; gap:1.25rem; margin:.65rem 0 1rem; color:var(--muted); font-size:.74rem; }
    .metadata b { color:var(--text); font-family:"Cascadia Code",Consolas,monospace; font-weight:500; }
    .cyan { color:var(--accent)!important; } .ok { color:var(--ok)!important; } .warn { color:var(--warn)!important; } .bad { color:var(--bad)!important; } .muted { color:var(--muted)!important; }
    .section-head { display:flex; align-items:center; justify-content:space-between; margin:1.5rem 0 .75rem; }
    .section-head h2 { font-size:1.05rem; margin:0; } .section-head span { color:var(--muted); font-size:.78rem; }
    .panel { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:1.05rem; box-shadow:0 12px 35px rgba(0,0,0,.12); height:100%; }
    .panel-title { display:flex; justify-content:space-between; color:var(--text); font-weight:700; border-bottom:1px solid var(--border); padding-bottom:.8rem; margin-bottom:.65rem; }
    .panel-title small { color:var(--accent); font-family:"Cascadia Code",Consolas,monospace; font-size:.66rem; letter-spacing:.1em; text-transform:uppercase; }
    .row { display:flex; justify-content:space-between; gap:1rem; padding:.6rem 0; border-bottom:1px solid rgba(38,52,77,.65); font-size:.86rem; }
    .row:last-child { border-bottom:0; } .label { color:var(--muted); } .value { font-family:"Cascadia Code",Consolas,monospace; text-align:right; }
    .badge { display:inline-block; border-radius:999px; padding:4px 9px; font-size:.68rem; font-weight:700; letter-spacing:.04em; }
    .badge-ok { color:var(--ok); background:rgba(34,197,94,.12); } .badge-warn { color:var(--warn); background:rgba(245,158,11,.12); } .badge-bad { color:var(--bad); background:rgba(239,68,68,.12); } .badge-info { color:var(--accent); background:rgba(0,180,216,.12); } .badge-muted { color:var(--muted); background:rgba(139,155,184,.12); }
    .workflow { display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin:.6rem 0 1.2rem; }
    .workflow-step { position:relative; background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:.8rem .65rem; text-align:center; box-shadow:0 8px 20px rgba(0,0,0,.12); }
    .workflow-step:not(:last-child):after { content:"➜"; position:absolute; right:-17px; top:50%; transform:translateY(-50%); z-index:2; color:var(--accent); font-size:1rem; font-weight:700; }
    .workflow-step.s-completed { border-color:rgba(34,197,94,.45); } .workflow-step.s-in_progress { border-color:var(--accent); }
    .workflow-step.s-paused { border-color:rgba(245,158,11,.6); } .workflow-step.s-blocked, .workflow-step.s-failed { border-color:rgba(239,68,68,.6); }
    .workflow-icon { display:grid; place-items:center; width:42px; height:42px; margin:0 auto .5rem; border-radius:11px; color:var(--accent); background:rgba(0,180,216,.12); font-size:1.35rem; }
    .workflow-name { color:var(--text); font-size:.73rem; font-weight:700; line-height:1.2; }
    .workflow-status { color:var(--muted); font-family:"Cascadia Code",Consolas,monospace; font-size:.58rem; letter-spacing:.07em; margin-top:.35rem; text-transform:uppercase; }
    .s-completed .workflow-status { color:var(--ok); } .s-in_progress .workflow-status { color:var(--accent); } .s-paused .workflow-status, .s-partial_unresolved .workflow-status { color:var(--warn); }
    .s-blocked .workflow-status, .s-failed .workflow-status { color:var(--bad); }
    .control-box { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:.8rem 1rem .25rem; margin-bottom:1rem; }
    .control-label { color:var(--muted); font-family:"Cascadia Code",Consolas,monospace; font-size:.67rem; letter-spacing:.1em; text-transform:uppercase; margin-bottom:.45rem; }
    .theme-label { color:var(--muted); font-size:.73rem; margin-bottom:.2rem; }
    .map-panel { background:var(--surface); border:1px solid var(--border); border-radius:14px; overflow:hidden; box-shadow:0 12px 35px rgba(0,0,0,.12); }
    .map-caption { padding:.75rem 1rem; color:var(--muted); font-size:.76rem; border-top:1px solid var(--border); }
    .control-spacer { height:29px; }
    .governance-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:.65rem 1rem .25rem; margin:.35rem 0 1rem; }
    .panel-title > span:first-child { font-size:1rem; }
    .pulse-dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; background:var(--bad); box-shadow:0 0 0 0 rgba(239,68,68,.65); animation:pulse-risk 1.5s infinite; }
    .pulse-dot.warn { background:var(--warn); box-shadow:0 0 0 0 rgba(245,158,11,.55); animation:pulse-warn 1.7s infinite; }
    @keyframes pulse-risk { 0% { box-shadow:0 0 0 0 rgba(239,68,68,.65); } 70% { box-shadow:0 0 0 10px rgba(239,68,68,0); } 100% { box-shadow:0 0 0 0 rgba(239,68,68,0); } }
    @keyframes pulse-warn { 0% { box-shadow:0 0 0 0 rgba(245,158,11,.55); } 70% { box-shadow:0 0 0 10px rgba(245,158,11,0); } 100% { box-shadow:0 0 0 0 rgba(245,158,11,0); } }
    [data-baseweb="select"] > div { background:var(--surface2)!important; border-color:var(--border)!important; color:var(--text)!important; min-height:38px; }
    [data-baseweb="select"] input, [data-baseweb="select"] span { color:var(--text)!important; }
    [data-baseweb="popover"], [data-baseweb="menu"] { background:var(--surface)!important; color:var(--text)!important; }
    [role="option"] { background:var(--surface)!important; color:var(--text)!important; }
    [role="option"]:hover { background:var(--surface2)!important; color:var(--accent)!important; }
    div.st-key-run_scenario button { background:linear-gradient(135deg,#0369a1,#00a6c7); border-color:#00b4d8; color:#fff; }
    div.st-key-approve_action button { background:#16803c; border-color:#22c55e; color:#fff; }
    div.st-key-reject_action button { background:#b4232f; border-color:#ef4444; color:#fff; }
    div.st-key-approve_action button:disabled, div.st-key-reject_action button:disabled, div.st-key-run_scenario button:disabled { background:var(--surface2); border-color:var(--border); color:var(--muted); }
    [data-testid="stRadio"] > div { gap:4px; justify-content:flex-end; }
    [data-testid="stRadio"] label { background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:5px 10px; }
    .trace { background:#0b1220; border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:10px; padding:1rem 1.1rem; color:#b6c5d7; font-family:"Cascadia Code",Consolas,monospace; font-size:.76rem; line-height:1.9; }
    .stButton button { border:1px solid var(--border); background:var(--surface2); color:var(--text); border-radius:9px; font-weight:600; }
    .stButton button:hover { border-color:var(--accent); color:var(--accent); }
    .stButton button:focus { box-shadow:0 0 0 2px rgba(0,180,216,.35); }
    div.st-key-run_scenario button:hover { background:linear-gradient(135deg,#075985,#0891b2); border-color:#67e8f9; color:#fff; }
    div.st-key-approve_action button:hover { background:#1f9d4c; border-color:#86efac; color:#fff; }
    div.st-key-reject_action button:hover { background:#d93645; border-color:#fca5a5; color:#fff; }
    div.st-key-run_scenario button:focus, div.st-key-approve_action button:focus, div.st-key-reject_action button:focus { color:#fff; }
    div.st-key-excel_report button, div.st-key-history_excel button, div.st-key-view_evidence button { background:linear-gradient(135deg,#0369a1,#00a6c7); border-color:#00b4d8; color:#fff; }
    div.st-key-excel_report button:hover, div.st-key-history_excel button:hover, div.st-key-view_evidence button:hover { background:linear-gradient(135deg,#075985,#0891b2); border-color:#67e8f9; color:#fff; }
    .report-action { padding-top:1.55rem; }
    div.st-key-rejection_reason input { border-color:var(--border); }
    div.st-key-rejection_reason.required-error input { border-color:#ef4444!important; box-shadow:0 0 0 2px rgba(239,68,68,.2); }
    [data-testid="stDataFrame"] { border:1px solid var(--border); border-radius:12px; overflow:hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

SESSION_DEFAULTS: dict[str, Any] = {
    "theme_choice": "Auto",
    "current_run_id": None,
    "current_trace_id": None,
    "workflow_status": "READY",
    "workflow_events": [],
    "workflow_event_ids": set(),
    "workflow_sequence": 0,
    "agent1_result": None,
    "agent2_result": None,
    "command_center_result": None,
    "evidence_bundle": None,
    "stream_active": False,
    "stream_complete": False,
    "stream_error": None,
    "selected_scenario": "pause",
    "approval_status": None,
    "execution_status": None,
    "verification_status": None,
    "last_event_id": None,
    "bedrock_fallback_used": None,
    "api_connected": None,
    "api_health": {},
    "api_error": None,
    "run_history": {},
    "execution_history": [],
    "reject_validation_error": False,
    "show_evidence": False,
    "pending_run_selection": None,
}

for _key, _default in SESSION_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = copy.deepcopy(_default)

selected_theme = st.session_state.theme_choice
light_theme = """
<style>
:root { --bg:#f4f7fb; --surface:#ffffff; --surface2:#f1f5f9; --border:#d0dae8;
  --text:#0f172a; --muted:#64748b; --accent:#0284c7; --accent2:#0369a1;
  --ok:#16a34a; --warn:#d97706; --bad:#dc2626; }
.stApp { background:radial-gradient(900px 420px at 8% -20%,rgba(11,61,145,.1),transparent),var(--bg); }
.metric { background:linear-gradient(145deg,#fff,#f7faff); box-shadow:0 10px 30px rgba(15,23,42,.08); }
.trace { background:#0f172a; color:#e2e8f0; }
</style>
"""
auto_theme = """
<style>
@media (prefers-color-scheme: light) {
  :root { --bg:#f4f7fb; --surface:#ffffff; --surface2:#f1f5f9; --border:#d0dae8;
    --text:#0f172a; --muted:#64748b; --accent:#0284c7; --accent2:#0369a1;
    --ok:#16a34a; --warn:#d97706; --bad:#dc2626; }
  .stApp { background:radial-gradient(900px 420px at 8% -20%,rgba(11,61,145,.1),transparent),var(--bg); }
  .metric { background:linear-gradient(145deg,#fff,#f7faff); box-shadow:0 10px 30px rgba(15,23,42,.08); }
}
</style>
"""
st.markdown(
    light_theme if selected_theme == "Light" else auto_theme if selected_theme == "Auto" else "",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_api_client(base_url: str) -> RunApiClient:
    return RunApiClient(base_url)


ui_config = load_ui_config()
api_client = get_api_client(ui_config.api_base_url)

OK_VALUES = {
    "ALLOW",
    "APPROVED",
    "NOT_REQUIRED",
    "EXECUTED",
    "PASSED",
    "COMPLETED",
    "VERIFIED",
    "MATCHED",
    "COMPLETE",
    "LOW",
}
WARN_VALUES = {
    "PAUSE",
    "PENDING",
    "NOT_STARTED",
    "PAUSED",
    "AWAITING_APPROVAL",
    "IN_PROGRESS",
    "RUNNING",
    "MEDIUM",
    "PARTIAL_UNRESOLVED",
}
BAD_VALUES = {"DENY", "REJECTED", "BLOCKED", "FAILED", "EXPIRED", "HIGH", "CRITICAL"}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def tone_for(value: Any) -> str:
    text = str(value or "").upper()
    if text in OK_VALUES:
        return "ok"
    if text in BAD_VALUES:
        return "bad"
    if text in WARN_VALUES:
        return "warn"
    return "muted" if text in {"", "—"} else "cyan"


def badge(value: Any, tone: str | None = None) -> str:
    resolved = tone or tone_for(value)
    css = {"ok": "badge-ok", "warn": "badge-warn", "bad": "badge-bad", "muted": "badge-muted"}.get(
        resolved, "badge-info"
    )
    return f'<span class="badge {css}">{esc(value)}</span>'


def panel(title: str, rows: list[tuple[str, str]], icon: str = "◆", tag: str = "backend") -> str:
    content = f'<div class="panel"><div class="panel-title"><span>{icon}&nbsp; {esc(title)}</span><small>{esc(tag)}</small></div>'
    content += "".join(
        f'<div class="row"><span class="label">{esc(key)}</span><span class="value">{value}</span></div>'
        for key, value in rows
    )
    return content + "</div>"


def metric_card(label: str, value: Any, tone: str) -> str:
    return f'<div class="metric"><div class="metric-label">{esc(label)}</div><div class="metric-value {tone}">{esc(value)}</div></div>'


def has_run(view: dict[str, Any]) -> bool:
    return view.get("run_id") not in {None, "", "—"}


def log_history(
    action: str,
    result: str,
    details: str,
    run_id: str | None,
    approval: str = "—",
    user: str = "System",
) -> None:
    st.session_state.execution_history.append(
        {
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Run ID": run_id or "—",
            "Action": action,
            "Result": result,
            "Details": details,
            "Approval": approval,
            "User": user,
        }
    )


def mark_backend_down(exc: RunApiError) -> None:
    if exc.retryable and exc.status_code is None:
        st.session_state.api_connected = False
        st.session_state.api_error = exc.safe_message


def check_backend() -> None:
    try:
        st.session_state.api_health = api_client.health_check()
        st.session_state.api_connected = True
        st.session_state.api_error = None
    except RunApiError as exc:
        st.session_state.api_health = {}
        st.session_state.api_connected = False
        st.session_state.api_error = exc.safe_message


def sync_run_state(view: dict[str, Any], evidence_bundle: dict[str, Any] | None) -> None:
    run_id = str(view["run_id"])
    view["updated_at"] = datetime.now().strftime("%H:%M:%S")
    st.session_state.current_run_id = run_id
    st.session_state.current_trace_id = view.get("trace_id")
    st.session_state.workflow_status = view.get("status")
    st.session_state.agent1_result = view.get("agent1_result")
    st.session_state.agent2_result = view.get("agent2_result")
    st.session_state.approval_status = view.get("approval")
    st.session_state.execution_status = view.get("execution")
    st.session_state.verification_status = view.get("verification")
    st.session_state.bedrock_fallback_used = (view.get("ai_reasoning") or {}).get("fallback_used")
    st.session_state.command_center_result = view
    st.session_state.evidence_bundle = evidence_bundle
    st.session_state.run_history[run_id] = view
    st.session_state.pending_run_selection = run_id


def refresh_run(run_id: str) -> bool:
    """Load the authoritative run state and evidence bundle from the backend."""
    try:
        view = normalize_api_run(api_client.get_run(run_id))
    except RunApiError as exc:
        mark_backend_down(exc)
        st.session_state.stream_error = f"Unable to load run {run_id}: {exc.safe_message}"
        return False
    evidence_bundle: dict[str, Any] | None = None
    try:
        evidence_bundle = api_client.get_evidence(run_id, trace_id=view.get("trace_id"))
    except RunApiError as exc:
        st.session_state.stream_error = (
            f"Evidence bundle unavailable for {run_id}: {exc.safe_message}"
        )
    sync_run_state(view, evidence_bundle)
    return True


def accept_event(
    event: dict[str, Any], expected_run_id: str | None, expected_trace_id: str | None
) -> bool:
    accepted, sequence = should_accept_sse_event(
        event,
        seen_event_ids=st.session_state.workflow_event_ids,
        last_sequence=st.session_state.workflow_sequence,
        expected_run_id=expected_run_id,
        expected_trace_id=expected_trace_id,
    )
    if accepted:
        st.session_state.workflow_sequence = sequence
        st.session_state.workflow_events.append(event)
        if event.get("event_id"):
            st.session_state.last_event_id = str(event["event_id"])
    return accepted


def replay_post_decision_events(run_id: str) -> None:
    """Fetch SSE events appended after the approval decision using Last-Event-ID."""
    try:
        for event in api_client.replay_run_stream(
            run_id, last_event_id=st.session_state.last_event_id
        ):
            accept_event(event, run_id, st.session_state.current_trace_id)
    except RunApiError as exc:
        st.session_state.stream_error = f"Post-decision event replay failed: {exc.safe_message}"


def stage_bar_for(view: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, str]]:
    return build_workflow_stage_bar(
        workflow_stages=view.get("workflow_stages"),
        sse_events=events,
        governance_decision=view.get("policy_decision"),
        approval_status=view.get("approval"),
        execution_status=view.get("execution"),
        verification_status=view.get("verification"),
    )


def render_kpis(view: dict[str, Any]) -> str:
    overall = str(view.get("overall_risk") or "—").upper()
    status = str(view.get("status") or "—")
    cards = [
        metric_card("RUN ID", view.get("run_id"), "cyan"),
        metric_card("OVERALL RISK", overall, tone_for(overall)),
        metric_card("WORKFLOW STATUS", status, tone_for(status)),
        metric_card("LAST UPDATED", view.get("updated_at") or "—", "cyan"),
    ]
    return (
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;">'
        + "".join(cards)
        + "</div>"
    )


def render_metadata(view: dict[str, Any]) -> str:
    return (
        f'<div class="metadata"><span>TRACE <b>{esc(view.get("trace_id"))}</b></span>'
        f"<span>SCHEMA <b>{esc(view.get('schema_version'))}</b></span>"
        f"<span>TEAM <b>{esc(view.get('team_code'))}</b></span>"
        f"<span>SOURCE <b>{esc(view.get('data_source'))}</b></span>"
        f"<span>CREATED <b>{esc(view.get('created_at'))}</b></span></div>"
    )


def render_stage_bar(stage_bar: list[dict[str, str]]) -> str:
    content = '<div class="workflow">'
    for item in stage_bar:
        status = item["status"]
        content += (
            f'<div class="workflow-step s-{esc(status.lower())}"><div class="workflow-icon">{item["icon"]}</div>'
            f'<div class="workflow-name">{esc(item["name"])}</div><div class="workflow-status">{esc(status)}</div></div>'
        )
    return content + "</div>"


def render_activity(placeholder: Any, events: list[dict[str, Any]]) -> None:
    if not events:
        placeholder.caption(
            "No workflow events yet. Start a resilience analysis to stream backend events."
        )
        return
    rows = [format_activity_log_row(event) for event in reversed(events[-60:])]
    placeholder.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=260)


def render_progress(
    placeholder: Any, view: dict[str, Any], stage_bar: list[dict[str, str]]
) -> None:
    ratio, label = calculate_workflow_progress(
        stage_bar,
        workflow_status=view.get("status"),
        governance_decision=view.get("policy_decision"),
        approval_status=view.get("approval"),
    )
    placeholder.progress(max(0.0, min(1.0, ratio)), text=f"Workflow progress · {label}")


if st.session_state.api_connected is None:
    check_backend()

api_connected = bool(st.session_state.api_connected)
api_health = st.session_state.api_health or {}
if api_connected:
    connection_pill = (
        f'<div class="live"><span class="live-dot"></span> Backend Connected · '
        f"{esc(api_health.get('data_mode') or ui_config.data_mode)}</div>"
    )
else:
    connection_pill = (
        '<div class="live down"><span class="live-dot"></span> Backend Unavailable</div>'
    )

with st.container():
    st.markdown(
        '<div class="brand"><div class="logo">🛰️</div><div><div class="eyebrow">OPERATIONS</div>'
        '<div class="brand-title">Network Resilience Command Center <span>NRC²</span></div>'
        '<div class="subtle">Weather-to-action decision visibility · controlled operations environment</div></div>'
        f"{connection_pill}</div>",
        unsafe_allow_html=True,
    )
    st.radio(
        "Theme",
        ["Auto", "Dark", "Light"],
        horizontal=True,
        label_visibility="collapsed",
        key="theme_choice",
    )

if not api_connected:
    err_col, retry_col = st.columns([5, 1])
    with err_col:
        st.error(
            st.session_state.api_error
            or f"Command Center backend is unavailable at {ui_config.api_base_url}."
        )
    with retry_col:
        if st.button("↻  RETRY CONNECTION", use_container_width=True, key="retry_connection"):
            check_backend()
            st.rerun()

st.markdown(
    f'<div class="control-box"><div class="control-label">Command center controls</div>'
    f'<div class="subtle">Backend: {esc(ui_config.api_base_url)} · requested data mode: {esc(ui_config.data_mode)}</div></div>',
    unsafe_allow_html=True,
)

if st.session_state.pending_run_selection:
    st.session_state.run_id_selector = st.session_state.pending_run_selection
    st.session_state.pending_run_selection = None

SCENARIO_LABELS = {
    "pause": "PAUSE · human approval gate",
    "allow": "ALLOW · auto-execute",
    "deny": "DENY · governance block",
    "custom": "Custom inputs",
}
run_id_col, scenario_col, event_col, severity_col, region_col, run_col = st.columns(
    [1.2, 1.25, 1.15, 0.9, 1.15, 1.45]
)
with run_id_col:
    run_ids = list(st.session_state.run_history.keys())
    selected_run_id = st.selectbox(
        "Run ID",
        run_ids or ["No runs yet"],
        label_visibility="collapsed",
        key="run_id_selector",
        disabled=not run_ids,
    )
with scenario_col:
    scenario = st.selectbox(
        "Scenario",
        list(SCENARIO_OPTIONS),
        format_func=lambda value: SCENARIO_LABELS.get(value, value),
        label_visibility="collapsed",
        key="selected_scenario",
    )
custom_inputs = scenario == "custom"
with event_col:
    weather_event = st.selectbox(
        "Weather event",
        list(WEATHER_EVENT_OPTIONS),
        label_visibility="collapsed",
        disabled=not custom_inputs,
        key="weather_event_input",
    )
with severity_col:
    severity = st.selectbox(
        "Severity",
        list(SEVERITY_OPTIONS),
        index=2,
        label_visibility="collapsed",
        disabled=not custom_inputs,
        key="severity_input",
    )
with region_col:
    region = st.selectbox(
        "Affected region",
        list(REGION_OPTIONS),
        label_visibility="collapsed",
        disabled=not custom_inputs,
        key="region_input",
    )
with run_col:
    start_clicked = st.button(
        "▶  START RESILIENCE ANALYSIS",
        use_container_width=True,
        key="run_scenario",
        disabled=not api_connected or st.session_state.stream_active,
    )
if not custom_inputs:
    st.caption(
        "Scenario presets use backend-defined weather inputs. Choose “Custom inputs” to set event, severity, and region."
    )

if (
    run_ids
    and selected_run_id != st.session_state.current_run_id
    and selected_run_id in st.session_state.run_history
):
    with st.spinner(f"Loading run {selected_run_id}..."):
        st.session_state.workflow_events = []
        st.session_state.workflow_event_ids = set()
        st.session_state.workflow_sequence = 0
        st.session_state.last_event_id = None
        st.session_state.stream_error = None
        if refresh_run(selected_run_id):
            log_history(
                "Run selected", "VIEWED", "Loaded run from Run ID selector", selected_run_id
            )
    st.rerun()

view: dict[str, Any] = st.session_state.command_center_result or empty_authoritative_state()

with st.container(border=True):
    actionable = (
        api_connected
        and has_run(view)
        and is_approval_actionable(view.get("policy_decision"), view.get("approval"))
    )
    gate_left, gate_text, approve_col, reject_col = st.columns([1.25, 3.35, 1.2, 1.2])
    with gate_left:
        st.markdown('<div class="eyebrow">GOVERNANCE GATE</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="subtle" style="margin-top:.4rem;">Policy {badge(view.get("policy_decision", "—"))}<br>'
            f"Approval {badge(view.get('approval', '—'))}</div>",
            unsafe_allow_html=True,
        )
    with gate_text:
        if actionable:
            st.markdown(
                f'<div class="subtle">Run <b>{esc(view["run_id"])}</b> is paused awaiting an explicit human decision on a state-changing action.</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div class="subtle">Approve and Reject are enabled only when governance is PAUSE and approval is PENDING.</div>',
                unsafe_allow_html=True,
            )
        approval_user = st.text_input(
            "Approver reference",
            value="demo-approver",
            key="approval_user",
            disabled=not actionable,
        )
        rejection_reason = st.text_input(
            "Rejection reason",
            placeholder="Required when rejecting an action",
            key="rejection_reason",
            disabled=not actionable,
        )
        if st.session_state.reject_validation_error:
            st.markdown(
                "<style>div.st-key-rejection_reason input { border-color:#ef4444!important; box-shadow:0 0 0 2px rgba(239,68,68,.2); }</style>",
                unsafe_allow_html=True,
            )
    with approve_col:
        approve_action = st.button(
            "✓  APPROVE", use_container_width=True, key="approve_action", disabled=not actionable
        )
    with reject_col:
        reject_action = st.button(
            "✕  REJECT", use_container_width=True, key="reject_action", disabled=not actionable
        )


def submit_decision(decision: str, approver: str, comments: str) -> None:
    run_id = str(view["run_id"])
    approver_ref = approver.strip() or "demo-approver"
    try:
        with st.spinner(f"Submitting {decision.lower()}..."):
            if decision == "APPROVE":
                api_client.approve_run(run_id, approver_ref, comments)
            else:
                api_client.reject_run(run_id, approver_ref, comments)
    except RunApiError as exc:
        mark_backend_down(exc)
        st.error(f"{decision.title()} failed: {exc.safe_message}")
        return
    st.session_state.stream_error = None
    with st.spinner("Replaying post-decision workflow events..."):
        replay_post_decision_events(run_id)
        refresh_run(run_id)
    refreshed = st.session_state.command_center_result or {}
    log_history(
        "Preventive action approved" if decision == "APPROVE" else "Preventive action rejected",
        str(refreshed.get("execution", "—")),
        comments or "Approval confirmed by backend",
        run_id,
        approval=str(refreshed.get("approval", "—")),
        user=approver_ref,
    )
    st.rerun()


if approve_action:
    st.session_state.reject_validation_error = False
    submit_decision("APPROVE", approval_user, "")
if reject_action:
    reason = rejection_reason.strip()
    if not reason:
        st.session_state.reject_validation_error = True
        st.error("Rejection reason is required before rejecting this action.")
    else:
        st.session_state.reject_validation_error = False
        submit_decision("REJECT", approval_user, reason)

kpi_ph = st.empty()
meta_ph = st.empty()
error_ph = st.empty()
progress_ph = st.empty()
stages_ph = st.empty()
with st.expander("Live activity log", expanded=st.session_state.stream_active or not has_run(view)):
    activity_ph = st.empty()

current_events: list[dict[str, Any]] = st.session_state.workflow_events
current_stage_bar = stage_bar_for(view, current_events)
kpi_ph.markdown(render_kpis(view), unsafe_allow_html=True)
meta_ph.markdown(render_metadata(view), unsafe_allow_html=True)
if st.session_state.stream_error:
    error_ph.error(st.session_state.stream_error)
render_progress(progress_ph, view, current_stage_bar)
stages_ph.markdown(render_stage_bar(current_stage_bar), unsafe_allow_html=True)
render_activity(activity_ph, current_events)

MAX_STREAM_RECONNECTS = 3


def run_streaming_analysis(request: dict[str, Any]) -> None:
    st.session_state.workflow_events = []
    st.session_state.workflow_event_ids = set()
    st.session_state.workflow_sequence = 0
    st.session_state.last_event_id = None
    st.session_state.stream_active = True
    st.session_state.stream_complete = False
    st.session_state.stream_error = None
    st.session_state.show_evidence = False
    error_ph.empty()

    live = empty_authoritative_state()
    live["status"] = "CONNECTING"
    expected_run_id: str | None = None
    expected_trace_id: str | None = None
    terminal_event: dict[str, Any] | None = None
    reconnects = 0
    events_iter = api_client.stream_run(request)

    while True:
        try:
            for event in events_iter:
                if not accept_event(event, expected_run_id, expected_trace_id):
                    continue
                expected_run_id = expected_run_id or str(event.get("run_id") or "") or None
                expected_trace_id = expected_trace_id or str(event.get("trace_id") or "") or None
                live = apply_sse_event_to_state(live, event)
                live["updated_at"] = datetime.now().strftime("%H:%M:%S")
                events = st.session_state.workflow_events
                bar = stage_bar_for(live, events)
                kpi_ph.markdown(render_kpis(live), unsafe_allow_html=True)
                meta_ph.markdown(render_metadata(live), unsafe_allow_html=True)
                render_progress(progress_ph, live, bar)
                stages_ph.markdown(render_stage_bar(bar), unsafe_allow_html=True)
                render_activity(activity_ph, events)
                if event.get("terminal"):
                    terminal_event = event
                    break
        except RunApiError as exc:
            if not exc.retryable or not expected_run_id or reconnects >= MAX_STREAM_RECONNECTS:
                mark_backend_down(exc)
                st.session_state.stream_error = f"Workflow stream failed: {exc.safe_message}"
                break
        if terminal_event is not None:
            break
        if not expected_run_id or reconnects >= MAX_STREAM_RECONNECTS:
            st.session_state.stream_error = (
                st.session_state.stream_error
                or "Workflow stream ended before a terminal event was received."
            )
            break
        reconnects += 1
        error_ph.warning(
            f"SSE stream interrupted; reconnecting with Last-Event-ID (attempt {reconnects}/{MAX_STREAM_RECONNECTS})..."
        )
        time.sleep(ui_config.sse_reconnect_delay_seconds)
        events_iter = api_client.replay_run_stream(
            expected_run_id, last_event_id=st.session_state.last_event_id
        )

    st.session_state.stream_active = False
    st.session_state.stream_complete = terminal_event is not None
    request_label = (
        request.get("scenario")
        or f"{request.get('weather_event')} / {request.get('severity')} / {request.get('affected_region')}"
    )

    if terminal_event is not None and terminal_event.get("event_type") == "run_failed":
        failure = live.get("stream_error") or format_safe_api_error(terminal_event.get("message"))
        st.session_state.stream_error = f"Workflow failed: {failure}"
        live["updated_at"] = datetime.now().strftime("%H:%M:%S")
        st.session_state.command_center_result = live
        log_history(
            "Resilience analysis", "FAILED", f"{request_label} · {failure}", expected_run_id
        )
    elif expected_run_id and terminal_event is not None:
        with st.spinner("Loading authoritative run state and evidence..."):
            refresh_run(expected_run_id)
        final = st.session_state.command_center_result or live
        log_history(
            "Resilience analysis",
            str(final.get("status", "—")),
            f"{request_label} · policy={final.get('policy_decision', '—')}",
            expected_run_id,
            approval=str(final.get("approval", "—")),
        )
    st.rerun()


if start_clicked:
    run_streaming_analysis(
        build_run_request(
            scenario, weather_event=weather_event, severity=severity, affected_region=region
        )
    )

st.markdown(
    '<div class="section-head"><h2>01 / Signal and impact</h2><span>Agent 1 correlation output</span></div>',
    unsafe_allow_html=True,
)
signal_cards = [
    (
        "⛈️",
        "Weather alert",
        [
            ("Event", esc(view.get("event"))),
            ("Severity", badge(view.get("severity"))),
            ("Forecast", esc(view.get("forecast"))),
        ],
    ),
    (
        "◎",
        "Geo correlation",
        [
            ("Affected region", esc(view.get("region"))),
            ("Geo match", badge(view.get("geo_match_status"))),
            ("Assets mapped", badge(view.get("asset_mapping_status"))),
        ],
    ),
    (
        "◈",
        "Asset impact",
        [
            ("Affected network elements", esc(view.get("affected_ne_count", "—"))),
            (
                "Critical elements",
                f'<span class="bad">{esc(view.get("critical_ne_count", "—"))}</span>',
            ),
            ("Services at risk", esc(view.get("affected_service_count", "—"))),
        ],
    ),
]
for col, (icon, title, rows) in zip(st.columns(3), signal_cards, strict=True):
    col.markdown(panel(title, rows, icon), unsafe_allow_html=True)

st.markdown(
    '<div class="section-head"><h2>02 / Weather impact GIS</h2><span>Affected area from backend · asset positions are illustrative</span></div>',
    unsafe_allow_html=True,
)
REGION_DISPLAY_CENTERS = {
    "Region-A": (19.0760, 72.8777),
    "Region-B": (13.0827, 80.2707),
    "Region-C": (28.6139, 77.2090),
}
affected_area = view.get("affected_area") or {}
has_bbox = all(key in affected_area for key in ("south", "north", "west", "east"))
if has_bbox:
    center_lat = (float(affected_area["south"]) + float(affected_area["north"])) / 2
    center_lon = (float(affected_area["west"]) + float(affected_area["east"])) / 2
else:
    center_lat, center_lon = REGION_DISPLAY_CENTERS.get(str(view.get("region")), (20.5937, 78.9629))

backend_assets: list[dict[str, Any]] = view.get("affected_assets") or []
asset_records = []
for index, asset in enumerate(backend_assets):
    angle = 2 * math.pi * index / max(len(backend_assets), 1)
    asset_records.append(
        {
            "id": str(asset.get("ne_id") or f"NE-{index + 1}"),
            "lat": center_lat + 0.09 * math.sin(angle),
            "lon": center_lon + 0.11 * math.cos(angle),
            "risk": str(asset.get("risk") or "—").upper(),
        }
    )
asset_points = pd.DataFrame(asset_records, columns=["id", "lat", "lon", "risk"])
storm_center = (
    pd.DataFrame([{"lat": center_lat, "lon": center_lon}])
    if has_run(view)
    else pd.DataFrame(columns=["lat", "lon"])
)
critical_assets = asset_points[asset_points["risk"].isin(["HIGH", "CRITICAL"])]
medium_assets = asset_points[asset_points["risk"] == "MEDIUM"]
if has_bbox:
    zone_polygon = [
        [float(affected_area["west"]), float(affected_area["south"])],
        [float(affected_area["east"]), float(affected_area["south"])],
        [float(affected_area["east"]), float(affected_area["north"])],
        [float(affected_area["west"]), float(affected_area["north"])],
        [float(affected_area["west"]), float(affected_area["south"])],
    ]
    weather_zone = pd.DataFrame([{"polygon": zone_polygon}])
else:
    weather_zone = pd.DataFrame(columns=["polygon"])
network_routes = pd.DataFrame(
    [
        {"path": [[center_lon, center_lat], [row["lon"], row["lat"]]]}
        for _, row in asset_points.iterrows()
    ],
    columns=["path"],
)
map_layers = [
    pdk.Layer(
        "PolygonLayer",
        data=weather_zone,
        get_polygon="polygon",
        get_fill_color="[239, 68, 68, 24]",
        get_line_color="[239, 68, 68, 150]",
        stroked=True,
        filled=True,
        line_width_min_pixels=2,
    ),
    pdk.Layer(
        "PathLayer",
        data=network_routes,
        get_path="path",
        get_color="[0, 180, 216, 180]",
        get_width=3,
        width_min_pixels=2,
    ),
    pdk.Layer(
        "ScatterplotLayer",
        data=storm_center,
        get_position="[lon, lat]",
        get_radius=20000,
        get_fill_color="[239, 68, 68, 55]",
        get_line_color="[239, 68, 68, 190]",
        stroked=True,
        filled=True,
        line_width_min_pixels=2,
    ),
    pdk.Layer(
        "ScatterplotLayer",
        data=critical_assets,
        get_position="[lon, lat]",
        get_radius=1800,
        get_fill_color="[239, 68, 68, 230]",
        get_line_color="[255, 255, 255, 230]",
        stroked=True,
        filled=True,
        line_width_min_pixels=1,
        pickable=True,
    ),
    pdk.Layer(
        "ScatterplotLayer",
        data=medium_assets,
        get_position="[lon, lat]",
        get_radius=1800,
        get_fill_color="[245, 158, 11, 230]",
        get_line_color="[255, 255, 255, 230]",
        stroked=True,
        filled=True,
        line_width_min_pixels=1,
        pickable=True,
    ),
    pdk.Layer(
        "TextLayer",
        data=asset_points,
        get_position="[lon, lat]",
        get_text="id",
        get_size=12,
        get_color="[232, 237, 247, 255]",
        get_text_anchor="'middle'",
        get_alignment_baseline="'bottom'",
        get_pixel_offset="[0, -10]",
    ),
]
map_view = pdk.ViewState(
    latitude=center_lat, longitude=center_lon, zoom=9 if has_bbox else 4.2, pitch=0
)
map_col, map_legend = st.columns([2.6, 1])
with map_col:
    st.markdown('<div class="map-panel">', unsafe_allow_html=True)

    @st.fragment(run_every="2s")
    def render_live_weather_map() -> None:
        phase = (datetime.now().timestamp() % 2) / 2
        animated_weather = pdk.Layer(
            "ScatterplotLayer",
            data=storm_center,
            get_position="[lon, lat]",
            get_radius=int(20000 + phase * 8000),
            get_fill_color="[239, 68, 68, 38]",
            get_line_color="[239, 68, 68, 210]",
            stroked=True,
            filled=True,
            line_width_min_pixels=3,
        )
        node_radius = int(1800 + phase * 900)
        critical_pulse = pdk.Layer(
            "ScatterplotLayer",
            data=critical_assets,
            get_position="[lon, lat]",
            get_radius=node_radius,
            get_fill_color="[239, 68, 68, 36]",
            get_line_color="[239, 68, 68, 210]",
            stroked=True,
            filled=True,
            line_width_min_pixels=2,
        )
        medium_pulse = pdk.Layer(
            "ScatterplotLayer",
            data=medium_assets,
            get_position="[lon, lat]",
            get_radius=node_radius,
            get_fill_color="[245, 158, 11, 34]",
            get_line_color="[245, 158, 11, 210]",
            stroked=True,
            filled=True,
            line_width_min_pixels=2,
        )
        st.pydeck_chart(
            pdk.Deck(
                layers=[
                    map_layers[0],
                    map_layers[1],
                    animated_weather,
                    critical_pulse,
                    medium_pulse,
                    *map_layers[3:],
                ],
                initial_view_state=map_view,
                map_provider="carto",
                map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
                tooltip={"html": "<b>{id}</b><br/>Risk: {risk}", "style": {"color": "#e8edf7"}},
            ),
            use_container_width=True,
            height=430,
        )

    render_live_weather_map()
    map_caption = (
        f"Affected area for <b>{esc(view.get('region'))}</b> from Agent 1 bounding box"
        if has_bbox
        else "No affected area yet · start an analysis to load backend geospatial data"
    )
    st.markdown(f'<div class="map-caption">{map_caption}</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
with map_legend:
    bbox_text = (
        f"{affected_area['south']} to {affected_area['north']}°N · {affected_area['west']} to {affected_area['east']}°E"
        if has_bbox
        else "—"
    )
    st.markdown(
        panel(
            "Incident overlay",
            [
                ("Weather event", badge(view.get("event"), "bad" if has_run(view) else "muted")),
                ("Affected area", esc(bbox_text)),
                (
                    "High/critical risk assets",
                    f'<span class="bad"><span class="pulse-dot"></span>{len(critical_assets)}</span>',
                ),
                (
                    "Medium risk assets",
                    f'<span class="warn"><span class="pulse-dot warn"></span>{len(medium_assets)}</span>',
                ),
                ("Unresolved elements", esc(len(view.get("unresolved_network_elements") or []))),
            ],
            "⌖",
        ),
        unsafe_allow_html=True,
    )

risk_data: dict[str, Any] = view.get("risk_assessment") or {}
recommendation: dict[str, Any] = view.get("recommendation") or {}
governance: dict[str, Any] = view.get("governance") or {}
confidence = recommendation.get("confidence")
st.markdown(
    f'<div class="section-head"><h2>03 / Risk assessment</h2><span>Recommendation confidence · {esc(confidence if confidence is not None else "—")}</span></div>',
    unsafe_allow_html=True,
)
risk_col, assets_col = st.columns([1, 2.2])
with risk_col:
    score = risk_data.get("risk_score")
    st.markdown(
        panel(
            "Risk profile",
            [
                ("Risk score", esc(f"{score}/100" if score is not None else "—")),
                ("Overall risk", badge(view.get("overall_risk"))),
                ("Weather risk", badge(risk_data.get("weather_risk", "—"))),
                ("Network risk", badge(risk_data.get("network_risk", "—"))),
                ("Service risk", badge(risk_data.get("service_risk", "—"))),
            ],
            "⚠",
        ),
        unsafe_allow_html=True,
    )
    for reason in risk_data.get("risk_reasons") or []:
        st.caption(f"• {reason}")
with assets_col:
    st.markdown(
        '<div class="panel-title">Top affected assets <small>Agent 2 enrichment</small></div>',
        unsafe_allow_html=True,
    )
    if backend_assets:
        st.dataframe(
            [
                {
                    "NE ID": item.get("ne_id", "—"),
                    "Type": item.get("type", "—"),
                    "Region": item.get("region", "—"),
                    "Criticality": item.get("criticality", "—"),
                    "Risk": item.get("risk", "—"),
                    "Operational status": item.get("operational_status", "—"),
                    "Redundancy": item.get("redundancy_available", "—"),
                    "Services": ", ".join(item.get("services") or []),
                }
                for item in backend_assets
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("No asset impact data yet.")
    unresolved = view.get("unresolved_network_elements") or []
    if unresolved:
        st.warning(
            f"Unresolved network elements (not in inventory): {', '.join(map(str, unresolved))}"
        )

st.markdown(
    '<div class="section-head"><h2>04 / Governed preventive action</h2><span>Policy engine · human-in-the-loop</span></div>',
    unsafe_allow_html=True,
)
if recommendation:
    st.info(
        f"🔧  {recommendation.get('action', '—')} — {recommendation.get('expected_outcome', '—')}"
    )
else:
    st.caption("No recommendation yet.")
status_cards = [
    ("Policy decision", view.get("policy_decision", "—")),
    ("Human approval", view.get("approval", "—")),
    ("Execution", view.get("execution", "—")),
    ("Verification", view.get("verification", "—")),
]
for col, (label, value) in zip(st.columns(4), status_cards, strict=True):
    col.markdown(metric_card(label, value, tone_for(value)), unsafe_allow_html=True)
if governance.get("reason"):
    st.caption(f"Governance reason: {governance['reason']}")

bedrock = format_bedrock_display_model(view.get("ai_reasoning"))
st.markdown(
    '<div class="section-head"><h2>Bedrock reasoning</h2><span>Explanation only · does not change risk or governance</span></div>',
    unsafe_allow_html=True,
)
if not bedrock["available"]:
    st.caption(bedrock["banner_message"])
else:
    if bedrock["fallback_used"]:
        st.warning(
            f"⚠  {bedrock['banner_message']}"
            + (
                f" Reason: {format_safe_api_error(bedrock['error_reason'])}"
                if bedrock["error_reason"]
                else ""
            )
        )
    else:
        st.success(bedrock["banner_message"])
    meta_col, text_col = st.columns([1, 2.2])
    with meta_col:
        st.markdown(
            panel(
                "Model trace",
                [
                    ("Model ID", esc(bedrock["model_id"])),
                    (
                        "Result status",
                        badge(
                            bedrock["result_status"], "warn" if bedrock["fallback_used"] else "ok"
                        ),
                    ),
                    (
                        "Fallback used",
                        badge(
                            bedrock["fallback_used"], "warn" if bedrock["fallback_used"] else "ok"
                        ),
                    ),
                    (
                        "Error reason",
                        esc(
                            format_safe_api_error(bedrock["error_reason"])
                            if bedrock["error_reason"]
                            else "—"
                        ),
                    ),
                ],
                "✦",
            ),
            unsafe_allow_html=True,
        )
    with text_col:
        st.markdown(f"**Situation summary** — {bedrock['situation_summary']}")
        st.markdown(f"**Risk explanation** — {bedrock['risk_explanation']}")
        st.markdown(f"**Recommendation rationale** — {bedrock['recommendation_rationale']}")
        st.markdown(f"**Operator summary** — {bedrock['operator_summary']}")

st.markdown(
    '<div class="section-head"><h2>05 / Verification and evidence</h2><span>Post-action controls · synthetic verification</span></div>',
    unsafe_allow_html=True,
)
verification_col, evidence_col = st.columns(2)
verification_data: dict[str, Any] = view.get("verification_details") or {}
with verification_col:
    st.markdown(
        panel(
            "Verification checks",
            [
                ("Verification status", badge(view.get("verification", "—"))),
                (
                    "Mode",
                    badge(
                        "SYNTHETIC" if verification_data.get("synthetic", True) else "LIVE", "info"
                    ),
                ),
                (
                    "Action result verified",
                    esc(verification_data.get("action_result_verified", "—")),
                ),
                ("Asset state", esc(verification_data.get("asset_state", "—"))),
                ("Post-action check", esc(verification_data.get("post_action_check", "—"))),
                ("Outcome verified", esc(verification_data.get("outcome_verified", "—"))),
            ],
            "✓",
        ),
        unsafe_allow_html=True,
    )
    for reason in verification_data.get("verification_reasons") or []:
        st.caption(f"• {reason}")
evidence_items: list[dict[str, Any]] = view.get("evidence") or []
with evidence_col:
    st.markdown(
        panel(
            "Evidence chain",
            [
                ("Evidence records", esc(len(evidence_items))),
                (
                    "Latest source",
                    esc(evidence_items[-1].get("source", "—") if evidence_items else "—"),
                ),
                (
                    "Latest status",
                    esc(evidence_items[-1].get("status", "—") if evidence_items else "—"),
                ),
                (
                    "Synthetic data",
                    esc(
                        any(item.get("synthetic") for item in evidence_items)
                        if evidence_items
                        else "—"
                    ),
                ),
                ("Trace ID", esc(view.get("trace_id"))),
            ],
            "⌁",
        ),
        unsafe_allow_html=True,
    )
    if st.button(
        "⌁  VIEW EVIDENCE", key="view_evidence", disabled=not (api_connected and has_run(view))
    ):
        try:
            with st.spinner("Loading evidence bundle..."):
                st.session_state.evidence_bundle = api_client.get_evidence(
                    str(view["run_id"]), trace_id=view.get("trace_id")
                )
            st.session_state.show_evidence = True
        except RunApiError as exc:
            mark_backend_down(exc)
            st.error(f"Evidence bundle unavailable: {exc.safe_message}")

if st.session_state.show_evidence and st.session_state.evidence_bundle:
    evidence_summary = format_evidence_bundle_summary(
        st.session_state.evidence_bundle,
        fallback_evidence=evidence_items,
        fallback_cw_counts=view.get("cloudwatch_event_counts"),
    )
    with st.container(border=True):
        st.markdown('<div class="eyebrow">EVIDENCE BUNDLE</div>', unsafe_allow_html=True)
        count_cards = [
            ("Application evidence", evidence_summary["app_evidence_count"]),
            ("CloudWatch total", evidence_summary["cloudwatch_total_count"]),
            ("Agent 1 CloudWatch", evidence_summary["cloudwatch_agent1_count"]),
            ("Agent 2 CloudWatch", evidence_summary["cloudwatch_agent2_count"]),
        ]
        for col, (label, value) in zip(st.columns(4), count_cards, strict=True):
            col.markdown(metric_card(label, value, "cyan"), unsafe_allow_html=True)
        if evidence_summary["rows"]:
            st.dataframe(
                pd.DataFrame(evidence_summary["rows"]), use_container_width=True, hide_index=True
            )

st.markdown(
    '<div class="section-head"><h2>06 / Execution history</h2><span>Current browser session</span></div>',
    unsafe_allow_html=True,
)
with st.container(border=True):
    history_df = pd.DataFrame(
        st.session_state.execution_history,
        columns=["Timestamp", "Run ID", "Action", "Result", "Details", "Approval", "User"],
    )
    if history_df.empty:
        st.caption(
            "No actions recorded yet. Run or approve/reject an action to populate this card."
        )
    else:
        st.dataframe(history_df, use_container_width=True, hide_index=True)
        history_buffer = BytesIO()
        with pd.ExcelWriter(history_buffer, engine="openpyxl") as writer:
            history_df.to_excel(writer, sheet_name="Execution History", index=False)
        st.download_button(
            "⇩  Download History",
            data=history_buffer.getvalue(),
            file_name="network-resilience-execution-history.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=False,
            key="history_excel",
        )

service_impact: dict[str, Any] = view.get("service_impact") or {}
with st.expander("Workflow timeline and service impact", expanded=False):
    timeline = view.get("workflow_stages") or []
    if timeline:
        st.dataframe(pd.DataFrame(timeline), use_container_width=True, hide_index=True)
    st.markdown(
        panel(
            "Service exposure",
            [
                ("Affected services", esc(service_impact.get("affected_service_count", "—"))),
                ("Service risk", esc(service_impact.get("service_risk", "—"))),
                (
                    "Critical elements",
                    esc(service_impact.get("critical_network_element_count", "—")),
                ),
                (
                    "High-criticality elements",
                    esc(service_impact.get("high_criticality_network_element_count", "—")),
                ),
                (
                    "Redundancy for all",
                    esc(service_impact.get("redundancy_available_for_all", "—")),
                ),
                ("Impact summary", esc(service_impact.get("impact_summary", "—"))),
            ],
            "▦",
        ),
        unsafe_allow_html=True,
    )

with st.expander("AI explainability and recommendation"):
    st.markdown(
        panel(
            "Recommendation",
            [
                ("Recommendation ID", esc(recommendation.get("recommendation_id", "—"))),
                ("Action", esc(recommendation.get("action", "—"))),
                (
                    "Targets",
                    esc(", ".join(recommendation.get("target_network_elements") or []) or "—"),
                ),
                ("Confidence", esc(recommendation.get("confidence", "—"))),
                ("State changing", esc(recommendation.get("state_changing", "—"))),
                (
                    "Human approval required",
                    esc(recommendation.get("human_approval_required", "—")),
                ),
                ("Rationale", esc(recommendation.get("rationale", "—"))),
            ],
            "✦",
        ),
        unsafe_allow_html=True,
    )

if evidence_items:
    with st.expander("Evidence"):
        st.dataframe(pd.DataFrame(evidence_items), use_container_width=True, hide_index=True)

audit_records: list[dict[str, Any]] = view.get("audit_trace") or []
summary_df = pd.DataFrame(
    [
        {
            "Report type": "Network Resilience Command Center",
            "Generated at": datetime.now().isoformat(timespec="seconds"),
            "Run ID": view.get("run_id"),
            "Trace ID": view.get("trace_id"),
            "Region": view.get("region"),
            "Weather event": view.get("event"),
            "Severity": view.get("severity"),
            "Status": view.get("status"),
            "Policy decision": view.get("policy_decision"),
            "Human approval": view.get("approval"),
            "Execution": view.get("execution"),
            "Verification": view.get("verification"),
            "Overall risk": view.get("overall_risk"),
            "Risk score": risk_data.get("risk_score", "—"),
            "Network risk": risk_data.get("network_risk", "—"),
            "Service risk": risk_data.get("service_risk", "—"),
            "Recommendation": recommendation.get("action", "—"),
            "Recommendation confidence": recommendation.get("confidence", "—"),
            "Bedrock fallback used": bedrock["fallback_used"] if bedrock["available"] else "—",
        }
    ]
)
assets_df = pd.DataFrame(backend_assets) if backend_assets else pd.DataFrame(columns=["ne_id"])
assets_df["run_id"] = view.get("run_id")
trace_df = pd.DataFrame(
    [
        {
            "Sequence": f"{index + 1:02d}",
            "Stage": record.get("stage", "—"),
            "Event type": record.get("event_type", "—"),
            "Status": record.get("status", "—"),
            "Source": record.get("source", "—"),
            "Recorded at": record.get("recorded_at", "—"),
            "Summary": record.get("summary", "—"),
        }
        for index, record in enumerate(audit_records)
    ],
    columns=["Sequence", "Stage", "Event type", "Status", "Source", "Recorded at", "Summary"],
)
excel_buffer = BytesIO()
with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
    summary_df.to_excel(writer, sheet_name="Run Summary", index=False)
    assets_df.to_excel(writer, sheet_name="Affected Assets", index=False)
    trace_df.to_excel(writer, sheet_name="Audit Trace", index=False)
    if current_events:
        pd.DataFrame([format_activity_log_row(event) for event in current_events]).to_excel(
            writer, sheet_name="SSE Events", index=False
        )

audit_title, report_action = st.columns([4.8, 1.2])
with audit_title:
    st.markdown(
        '<div class="section-head"><h2>07 / Audit trace</h2><span class="mono">Immutable event lineage</span></div>',
        unsafe_allow_html=True,
    )
with report_action:
    st.markdown('<div class="report-action"></div>', unsafe_allow_html=True)
    st.download_button(
        "⇩  Report",
        data=excel_buffer.getvalue(),
        file_name=f"{view.get('run_id', 'run')}-resilience-report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        key="excel_report",
        disabled=not has_run(view),
    )
if not trace_df.empty:
    st.dataframe(trace_df, use_container_width=True, hide_index=True)
st.markdown(
    '<div class="trace">Weather → Geo → Assets → Risk → Recommendation → Policy<br>'
    "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;→ Approval → Action → Verification → Evidence<br><br>"
    f'<span class="cyan">TRACE ID</span>  {esc(view.get("trace_id"))} &nbsp; <span class="cyan">REGION</span>  {esc(view.get("region"))} '
    f'&nbsp; <span class="cyan">MODE</span>  {esc(api_health.get("data_mode") or view.get("data_source"))}</div>',
    unsafe_allow_html=True,
)
st.caption(
    "Excel workbook includes run summary, affected asset inventory, audit trace, and streamed SSE events."
)
st.caption(
    "NRC² dashboard · All risk, governance, approval, execution, and verification states come from the Command Center Backend API · No live network changes are performed"
)
