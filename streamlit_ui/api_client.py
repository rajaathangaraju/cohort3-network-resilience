"""HTTP & SSE client and pure view-mapping helpers for the Streamlit Command Center UI.

The Streamlit UI is strictly a presentation and operator-interaction layer:
- Never invokes AgentCore Agent 1 or Agent 2 directly.
- Never calculates risk scores, overall risk, recommendations, or governance decisions.
- Never stores or exposes AWS credentials, session tokens, private keys, or raw stack traces.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HIGH_LEVEL_WORKFLOW_STEPS: tuple[tuple[str, str, str], ...] = (
    ("WEATHER", "⛈️", "Weather alert"),
    ("GEO", "◎", "Geo correlation"),
    ("ASSET", "◈", "Asset impact"),
    ("RISK", "⚠", "Risk assessment"),
    ("ACTION", "⚙", "Preventive action"),
    ("VERIFY", "✓", "Verification"),
    ("EVIDENCE", "⌁", "Evidence"),
)

EVENT_TO_HIGH_LEVEL_STAGE: dict[str, str] = {
    "connected": "WEATHER",
    "run_started": "WEATHER",
    "agent1_started": "WEATHER",
    "weather_received": "WEATHER",
    "weather_completed": "WEATHER",
    "geo_correlation_completed": "GEO",
    "asset_mapping_completed": "ASSET",
    "agent1_completed": "ASSET",
    "agent2_started": "ASSET",
    "inventory_enriched": "ASSET",
    "service_impact_completed": "ASSET",
    "risk_assessed": "RISK",
    "bedrock_reasoning_completed": "RISK",
    "recommendation_generated": "RISK",
    "governance_decided": "RISK",
    "approval_pending": "ACTION",
    "approval_approved": "ACTION",
    "approval_rejected": "ACTION",
    "action_started": "ACTION",
    "action_executed": "ACTION",
    "action_blocked": "ACTION",
    "verification_started": "VERIFY",
    "verification_passed": "VERIFY",
    "verification_blocked": "VERIFY",
    "evidence_captured": "EVIDENCE",
    "agent2_completed": "EVIDENCE",
    "run_completed": "EVIDENCE",
    "run_failed": "EVIDENCE",
}

BACKEND_STAGE_TO_HIGH_LEVEL: dict[str, str] = {
    "WEATHER": "WEATHER",
    "GEO_CORRELATION": "GEO",
    "GEO": "GEO",
    "ASSET_IMPACT": "ASSET",
    "ASSET": "ASSET",
    "RISK_ASSESSMENT": "RISK",
    "RECOMMENDATION": "RISK",
    "GOVERNANCE": "RISK",
    "RISK": "RISK",
    "APPROVAL": "ACTION",
    "ACTION": "ACTION",
    "VERIFICATION": "VERIFY",
    "VERIFY": "VERIFY",
    "EVIDENCE": "EVIDENCE",
}

DEMO_SCENARIOS: tuple[str, ...] = ("pause", "allow", "deny")
SCENARIO_OPTIONS: tuple[str, ...] = (*DEMO_SCENARIOS, "custom")
WEATHER_EVENT_OPTIONS: tuple[str, ...] = (
    "SEVERE_STORM",
    "HIGH_WIND",
    "FLOOD",
    "HURRICANE",
    "ICE_STORM",
    "HEATWAVE",
    "WILDFIRE",
)
SEVERITY_OPTIONS: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
REGION_OPTIONS: tuple[str, ...] = (
    "Demo-Region-A",
    "Region-A",
    "Region-B",
    "Region-C",
    "Demo-Region-Deny",
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(
        r"(?i)(aws_secret_access_key|aws_session_token|authorization|bearer|x-api-key)"
        r"\s*[:=]\s*[^\s,;\"']+"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


class RunApiError(RuntimeError):
    """Raised when the Command Center Backend API cannot complete an operation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        safe = format_safe_api_error(message)
        super().__init__(safe)
        self.safe_message = safe
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class StreamlitUiConfig:
    """Environment-driven configuration for the Streamlit Command Center UI."""

    api_base_url: str = "http://127.0.0.1:8000"
    data_mode: str = "aws_live"
    sse_connect_timeout_seconds: float = 10.0
    sse_read_timeout_seconds: float = 300.0
    sse_reconnect_delay_seconds: float = 2.0


def load_ui_config(env: dict[str, str] | None = None) -> StreamlitUiConfig:
    """Load Streamlit UI configuration from environment variables without hardcoding IPs."""
    source = env if env is not None else os.environ
    raw_url = (
        source.get("COMMAND_CENTER_API_BASE_URL")
        or source.get("NETWORK_RESILIENCE_API_URL")
        or "http://127.0.0.1:8000"
    ).strip()
    raw_mode = (source.get("COMMAND_CENTER_DATA_MODE") or "aws_live").strip().lower()
    connect_timeout = _parse_positive_float(source.get("SSE_CONNECT_TIMEOUT_SECONDS"), default=10.0)
    read_timeout = _parse_positive_float(source.get("SSE_READ_TIMEOUT_SECONDS"), default=300.0)
    reconnect_delay = _parse_positive_float(source.get("SSE_RECONNECT_DELAY_SECONDS"), default=2.0)
    return StreamlitUiConfig(
        api_base_url=raw_url.rstrip("/"),
        data_mode=raw_mode,
        sse_connect_timeout_seconds=connect_timeout,
        sse_read_timeout_seconds=read_timeout,
        sse_reconnect_delay_seconds=reconnect_delay,
    )


def _parse_positive_float(raw: str | None, *, default: float) -> float:
    if not raw:
        return default
    try:
        val = float(raw)
        return val if val > 0 else default
    except ValueError:
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def format_safe_api_error(raw: object) -> str:
    """Sanitize an error message for operator display without stack traces or secrets."""
    text = str(raw or "Command Center backend request failed.").strip()
    if "Traceback (most recent call last)" in text:
        text = text.split("Traceback (most recent call last)")[0].strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = lines[0] if lines else "Command Center backend request failed."
    if len(summary) > 360:
        summary = summary[:357] + "..."
    return summary


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret patterns from a JSON-like structure for raw display."""
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def build_run_request(
    scenario: str,
    *,
    weather_event: str,
    severity: str,
    affected_region: str,
) -> dict[str, Any]:
    """Build the run request body for POST /api/runs or /api/runs/stream.

    Named demo scenarios send only `scenario`: the backend overrides preset inputs with any
    supplied weather fields, which would change the governed outcome of the preset.
    """
    cleaned = (scenario or "").strip().lower()
    if cleaned in DEMO_SCENARIOS:
        return {"scenario": cleaned}
    return {
        "weather_event": weather_event.strip().upper(),
        "severity": severity.strip().upper(),
        "affected_region": affected_region.strip(),
    }


def is_approval_actionable(policy_decision: str | None, approval_status: str | None) -> bool:
    """Approve/Reject are only valid for governance PAUSE with approval PENDING."""
    return (policy_decision or "").upper() == "PAUSE" and (approval_status or "").upper() == (
        "PENDING"
    )


def build_api_url(base_url: str, path: str, query: dict[str, str] | None = None) -> str:
    """Construct a normalized full URL against the configured backend API base URL."""
    normalized_base = base_url.rstrip("/") + "/"
    joined = urljoin(normalized_base, path.lstrip("/"))
    if query:
        clean_q = {k: v for k, v in query.items() if v is not None and str(v).strip() != ""}
        if clean_q:
            return f"{joined}?{urlencode(clean_q)}"
    return joined


def parse_sse_frame_lines(lines: Iterable[str]) -> dict[str, Any] | None:
    """Parse a single SSE event block (lines between blank lines) into a dict.

    Returns None for transport comments (e.g. `: heartbeat`) or empty frames.
    Raises RunApiError if the `data:` payload is malformed JSON.
    """
    cleaned_lines = [line.rstrip("\r") for line in lines if line.strip() != ""]
    if not cleaned_lines:
        return None
    if all(line.startswith(":") for line in cleaned_lines):
        return None

    event_id: str | None = None
    event_type: str | None = None
    retry_ms: int | None = None
    data_lines: list[str] = []

    for line in cleaned_lines:
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("retry:"):
            raw_retry = line[6:].strip()
            if raw_retry.isdigit():
                retry_ms = int(raw_retry)
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))

    if not data_lines:
        return None

    raw_data = "\n".join(data_lines)
    try:
        parsed = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise RunApiError("Malformed SSE event payload received from backend.") from exc

    if not isinstance(parsed, dict):
        raise RunApiError("SSE event data must be a JSON object.")

    if event_id and not parsed.get("event_id"):
        parsed["event_id"] = event_id
    if event_type and not parsed.get("event_type"):
        parsed["event_type"] = event_type
    if retry_ms is not None:
        parsed["retry_ms"] = retry_ms
    return parsed


def iter_sse_events_from_lines(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    """Incrementally parse an iterable of SSE wire lines into validated event dictionaries."""
    buffer: list[str] = []
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, bytes)
            else str(raw_line)
        )
        stripped = line.rstrip("\r\n")
        if stripped == "":
            if buffer:
                frame = parse_sse_frame_lines(buffer)
                buffer = []
                if frame is not None:
                    yield frame
            continue
        buffer.append(stripped)

    if buffer:
        frame = parse_sse_frame_lines(buffer)
        if frame is not None:
            yield frame


def should_accept_sse_event(
    event: dict[str, Any],
    *,
    seen_event_ids: set[str],
    last_sequence: int,
    expected_run_id: str | None = None,
    expected_trace_id: str | None = None,
) -> tuple[bool, int]:
    """Validate SSE event deduplication (`event_id`), monotonic `sequence`, and correlation IDs.

    Returns `(accepted, updated_last_sequence)`.
    """
    event_id = str(event.get("event_id") or "").strip()
    if event_id and event_id in seen_event_ids:
        return False, last_sequence

    raw_seq = event.get("sequence")
    if isinstance(raw_seq, int):
        if raw_seq <= last_sequence:
            return False, last_sequence
        next_seq = raw_seq
    else:
        next_seq = last_sequence + 1

    ev_run_id = str(event.get("run_id") or "").strip()
    if expected_run_id and ev_run_id and ev_run_id != expected_run_id:
        return False, last_sequence

    ev_trace_id = str(event.get("trace_id") or "").strip()
    if expected_trace_id and ev_trace_id and ev_trace_id != expected_trace_id:
        return False, last_sequence

    if event_id:
        seen_event_ids.add(event_id)
    return True, next_seq


def map_event_to_high_level_stage(event_type: str, stage: str | None = None) -> str:
    """Map a backend SSE event_type or stage name to one of the 7 high-level UI steps."""
    cleaned_type = (event_type or "").strip().lower()
    if cleaned_type in EVENT_TO_HIGH_LEVEL_STAGE:
        return EVENT_TO_HIGH_LEVEL_STAGE[cleaned_type]
    cleaned_stage = (stage or "").strip().upper()
    return BACKEND_STAGE_TO_HIGH_LEVEL.get(cleaned_stage, "WEATHER")


def build_workflow_stage_bar(
    *,
    workflow_stages: list[dict[str, Any]] | None = None,
    sse_events: list[dict[str, Any]] | None = None,
    governance_decision: str | None = None,
    approval_status: str | None = None,
    execution_status: str | None = None,
    verification_status: str | None = None,
) -> list[dict[str, str]]:
    """Build the 7 high-level workflow cards from authoritative backend stages and SSE events."""
    statuses: dict[str, str] = {code: "PENDING" for code, _, _ in HIGH_LEVEL_WORKFLOW_STEPS}

    for item in workflow_stages or []:
        raw_stage = str(item.get("stage") or "").upper()
        raw_status = str(item.get("status") or "PENDING").upper()
        high_stage = BACKEND_STAGE_TO_HIGH_LEVEL.get(raw_stage)
        if not high_stage:
            continue
        # For RISK (which combines RISK_ASSESSMENT, RECOMMENDATION, GOVERNANCE),
        # keep COMPLETED if risk/recommendation completed even when governance is PAUSED/BLOCKED,
        # or reflect governance status when appropriate.
        if high_stage == "RISK" and raw_stage in {"RISK_ASSESSMENT", "RECOMMENDATION"}:
            if statuses["RISK"] == "PENDING":
                statuses["RISK"] = raw_status
        elif high_stage == "RISK" and raw_stage == "GOVERNANCE":
            if raw_status in {"FAILED"} or statuses["RISK"] == "PENDING":
                statuses["RISK"] = raw_status
        elif high_stage == "ACTION" and raw_stage == "APPROVAL":
            if raw_status == "PAUSED":
                statuses["ACTION"] = "PAUSED"
            elif raw_status in {"BLOCKED", "FAILED"} and statuses["ACTION"] == "PENDING":
                statuses["ACTION"] = raw_status
        else:
            statuses[high_stage] = raw_status

    for ev in sse_events or []:
        ev_type = str(ev.get("event_type") or "")
        ev_stage = str(ev.get("stage") or "")
        ev_status = str(ev.get("status") or "IN_PROGRESS").upper()
        high_stage = map_event_to_high_level_stage(ev_type, ev_stage)
        if ev_type == "run_completed":
            statuses["EVIDENCE"] = "COMPLETED"
            continue
        if ev_type == "run_failed":
            failed_target = BACKEND_STAGE_TO_HIGH_LEVEL.get(
                str((ev.get("data") or {}).get("failed_stage") or ev_stage).upper(),
                high_stage,
            )
            statuses[failed_target] = "FAILED"
            continue
        if ev_type == "approval_pending":
            statuses["ACTION"] = "PAUSED"
            continue
        if ev_type == "action_blocked" and statuses["ACTION"] == "PAUSED":
            continue
        if high_stage == "RISK" and ev_type == "governance_decided":
            statuses["RISK"] = "COMPLETED"
            continue
        statuses[high_stage] = ev_status

    gov = (governance_decision or "").upper()
    apr = (approval_status or "").upper()
    exe = (execution_status or "").upper()
    ver = (verification_status or "").upper()

    if gov == "PAUSE" and apr == "PENDING":
        statuses["ACTION"] = "PAUSED"
        if ver in {"NOT_STARTED", "PENDING", ""}:
            statuses["VERIFY"] = "PENDING"
    elif gov == "DENY" or apr == "REJECTED":
        if exe == "BLOCKED":
            statuses["ACTION"] = "BLOCKED"
        if ver == "BLOCKED":
            statuses["VERIFY"] = "BLOCKED"
    elif exe == "EXECUTED":
        statuses["ACTION"] = "COMPLETED"
        if ver == "PASSED":
            statuses["VERIFY"] = "COMPLETED"

    return [
        {
            "stage": code,
            "icon": icon,
            "name": label,
            "status": statuses.get(code, "PENDING"),
        }
        for code, icon, label in HIGH_LEVEL_WORKFLOW_STEPS
    ]


def calculate_workflow_progress(
    stage_bar: list[dict[str, str]],
    *,
    workflow_status: str | None = None,
    governance_decision: str | None = None,
    approval_status: str | None = None,
) -> tuple[float, str]:
    """Calculate stage-completion ratio and label from high-level workflow stage states.

    - Never shows 100% while PAUSED / AWAITING_APPROVAL.
    - Distinguishes terminal BLOCKED / FAILED runs from full execution completion.
    """
    if not stage_bar:
        return 0.0, "IDLE"

    total = len(stage_bar)
    completed = sum(1 for item in stage_bar if item.get("status") == "COMPLETED")
    in_progress = sum(1 for item in stage_bar if item.get("status") == "IN_PROGRESS")
    failed = any(item.get("status") == "FAILED" for item in stage_bar)
    paused = any(item.get("status") == "PAUSED" for item in stage_bar)
    blocked = any(item.get("status") == "BLOCKED" for item in stage_bar)

    wf = (workflow_status or "").upper()
    gov = (governance_decision or "").upper()
    apr = (approval_status or "").upper()

    if failed or "FAILED" in wf:
        ratio = min(0.95, max(0.1, completed / total))
        return ratio, f"FAILED ({completed}/{total} stages completed)"

    if paused or (gov == "PAUSE" and apr == "PENDING") or "AWAITING" in wf or "PAUSED" in wf:
        ratio = min(0.85, completed / total)
        return ratio, f"PAUSED / AWAITING APPROVAL ({completed}/{total} stages completed)"

    if blocked and (gov == "DENY" or apr == "REJECTED" or "BLOCKED" in wf or "DENIED" in wf):
        ratio = completed / total
        return ratio, f"TERMINAL BLOCKED ({completed}/{total} stages completed)"

    if completed == total:
        return 1.0, f"COMPLETED ({total}/{total} stages completed)"

    ratio = min(0.95, (completed + 0.5 * in_progress) / total)
    if completed == 0 and in_progress == 0:
        return 0.0, "READY"
    return ratio, f"IN PROGRESS ({completed}/{total} stages completed)"


def empty_authoritative_state() -> dict[str, Any]:
    """Return an empty initial UI state prior to any backend run execution."""
    return {
        "run_id": "—",
        "trace_id": "—",
        "status": "READY",
        "overall_risk": "—",
        "approval": "—",
        "execution": "—",
        "verification": "—",
        "region": "Demo-Region-A",
        "severity": "HIGH",
        "event": "SEVERE_STORM",
        "forecast": "—",
        "schema_version": "0.1",
        "team_code": "—",
        "created_at": "—",
        "data_source": "—",
        "geo_match_status": "—",
        "asset_mapping_status": "—",
        "affected_area": {},
        "affected_assets": [],
        "unresolved_network_elements": [],
        "service_impact": {},
        "risk_assessment": {},
        "recommendation": {},
        "governance": {},
        "approval_details": {},
        "execution_details": {},
        "verification_details": {},
        "status_card": {},
        "workflow_stages": [],
        "evidence": [],
        "audit_trace": [],
        "ai_reasoning": {},
        "agent1_result": None,
        "agent2_result": None,
        "cloudwatch_event_counts": {"total": 0, "agent1": 0, "agent2": 0},
        "raw_payload": {},
    }


def normalize_api_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Map an authoritative backend run (`CommandCenterView` or `Agent2Result`) for UI display.

    Performs zero risk or governance calculation—reads exclusively from backend fields.
    """
    if not payload:
        return empty_authoritative_state()

    command = _as_dict(payload.get("command_center"))
    agent2_res = _as_dict(payload.get("agent2_result"))
    agent1_res = _as_dict(payload.get("agent1_result"))

    header = payload.get("header") or command.get("header") or {}
    context = payload.get("input_context") or agent2_res.get("input_context") or {}
    weather = payload.get("weather") or command.get("weather") or {}
    geo = payload.get("geo") or command.get("geo") or {}
    assets_block = payload.get("assets") or command.get("assets") or {}
    risk = (
        payload.get("risk")
        or payload.get("risk_assessment")
        or command.get("risk")
        or agent2_res.get("risk_assessment")
        or {}
    )
    recommendation = (
        payload.get("recommendation")
        or command.get("recommendation")
        or agent2_res.get("recommendation")
        or {}
    )
    governance = (
        payload.get("governance") or command.get("governance") or agent2_res.get("governance") or {}
    )
    approval = (
        payload.get("approval") or command.get("approval") or agent2_res.get("approval") or {}
    )
    execution = (
        payload.get("execution") or command.get("execution") or agent2_res.get("execution") or {}
    )
    verification = (
        payload.get("verification")
        or command.get("verification")
        or agent2_res.get("verification")
        or {}
    )
    status_card = payload.get("status_card") or command.get("status_card") or {}
    ai_reasoning = (
        payload.get("ai_reasoning")
        or command.get("ai_reasoning")
        or agent2_res.get("ai_reasoning")
        or {}
    )
    service_impact = (
        payload.get("service_impact")
        or agent2_res.get("service_impact")
        or {
            "affected_service_count": assets_block.get("affected_service_count"),
            "critical_network_element_count": assets_block.get("critical_ne_count"),
            "service_risk": risk.get("service_risk"),
        }
    )

    affected_assets = (
        payload.get("affected_assets")
        or assets_block.get("asset_details")
        or agent2_res.get("affected_assets")
        or []
    )
    unresolved = (
        payload.get("unresolved_network_elements")
        or assets_block.get("unresolved_ne_ids")
        or agent2_res.get("unresolved_network_elements")
        or []
    )
    workflow_stages = (
        payload.get("workflow_stages")
        or command.get("workflow_stages")
        or agent2_res.get("workflow_stages")
        or []
    )
    evidence = (
        payload.get("evidence") or command.get("evidence") or agent2_res.get("evidence") or []
    )
    audit_trace = payload.get("audit_trace") or command.get("audit_trace") or evidence

    run_id = str(payload.get("run_id") or header.get("run_id") or agent2_res.get("run_id") or "—")
    trace_id = str(
        payload.get("trace_id") or header.get("trace_id") or agent2_res.get("trace_id") or "—"
    )
    workflow_status = str(
        payload.get("overall_status")
        or header.get("workflow_status")
        or payload.get("status")
        or "COMPLETED"
    )
    overall_risk = str(header.get("overall_risk") or risk.get("overall_risk") or "—")

    policy_decision = str(status_card.get("policy_decision") or governance.get("decision") or "—")
    approval_status = str(status_card.get("human_approval") or approval.get("status") or "—")
    execution_status = str(status_card.get("execution") or execution.get("status") or "—")
    verification_status = str(status_card.get("verification") or verification.get("status") or "—")

    cw_counts = payload.get("cloudwatch_event_counts") or {"total": 0, "agent1": 0, "agent2": 0}

    return {
        "run_id": run_id,
        "trace_id": trace_id,
        "status": workflow_status,
        "overall_risk": overall_risk,
        "policy_decision": policy_decision,
        "approval": approval_status,
        "execution": execution_status,
        "verification": verification_status,
        "region": str(
            weather.get("affected_region")
            or geo.get("region")
            or context.get("affected_region")
            or agent1_res.get("affected_region")
            or "—"
        ),
        "severity": str(
            weather.get("severity") or context.get("severity") or agent1_res.get("severity") or "—"
        ),
        "event": str(
            weather.get("event")
            or context.get("weather_event")
            or agent1_res.get("weather_event")
            or "—"
        ),
        "forecast": str(
            weather.get("forecast") or context.get("forecast") or agent1_res.get("forecast") or "—"
        ),
        "schema_version": str(
            header.get("schema_version")
            or payload.get("schema_version")
            or agent2_res.get("schema_version")
            or "0.1"
        ),
        "team_code": str(
            header.get("team_code")
            or payload.get("team_code")
            or agent2_res.get("team_code")
            or "—"
        ),
        "created_at": str(payload.get("created_at") or agent2_res.get("created_at") or "—"),
        "data_source": str(
            payload.get("data_source") or agent2_res.get("data_source") or "synthetic"
        ),
        "geo_match_status": str(geo.get("geo_match_status") or "—"),
        "asset_mapping_status": str(geo.get("asset_mapping_status") or "—"),
        "affected_area": (
            geo.get("affected_area")
            or context.get("affected_area")
            or agent1_res.get("affected_area")
            or {}
        ),
        "affected_ne_count": assets_block.get("affected_ne_count", len(affected_assets)),
        "critical_ne_count": assets_block.get(
            "critical_ne_count",
            service_impact.get("critical_network_element_count", 0),
        ),
        "affected_service_count": assets_block.get(
            "affected_service_count",
            service_impact.get("affected_service_count", 0),
        ),
        "affected_assets": list(affected_assets),
        "unresolved_network_elements": list(unresolved),
        "service_impact": dict(service_impact),
        "risk_assessment": dict(risk),
        "recommendation": dict(recommendation),
        "governance": dict(governance),
        "approval_details": dict(approval),
        "execution_details": dict(execution),
        "verification_details": dict(verification),
        "status_card": {
            "policy_decision": policy_decision,
            "human_approval": approval_status,
            "execution": execution_status,
            "verification": verification_status,
        },
        "workflow_stages": list(workflow_stages),
        "evidence": list(evidence),
        "audit_trace": list(audit_trace),
        "ai_reasoning": dict(ai_reasoning) if isinstance(ai_reasoning, dict) else {},
        "agent1_result": agent1_res or None,
        "agent2_result": agent2_res or None,
        "cloudwatch_event_counts": dict(cw_counts),
        "raw_payload": payload,
    }


def apply_sse_event_to_state(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Incrementally update UI display state from a single validated WorkflowSSEEvent dict."""
    updated = dict(state)
    ev_type = str(event.get("event_type") or "")
    ev_status = str(event.get("status") or "")
    data = _as_dict(event.get("data"))

    if event.get("run_id") and updated.get("run_id") in {"", "—", None}:
        updated["run_id"] = str(event["run_id"])
    if event.get("trace_id") and updated.get("trace_id") in {"", "—", None}:
        updated["trace_id"] = str(event["trace_id"])

    if ev_type == "connected":
        updated["status"] = "CONNECTED"
    elif ev_type == "run_started":
        updated["run_id"] = str(data.get("run_id") or event.get("run_id") or updated.get("run_id"))
        updated["trace_id"] = str(
            data.get("trace_id") or event.get("trace_id") or updated.get("trace_id")
        )
        updated["status"] = "RUNNING"
        if data.get("data_mode"):
            updated["data_source"] = str(data["data_mode"])
    elif ev_type == "agent1_started":
        updated["status"] = "AGENT1_PROCESSING"
    elif ev_type in {"weather_received", "weather_completed"}:
        if data.get("weather_event"):
            updated["event"] = str(data["weather_event"])
        if data.get("severity"):
            updated["severity"] = str(data["severity"])
        if data.get("affected_region"):
            updated["region"] = str(data["affected_region"])
    elif ev_type == "geo_correlation_completed":
        if data.get("affected_region"):
            updated["region"] = str(data["affected_region"])
        if isinstance(data.get("affected_area"), dict):
            updated["affected_area"] = dict(data["affected_area"])
        updated["geo_match_status"] = "MATCHED"
    elif ev_type == "asset_mapping_completed":
        updated["asset_mapping_status"] = "COMPLETE"
        if "affected_ne_count" in data:
            updated["affected_ne_count"] = data["affected_ne_count"]
    elif ev_type == "agent1_completed":
        updated["agent1_result"] = dict(data)
    elif ev_type == "agent2_started":
        updated["status"] = "AGENT2_PROCESSING"
    elif ev_type == "inventory_enriched":
        if "unresolved_network_elements" in data:
            updated["unresolved_network_elements"] = list(data["unresolved_network_elements"])
        if "resolved_count" in data:
            updated["affected_ne_count"] = data["resolved_count"]
    elif ev_type == "service_impact_completed":
        svc = dict(updated.get("service_impact") or {})
        svc.update(data)
        updated["service_impact"] = svc
        if "affected_service_count" in data:
            updated["affected_service_count"] = data["affected_service_count"]
        if "critical_network_element_count" in data:
            updated["critical_ne_count"] = data["critical_network_element_count"]
    elif ev_type == "risk_assessed":
        risk = dict(updated.get("risk_assessment") or {})
        risk.update(data)
        updated["risk_assessment"] = risk
        if data.get("overall_risk"):
            updated["overall_risk"] = str(data["overall_risk"])
    elif ev_type == "bedrock_reasoning_completed":
        updated["ai_reasoning"] = {
            "model_id": data.get("model_id"),
            "result_status": data.get("result_status"),
            "fallback_used": bool(data.get("fallback_used", False)),
            "error_reason": data.get("error_reason"),
            "reasoning": {
                "situation_summary": data.get("situation_summary"),
                "risk_explanation": data.get("risk_explanation"),
                "recommendation_rationale": data.get("recommendation_rationale"),
                "operator_summary": data.get("operator_summary"),
            },
        }
    elif ev_type == "recommendation_generated":
        rec = dict(updated.get("recommendation") or {})
        rec.update(data)
        updated["recommendation"] = rec
    elif ev_type == "governance_decided":
        gov = dict(updated.get("governance") or {})
        gov.update(data)
        updated["governance"] = gov
        if data.get("decision"):
            updated["policy_decision"] = str(data["decision"])
    elif ev_type in {"approval_pending", "approval_approved", "approval_rejected"}:
        apr = dict(updated.get("approval_details") or {})
        apr.update(data)
        updated["approval_details"] = apr
        if data.get("status"):
            updated["approval"] = str(data["status"])
        if ev_type == "approval_pending":
            updated["status"] = "AWAITING_APPROVAL"
    elif ev_type in {"action_started", "action_executed", "action_blocked"}:
        exe = dict(updated.get("execution_details") or {})
        exe.update(data)
        if ev_type == "action_started":
            exe["status"] = "IN_PROGRESS"
            updated["execution"] = "IN_PROGRESS"
        elif data.get("execution"):
            exe["status"] = data["execution"]
            updated["execution"] = str(data["execution"])
        elif ev_type == "action_blocked":
            exe["status"] = "BLOCKED"
            updated["execution"] = "BLOCKED"
        if event.get("message"):
            exe["result_message"] = str(event["message"])
        updated["execution_details"] = exe
        if data.get("approval"):
            updated["approval"] = str(data["approval"])
        if data.get("verification"):
            updated["verification"] = str(data["verification"])
    elif ev_type in {"verification_started", "verification_passed", "verification_blocked"}:
        ver = dict(updated.get("verification_details") or {})
        ver.update(data)
        if ev_type == "verification_started":
            ver["status"] = "IN_PROGRESS"
            updated["verification"] = "IN_PROGRESS"
        elif data.get("verification"):
            ver["status"] = data["verification"]
            updated["verification"] = str(data["verification"])
        if "reasons" in data:
            ver["verification_reasons"] = list(data["reasons"])
        updated["verification_details"] = ver
    elif ev_type == "evidence_captured":
        if "cloudwatch_event_count" in data:
            cw = dict(updated.get("cloudwatch_event_counts") or {})
            cw["total"] = int(data["cloudwatch_event_count"] or 0)
            updated["cloudwatch_event_counts"] = cw
    elif ev_type == "run_completed":
        updated["status"] = str(data.get("workflow_status") or ev_status or "COMPLETED")
        if data.get("overall_risk"):
            updated["overall_risk"] = str(data["overall_risk"])
        if data.get("governance_decision"):
            updated["policy_decision"] = str(data["governance_decision"])
        if data.get("approval_status") or data.get("approval"):
            updated["approval"] = str(data.get("approval_status") or data.get("approval"))
        if data.get("execution_status") or data.get("execution"):
            updated["execution"] = str(data.get("execution_status") or data.get("execution"))
        if data.get("verification_status") or data.get("verification"):
            updated["verification"] = str(
                data.get("verification_status") or data.get("verification")
            )
    elif ev_type == "run_failed":
        updated["status"] = "FAILED"
        updated["stream_error"] = format_safe_api_error(
            data.get("safe_message") or event.get("message") or "Workflow execution failed."
        )

    return updated


def format_bedrock_display_model(ai_reasoning: dict[str, Any] | None) -> dict[str, Any]:
    """Build a read-only display model for the Bedrock reasoning card with fallback notice."""
    if not ai_reasoning:
        return {
            "available": False,
            "model_id": "—",
            "operation": "—",
            "start_status": "—",
            "result_status": "—",
            "fallback_used": False,
            "banner_message": "AI reasoning not yet recorded for this run.",
            "error_reason": None,
            "situation_summary": "—",
            "risk_explanation": "—",
            "recommendation_rationale": "—",
            "operator_summary": "—",
        }

    fallback_used = bool(ai_reasoning.get("fallback_used", False))
    model_id = str(ai_reasoning.get("model_id") or "—")
    reasoning_block = _as_dict(ai_reasoning.get("reasoning")) or ai_reasoning
    banner_message = (
        "Deterministic fallback explanation used."
        if fallback_used
        else f"Live Amazon Bedrock reasoning completed ({model_id})."
    )
    return {
        "available": True,
        "model_id": model_id,
        "operation": str(ai_reasoning.get("operation") or "generate_resilience_explanation"),
        "start_status": str(ai_reasoning.get("start_status") or "—"),
        "result_status": str(ai_reasoning.get("result_status") or "—"),
        "fallback_used": fallback_used,
        "banner_message": banner_message,
        "error_reason": ai_reasoning.get("error_reason"),
        "situation_summary": str(reasoning_block.get("situation_summary") or "—"),
        "risk_explanation": str(reasoning_block.get("risk_explanation") or "—"),
        "recommendation_rationale": str(reasoning_block.get("recommendation_rationale") or "—"),
        "operator_summary": str(reasoning_block.get("operator_summary") or "—"),
    }


def format_evidence_bundle_summary(
    evidence_bundle: dict[str, Any] | None,
    *,
    fallback_evidence: list[dict[str, Any]] | None = None,
    fallback_cw_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Normalize the response from `GET /api/runs/{run_id}/evidence` for UI rendering."""
    bundle = evidence_bundle or {}
    raw_records = bundle.get("evidence") or fallback_evidence or []
    normalized_index = bundle.get("normalized_evidence") or []
    cw_events = bundle.get("cloudwatch_events") or []
    fallback_cw = fallback_cw_counts or {}

    a1_cw = sum(
        1
        for e in cw_events
        if "weather_network_intelligence" in str(e.get("logGroupName", ""))
        or "weather_network_intelligence" in str(e.get("message", ""))
    )
    a2_cw = sum(
        1
        for e in cw_events
        if "network_resilience" in str(e.get("logGroupName", ""))
        or '"agent_name": "network_resilience"' in str(e.get("message", ""))
        or '"agent_name":"network_resilience"' in str(e.get("message", ""))
    )

    total_cw = len(cw_events) if cw_events else int(fallback_cw.get("total", 0))
    agent1_cw = a1_cw if cw_events else int(fallback_cw.get("agent1", 0))
    agent2_cw = a2_cw if cw_events else int(fallback_cw.get("agent2", 0))

    rows: list[dict[str, Any]] = []
    source_records = normalized_index if normalized_index else raw_records
    for item in source_records:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "Evidence ID": str(item.get("evidence_id") or "—"),
                "Stage": str(item.get("stage") or "—"),
                "Event Type": str(item.get("event_type") or "—"),
                "Status": str(item.get("status") or "—"),
                "Recorded At": str(item.get("recorded_at") or item.get("timestamp") or "—"),
                "Source": str(item.get("source") or "—"),
                "Summary": format_safe_api_error(item.get("summary") or "—"),
                "Synthetic": bool(item.get("synthetic", True)),
            }
        )

    return {
        "app_evidence_count": int(bundle.get("evidence_count") or len(raw_records)),
        "normalized_evidence_count": len(rows),
        "cloudwatch_total_count": total_cw,
        "cloudwatch_agent1_count": agent1_cw,
        "cloudwatch_agent2_count": agent2_cw,
        "rows": rows,
        "runtime_metadata": bundle.get("runtime_metadata"),
        "workflow_summary": bundle.get("workflow_summary"),
    }


class RunApiClient:
    """Reusable HTTP & SSE client for the Command Center Backend API."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        sse_read_timeout: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        cfg = load_ui_config()
        resolved_base = (base_url or cfg.api_base_url).strip()
        self.base_url = resolved_base.rstrip("/")
        self.connect_timeout = connect_timeout or cfg.sse_connect_timeout_seconds
        self.read_timeout = read_timeout or 30.0
        self.sse_read_timeout = sse_read_timeout or cfg.sse_read_timeout_seconds
        self.session = session or self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        sess = requests.Session()
        # Only retry safe/idempotent GET requests; never auto-retry state-changing POSTs.
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            status=2,
            allowed_methods=frozenset(["GET"]),
            status_forcelist=(502, 503, 504),
            backoff_factor=0.25,
        )
        adapter = HTTPAdapter(max_retries=retry)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        return sess

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        url = build_api_url(self.base_url, path, query=query)
        headers: dict[str, str] = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        req_timeout = timeout or (self.connect_timeout, self.read_timeout)
        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                json=body,
                headers=headers,
                timeout=req_timeout,
            )
        except requests.Timeout as exc:
            raise RunApiError(
                f"Request to {path} timed out at {self.base_url}.",
                retryable=method.upper() == "GET",
            ) from exc
        except requests.ConnectionError as exc:
            raise RunApiError(
                f"Command Center backend is unavailable at {self.base_url}.",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise RunApiError(
                f"HTTP request to {path} failed: {format_safe_api_error(exc)}",
                retryable=False,
            ) from exc

        content_type = response.headers.get("Content-Type", "")
        if response.status_code >= 400:
            detail_msg = self._extract_error_detail(response)
            raise RunApiError(
                f"{method.upper()} {path} failed ({response.status_code}): {detail_msg}",
                status_code=response.status_code,
                retryable=response.status_code in {502, 503, 504},
            )

        if not response.content:
            return {}

        if "json" not in content_type.lower():
            raise RunApiError(
                f"Unexpected Content-Type '{content_type}' from {path} (expected application/json)."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RunApiError(f"Invalid JSON response returned from {path}.") from exc

        if not isinstance(payload, dict):
            raise RunApiError(f"Expected JSON object from {path}.")
        return payload

    @staticmethod
    def _extract_error_detail(response: requests.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                return format_safe_api_error(
                    data.get("message") or data.get("detail") or json.dumps(data)
                )
        except ValueError:
            pass
        return format_safe_api_error(response.text or f"HTTP {response.status_code}")

    def health_check(self) -> dict[str, Any]:
        """Check backend availability via GET /health with fallback to GET /openapi.json."""
        try:
            return self._request_json(
                "GET",
                "/health",
                timeout=(min(self.connect_timeout, 5.0), 5.0),
            )
        except RunApiError as first_err:
            if first_err.status_code == 404:
                schema = self._request_json(
                    "GET",
                    "/openapi.json",
                    timeout=(min(self.connect_timeout, 5.0), 5.0),
                )
                return {
                    "status": "ok",
                    "service": schema.get("info", {}).get("title", "command_center_backend"),
                }
            raise

    def create_run(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute POST /api/runs without automatic retry on state-changing creation."""
        return self._request_json(
            "POST",
            "/api/runs",
            body=request,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Execute GET /api/runs/{run_id}."""
        return self._request_json("GET", f"/api/runs/{run_id}")

    def approve_run(
        self,
        run_id: str,
        approver_reference: str = "demo-approver",
        comments: str = "",
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute POST /api/runs/{run_id}/approve without automatic retry."""
        payload: dict[str, Any] = {"approver_reference": approver_reference or "demo-approver"}
        if comments:
            payload["comments"] = comments
        return self._request_json(
            "POST",
            f"/api/runs/{run_id}/approve",
            body=payload,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    def reject_run(
        self,
        run_id: str,
        approver_reference: str = "demo-approver",
        comments: str = "",
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute POST /api/runs/{run_id}/reject without automatic retry."""
        payload: dict[str, Any] = {
            "approver_reference": approver_reference or "demo-approver",
            "comments": comments,
        }
        return self._request_json(
            "POST",
            f"/api/runs/{run_id}/reject",
            body=payload,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    def get_evidence(self, run_id: str, *, trace_id: str | None = None) -> dict[str, Any]:
        """Execute GET /api/runs/{run_id}/evidence."""
        query = {"trace_id": trace_id} if trace_id else None
        return self._request_json("GET", f"/api/runs/{run_id}/evidence", query=query)

    def stream_run(
        self,
        request: dict[str, Any],
        *,
        last_event_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream workflow progress events via POST /api/runs/stream (text/event-stream)."""
        url = build_api_url(self.base_url, "/api/runs/stream")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if last_event_id and last_event_id.strip():
            headers["Last-Event-ID"] = last_event_id.strip()

        try:
            response = self.session.post(
                url,
                json=request,
                headers=headers,
                stream=True,
                timeout=(self.connect_timeout, self.sse_read_timeout),
            )
        except requests.Timeout as exc:
            raise RunApiError(
                "SSE workflow stream timed out while waiting for backend events.",
                retryable=True,
            ) from exc
        except requests.ConnectionError as exc:
            raise RunApiError(
                f"Command Center backend is unavailable at {self.base_url}.",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise RunApiError(
                f"SSE stream connection failed: {format_safe_api_error(exc)}",
                retryable=False,
            ) from exc

        with response:
            if response.status_code >= 400:
                detail = self._extract_error_detail(response)
                raise RunApiError(
                    f"POST /api/runs/stream failed ({response.status_code}): {detail}",
                    status_code=response.status_code,
                )
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type.lower():
                raise RunApiError(
                    f"Expected text/event-stream from /api/runs/stream, got '{content_type}'."
                )
            try:
                yield from iter_sse_events_from_lines(response.iter_lines(decode_unicode=True))
            except requests.Timeout as exc:
                raise RunApiError(
                    "SSE stream read timed out before receiving terminal event.",
                    retryable=True,
                ) from exc
            except requests.RequestException as exc:
                raise RunApiError(
                    f"SSE stream interrupted: {format_safe_api_error(exc)}",
                    retryable=True,
                ) from exc

    def replay_run_stream(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Reconnect to GET /api/runs/{run_id}/stream with optional Last-Event-ID header."""
        url = build_api_url(self.base_url, f"/api/runs/{run_id}/stream")
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if last_event_id and last_event_id.strip():
            headers["Last-Event-ID"] = last_event_id.strip()

        try:
            response = self.session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(self.connect_timeout, self.sse_read_timeout),
            )
        except requests.Timeout as exc:
            raise RunApiError(
                f"SSE replay stream for '{run_id}' timed out.",
                retryable=True,
            ) from exc
        except requests.ConnectionError as exc:
            raise RunApiError(
                f"Command Center backend is unavailable at {self.base_url}.",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise RunApiError(
                f"SSE replay request failed: {format_safe_api_error(exc)}",
                retryable=False,
            ) from exc

        with response:
            if response.status_code >= 400:
                detail = self._extract_error_detail(response)
                raise RunApiError(
                    f"GET /api/runs/{run_id}/stream failed ({response.status_code}): {detail}",
                    status_code=response.status_code,
                )
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type.lower():
                raise RunApiError(
                    f"Expected text/event-stream from /api/runs/{run_id}/stream, "
                    f"got '{content_type}'."
                )
            try:
                yield from iter_sse_events_from_lines(response.iter_lines(decode_unicode=True))
            except requests.RequestException as exc:
                raise RunApiError(
                    f"SSE replay stream interrupted: {format_safe_api_error(exc)}",
                    retryable=True,
                ) from exc


def format_activity_log_row(event: dict[str, Any]) -> dict[str, Any]:
    """Format a WorkflowSSEEvent dict into a sanitized row for the live activity log panel."""
    ts = str(event.get("timestamp") or datetime.now().astimezone().isoformat())
    return {
        "Seq": event.get("sequence", "—"),
        "Timestamp": ts,
        "Stage": str(event.get("stage") or "—"),
        "Event Type": str(event.get("event_type") or "—"),
        "Status": str(event.get("status") or "—"),
        "Message": format_safe_api_error(event.get("message") or ""),
    }


__all__ = [
    "BACKEND_STAGE_TO_HIGH_LEVEL",
    "DEMO_SCENARIOS",
    "EVENT_TO_HIGH_LEVEL_STAGE",
    "HIGH_LEVEL_WORKFLOW_STEPS",
    "REGION_OPTIONS",
    "SCENARIO_OPTIONS",
    "SEVERITY_OPTIONS",
    "WEATHER_EVENT_OPTIONS",
    "RunApiClient",
    "RunApiError",
    "StreamlitUiConfig",
    "apply_sse_event_to_state",
    "build_api_url",
    "build_run_request",
    "build_workflow_stage_bar",
    "calculate_workflow_progress",
    "empty_authoritative_state",
    "format_activity_log_row",
    "format_bedrock_display_model",
    "format_evidence_bundle_summary",
    "format_safe_api_error",
    "is_approval_actionable",
    "iter_sse_events_from_lines",
    "load_ui_config",
    "map_event_to_high_level_stage",
    "normalize_api_run",
    "parse_sse_frame_lines",
    "redact_secrets",
    "should_accept_sse_event",
]
