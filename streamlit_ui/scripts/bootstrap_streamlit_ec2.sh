#!/usr/bin/env bash
set -euo pipefail

# Phase 13 EC2 Bootstrap (bootstrap_streamlit_ec2.sh):
# Reconstructs the complete streamlit_ui deployment folder and source code under
# $HOME/cohort3-network-resilience/streamlit_ui/ and runs the existing
# streamlit_ui/scripts/bootstrap_streamlit.sh environment setup.
#
# Does not start Streamlit or the backend service automatically; run
# streamlit_ui/scripts/streamlit.sh start afterwards.

PROJECT_DIR="$HOME/cohort3-network-resilience"

if [ ! -d "$PROJECT_DIR" ]; then
    echo "ERROR: $PROJECT_DIR does not exist. Run bootstrap_ec2.sh first." >&2
    exit 1
fi

STREAMLIT_DIR="$PROJECT_DIR/streamlit_ui"
SCRIPTS_DIR="$STREAMLIT_DIR/scripts"

mkdir -p "$SCRIPTS_DIR" "$STREAMLIT_DIR/logs" "$STREAMLIT_DIR/runtime"

# ------------------------------------------------------------
# 1. streamlit_ui/__init__.py
# ------------------------------------------------------------
cat > "$STREAMLIT_DIR/__init__.py" <<'PYEOF'
"""Streamlit Command Center UI package."""
PYEOF

# ------------------------------------------------------------
# 2. streamlit_ui/requirements.txt
# ------------------------------------------------------------
cat > "$STREAMLIT_DIR/requirements.txt" <<'REQEOF'
streamlit>=1.40.0
pandas>=2.2.0
pydeck>=0.9.0
requests>=2.32.0
REQEOF

# ------------------------------------------------------------
# 3. streamlit_ui/api_client.py (UI-safe API client; no credentials or secrets)
# ------------------------------------------------------------
cat > "$STREAMLIT_DIR/api_client.py" <<'PYEOF'
"""Command Center Backend HTTP/SSE API client for the Streamlit Command Center UI."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

REGION_OPTIONS: list[str] = [
    "Demo-Region-A",
    "Demo-Region-B",
    "Region-A",
    "Region-B",
    "Region-C",
]
SCENARIO_OPTIONS: list[str] = ["pause", "allow", "deny"]
SEVERITY_OPTIONS: list[str] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
WEATHER_EVENT_OPTIONS: list[str] = [
    "SEVERE_STORM",
    "HURRICANE",
    "FLOOD",
    "ICE_STORM",
    "HEATWAVE",
    "HIGH_WIND",
    "WILDFIRE",
]

SENSITIVE_PATTERNS = ("password", "secret", "token", "credential", "access_key")


class RunApiError(RuntimeError):
    """UI-safe error raised by the Command Center Backend API client."""


def load_ui_config() -> dict[str, str]:
    """Load configurable API base URL and data mode without embedding credentials."""
    return {
        "api_base_url": os.getenv("COMMAND_CENTER_API_BASE_URL", "http://127.0.0.1:8000"),
        "data_mode": os.getenv("COMMAND_CENTER_DATA_MODE", "aws_live"),
    }


def redact_secrets(text: str) -> str:
    lowered = text.lower()
    for word in SENSITIVE_PATTERNS:
        if word in lowered:
            return "[REDACTED_SENSITIVE_CONTENT]"
    return text[:400]


def build_run_request(
    *,
    scenario: str,
    weather_event: str,
    severity: str,
    affected_region: str,
    run_id: str,
    trace_id: str,
    approval_instruction: str = "NONE",
    approver_reference: str = "demo-approver",
) -> dict[str, Any]:
    """Build the POST /api/runs or /api/runs/stream request payload."""
    payload: dict[str, Any] = {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": run_id,
        "trace_id": trace_id,
        "data_source": "synthetic",
        "scenario": scenario,
        "weather_event": weather_event,
        "severity": severity,
        "affected_region": affected_region,
    }
    if approval_instruction and approval_instruction != "NONE":
        payload["approval_instruction"] = approval_instruction
        payload["approver_reference"] = approver_reference
    return payload


def _http_request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> tuple[int, dict[str, Any]]:
    req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlopen(request, timeout=timeout_seconds) as resp:
            status_code = int(resp.status)
            raw_text = resp.read().decode("utf-8")
    except Exception as exc:
        safe_msg = redact_secrets(str(exc))
        raise RunApiError(f"Command Center Backend request failed: {safe_msg}") from exc

    if not raw_text.strip():
        return status_code, {}
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RunApiError("Command Center Backend returned malformed JSON.") from exc
    if not isinstance(parsed, dict):
        raise RunApiError("Command Center Backend response root must be an object.")
    return status_code, parsed


def create_run(api_base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/runs -> complete CommandCenterView JSON response."""
    _, body = _http_request("POST", f"{api_base_url}/api/runs", body=payload)
    return body


def get_run(api_base_url: str, run_id: str) -> dict[str, Any]:
    """GET /api/runs/{run_id} -> enriched run state."""
    _, body = _http_request("GET", f"{api_base_url}/api/runs/{quote(run_id, safe='')}")
    return body


def approve_run(
    api_base_url: str,
    run_id: str,
    *,
    approver_reference: str = "demo-approver",
    comments: str | None = None,
) -> dict[str, Any]:
    """POST /api/runs/{run_id}/approve."""
    body_payload: dict[str, Any] = {"approver_reference": approver_reference}
    if comments:
        body_payload["comments"] = comments
    _, body = _http_request(
        "POST", f"{api_base_url}/api/runs/{quote(run_id, safe='')}/approve", body=body_payload
    )
    return body


def reject_run(
    api_base_url: str,
    run_id: str,
    *,
    approver_reference: str = "demo-approver",
    comments: str | None = None,
) -> dict[str, Any]:
    """POST /api/runs/{run_id}/reject."""
    body_payload: dict[str, Any] = {"approver_reference": approver_reference}
    if comments:
        body_payload["comments"] = comments
    _, body = _http_request(
        "POST", f"{api_base_url}/api/runs/{quote(run_id, safe='')}/reject", body=body_payload
    )
    return body


def get_evidence_bundle(
    api_base_url: str,
    run_id: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """GET /api/runs/{run_id}/evidence -> full sanitized evidence bundle."""
    url = f"{api_base_url}/api/runs/{quote(run_id, safe='')}/evidence"
    if trace_id:
        url = f"{url}?trace_id={quote(trace_id, safe='')}"
    _, body = _http_request("GET", url)
    return body


def empty_authoritative_state() -> dict[str, Any]:
    """Initial empty UI state; every display field comes strictly from the backend contract."""
    return {
        "run_id": "",
        "trace_id": "",
        "overall_status": "",
        "weather": {},
        "geo": {},
        "assets": {},
        "risk": {},
        "recommendation": {},
        "ai_reasoning": {},
        "governance": {},
        "approval": {},
        "execution": {},
        "verification": {},
        "workflow_stages": [],
        "evidence": [],
        "audit_trace": [],
        "evidence_bundle": {},
        "sse_events": [],
    }


def normalize_api_run(view: dict[str, Any]) -> dict[str, Any]:
    """Normalize an enriched backend run response into the authoritative UI state."""
    header = view.get("header", {})
    ai = view.get("ai_reasoning") or {}
    state = empty_authoritative_state()
    state.update(
        {
            "run_id": header.get("run_id", ""),
            "trace_id": header.get("trace_id", ""),
            "overall_status": view.get("overall_status", header.get("workflow_status", "")),
            "weather": view.get("weather", {}),
            "geo": view.get("geo", {}),
            "assets": view.get("assets", {}),
            "risk": view.get("risk", {}),
            "recommendation": view.get("recommendation", {}),
            "ai_reasoning": {
                "model_id": ai.get("model_id", ""),
                "result_status": ai.get("result_status", ""),
                "fallback_used": ai.get("fallback_used", True),
                "error_reason": ai.get("error_reason") or "",
                "situation_summary": (ai.get("reasoning") or {}).get(
                    "situation_summary", ""
                ),
                "risk_explanation": (ai.get("reasoning") or {}).get(
                    "risk_explanation", ""
                ),
                "recommendation_rationale": (ai.get("reasoning") or {}).get(
                    "recommendation_rationale", ""
                ),
                "operator_summary": (ai.get("reasoning") or {}).get("operator_summary", ""),
            },
            "governance": view.get("governance", {}),
            "approval": view.get("approval", {}),
            "execution": view.get("execution", {}),
            "verification": view.get("verification", {}),
            "workflow_stages": view.get("workflow_stages", []),
            "evidence": view.get("evidence", []),
            "audit_trace": view.get("audit_trace", []),
            "cloudwatch_event_counts": view.get("cloudwatch_event_counts", {}),
        }
    )
    return state


def should_accept_sse_event(event: dict[str, Any], run_id: str, trace_id: str) -> bool:
    """Accept only events matching the active run_id and trace_id (never unrelated runs)."""
    return bool(event.get("run_id")) and (
        event.get("run_id") == run_id and event.get("trace_id") == trace_id
    )


def apply_sse_event_to_state(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Apply a validated WorkflowSSEEvent to the authoritative UI state."""
    if not should_accept_sse_event(event, state.get("run_id", ""), state.get("trace_id", "")):
        return state

    state["sse_events"] = [*(state.get("sse_events") or []), event]
    event_data = event.get("data", {})
    event_type = str(event.get("event_type", ""))
    if event_type == "risk_assessed":
        state["risk"] = dict(event_data)
    elif event_type == "recommendation_generated":
        state["recommendation"] = dict(event_data)
    elif event_type == "governance_decided":
        state["governance"] = {
            "decision": event_data.get("decision"),
            "reason": event_data.get("reason", ""),
            "matched_policy_rules": event_data.get("matched_policy_rules", []),
        }
    elif event_type in {"approval_pending", "approval_approved", "approval_rejected"}:
        state["approval"] = dict(event_data)
    elif event_type in {"action_started", "action_executed", "action_blocked"}:
        state["execution"] = dict(event_data)
    elif event_type in {
        "verification_started",
        "verification_passed",
        "verification_blocked",
    }:
        state["verification"] = dict(event_data)
    elif event_type == "bedrock_reasoning_completed":
        state["ai_reasoning"].update(
            {
                "model_id": event_data.get("model_id", state["ai_reasoning"].get("model_id")),
                "result_status": event_data.get("result_status", ""),
                "fallback_used": event_data.get("fallback_used", True),
            }
        )
    if event.get("terminal"):
        state["overall_status"] = str(event_data.get("workflow_status", event.get("status", "")))
    return state


def build_workflow_stage_bar(state: dict[str, Any]) -> list[dict[str, str]]:
    """Map authoritative workflow_stages into UI pipeline labels: WEATHER -> GEO -> ASSET -> RISK -> ACTION -> VERIFY -> EVIDENCE."""
    labels = {
        "WEATHER": "WEATHER",
        "GEO_CORRELATION": "GEO",
        "ASSET_IMPACT": "ASSET",
        "RISK_ASSESSMENT": "RISK",
        "RECOMMENDATION": "RISK",
        "GOVERNANCE": "ACTION",
        "APPROVAL": "ACTION",
        "ACTION": "ACTION",
        "VERIFICATION": "VERIFY",
        "EVIDENCE": "EVIDENCE",
    }
    order = ["WEATHER", "GEO", "ASSET", "RISK", "ACTION", "VERIFY", "EVIDENCE"]
    mapped: dict[str, str] = {}
    for stage_state in state.get("workflow_stages", []):
        stage = str(stage_state.get("stage", ""))
        mapped[labels.get(stage, stage)] = str(stage_state.get("status", "PENDING"))

    return [
        {"label": label, "status": mapped.get(label, "PENDING")}
        for label in order
    ]


def calculate_workflow_progress(state: dict[str, Any]) -> float:
    stages = build_workflow_stage_bar(state)
    if not stages:
        return 0.0
    done = sum(1 for s in stages if s["status"] in {"COMPLETED", "PASSED"})
    return round(100.0 * done / len(stages), 1)


def format_activity_log_row(event: dict[str, Any]) -> dict[str, str]:
    """Format a WorkflowSSEEvent into a UI activity log row with redacted message text."""
    return {
        "sequence": str(event.get("sequence", "")),
        "event_type": str(event.get("event_type", "")),
        "stage": str(event.get("stage", "")),
        "status": str(event.get("status", "")),
        "recorded_at": str(event.get("timestamp", "")),
        "message": redact_secrets(str(event.get("message", ""))),
    }


def format_bedrock_display_model(ai_reasoning: dict[str, Any]) -> str:
    """Return a human-readable Bedrock explanation status string for the dashboard."""
    if not ai_reasoning:
        return "AI explanation unavailable"
    model_id = str(ai_reasoning.get("model_id", ""))
    fallback_used = bool(ai_reasoning.get("fallback_used", True))
    result_status = str(ai_reasoning.get("result_status", ""))
    if fallback_used:
        return f"{model_id or 'deterministic-fallback'} | status={result_status} | deterministic fallback"
    return f"{model_id} | status={result_status} | Bedrock explanation live"


def format_evidence_bundle_summary(bundle: dict[str, Any]) -> str:
    """Summarize the evidence bundle without exposing credentials or raw authorization data."""
    if not bundle:
        return "No evidence bundle captured yet."
    return (
        f"evidence_count={bundle.get('evidence_count', 0)} | "
        f"cloudwatch_events={len(bundle.get('cloudwatch_events') or [])} | "
        f"agent1_response={'present' if bundle.get('agent1_response') else 'missing'} | "
        f"agent2_response={'present' if bundle.get('agent2_response') else 'missing'}"
    )


def format_safe_api_error(exc: Exception) -> str:
    """Return a UI-safe error message with secrets and tokens redacted."""
    return redact_secrets(str(exc))
PYEOF

# ------------------------------------------------------------
# 4. streamlit_ui/scripts/streamlit.sh (start/stop/status/logs for Streamlit)
# ------------------------------------------------------------
cat > "$SCRIPTS_DIR/streamlit.sh" <<'SCRIPTEOF'
#!/usr/bin/env bash
set -euo pipefail

# Manage the Streamlit Command Center UI (start/stop/restart/status/logs/foreground).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAM_UI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$STREAM_UI_DIR/.." && pwd)"

PORT="${STREAMLIT_PORT:-8501}"
HOST="${STREAMLIT_HOST:-0.0.0.0}"
PID_FILE="$STREAM_UI_DIR/runtime/streamlit.pid"
LOG_FILE="$STREAM_UI_DIR/logs/streamlit.log"

cd "$REPO_ROOT"
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.venv/bin/activate"
fi

if [ -x "$STREAM_UI_DIR/../.venv/bin/streamlit" ]; then
    STREAMLIT_BIN="$STREAM_UI_DIR/../.venv/bin/streamlit"
else
    STREAMLIT_BIN="streamlit"
fi

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null || true)" 2>/dev/null
}

case "${1:-status}" in
    start)
        if is_running; then
            echo "Streamlit UI is already running (PID=$(cat "$PID_FILE"), http://$HOST:$PORT)."
            exit 0
        fi
        mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
        echo "Starting Streamlit Command Center UI on http://$HOST:$PORT ..."
        nohup "$STREAMLIT_BIN" run "$STREAM_UI_DIR/dashboard.py" \
            --server.port "$PORT" \
            --server.address "$HOST" \
            --server.headless true \
            >"$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        sleep 1
        if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "Streamlit UI started (PID=$(cat "$PID_FILE")). Logs: $LOG_FILE"
        else
            echo "ERROR: Streamlit UI failed to start. Log output:" >&2
            cat "$LOG_FILE" >&2 || true
            rm -f "$PID_FILE"
            exit 1
        fi
        ;;
    stop)
        if ! is_running; then
            echo "Streamlit UI is not running."
            rm -f "$PID_FILE"
            exit 0
        fi
        PID="$(cat "$PID_FILE")"
        echo "Stopping Streamlit UI (PID=$PID) ..."
        kill "$PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "Streamlit UI stopped."
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    status)
        if is_running; then
            echo "STATUS: RUNNING (PID=$(cat "$PID_FILE"), http://$HOST:$PORT)"
        else
            echo "STATUS: STOPPED"
            exit 1
        fi
        ;;
    logs)
        tail -n 50 -f "$LOG_FILE"
        ;;
    foreground)
        exec "$STREAMLIT_BIN" run "$STREAM_UI_DIR/dashboard.py" \
            --server.port "$PORT" --server.address "$HOST" --server.headless true
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|foreground}" >&2
        exit 1
        ;;
esac
SCRIPTEOF

# ------------------------------------------------------------
# 5. streamlit_ui/scripts/build_streamlit.sh (unit/compile validation)
# ------------------------------------------------------------
cat > "$SCRIPTS_DIR/build_streamlit.sh" <<'SCRIPTEOF'
#!/usr/bin/env bash
set -euo pipefail

# Compile-check the Streamlit UI source and run its API-client tests.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAM_UI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$STREAM_UI_DIR/.." && pwd)"

cd "$REPO_ROOT"
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.venv/bin/activate"
fi

echo "Compiling Streamlit UI modules ..."
python -m py_compile \
    "$STREAM_UI_DIR/api_client.py" \
    "$STREAM_UI_DIR/dashboard.py" \
    "$STREAM_UI_DIR/__init__.py"

echo "Running Streamlit API-client tests ..."
python -m pytest "$STREAM_UI_DIR/tests" -q
echo "BUILD_STREAMLIT=PASSED"
SCRIPTEOF

# ------------------------------------------------------------
# 6. streamlit_ui/scripts/deploy_streamlit.sh (frontend-only deploy helper)
# ------------------------------------------------------------
cat > "$SCRIPTS_DIR/deploy_streamlit.sh" <<'SCRIPTEOF'
#!/usr/bin/env bash
set -euo pipefail

# Bootstrap + build + start the Streamlit Command Center UI in one step.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/3] Bootstrapping Streamlit virtual environment ..."
bash "$SCRIPT_DIR/bootstrap_streamlit.sh"

echo "[2/3] Building and validating Streamlit UI ..."
bash "$SCRIPT_DIR/build_streamlit.sh"

echo "[3/3] Starting Streamlit Command Center UI ..."
bash "$SCRIPT_DIR/streamlit.sh" start
SCRIPTEOF

# ------------------------------------------------------------
# 7. streamlit_ui/tests/test_api_client.py (UI-safe unit tests; no live AWS)
# ------------------------------------------------------------
mkdir -p "$STREAMLIT_DIR/tests"
cat > "$STREAMLIT_DIR/tests/__init__.py" <<'PYEOF'
"""Streamlit UI tests."""
PYEOF

cat > "$STREAMLIT_DIR/tests/test_api_client.py" <<'PYEOF'
"""Unit tests for the Streamlit Command Center UI API client and state mappers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_client import (  # noqa: E402
    build_run_request,
    empty_authoritative_state,
    format_bedrock_display_model,
    format_safe_api_error,
    normalize_api_run,
    redact_secrets,
    should_accept_sse_event,
)


def test_build_run_request_has_no_credentials() -> None:
    payload = build_run_request(
        scenario="pause",
        weather_event="SEVERE_STORM",
        severity="HIGH",
        affected_region="Demo-Region-A",
        run_id="WX-UI-1",
        trace_id="TRACE-UI-1",
    )
    assert payload["run_id"] == "WX-UI-1"
    assert payload["trace_id"] == "TRACE-UI-1"
    assert payload["scenario"] == "pause"
    assert "approval_instruction" not in payload


def test_redact_secrets_removes_token_and_credentials() -> None:
    assert "SECRET_TOKEN_999" not in redact_secrets("aws_session_token=SECRET_TOKEN_999")
    assert redact_secrets("session_token=SECRET_TOKEN_999") == "[REDACTED_SENSITIVE_CONTENT]"
    assert redact_secrets("safe message stays") == "safe message stays"


def test_normalize_api_run_preserves_backend_state() -> None:
    view = {
        "header": {
            "run_id": "WX-1",
            "trace_id": "TRACE-1",
            "overall_risk": "CRITICAL",
            "workflow_status": "PAUSED_AWAITING_HUMAN_APPROVAL",
        },
        "weather": {"event": "SEVERE_STORM", "severity": "HIGH"},
        "risk": {"risk_score": 85, "overall_risk": "CRITICAL"},
        "workflow_stages": [
            {"stage": "WEATHER", "status": "COMPLETED"},
        ],
    }
    state = normalize_api_run(view)
    assert state["run_id"] == "WX-1"
    assert state["trace_id"] == "TRACE-1"
    assert state["risk"]["risk_score"] == 85


def test_should_accept_sse_event_filters_other_runs() -> None:
    event = {"run_id": "WX-1", "trace_id": "TRACE-1"}
    assert should_accept_sse_event(event, "WX-1", "TRACE-1") is True
    assert should_accept_sse_event(event, "WX-OTHER", "TRACE-OTHER") is False


def test_empty_authoritative_state_has_all_sections() -> None:
    state = empty_authoritative_state()
    for key in (
        "run_id",
        "trace_id",
        "weather",
        "geo",
        "assets",
        "risk",
        "recommendation",
        "ai_reasoning",
        "governance",
        "approval",
        "execution",
        "verification",
        "workflow_stages",
        "evidence",
    ):
        assert key in state


def test_format_bedrock_display_model_and_safe_error() -> None:
    live = format_bedrock_display_model(
        {"model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "fallback_used": False, "result_status": "SUCCEEDED"}
    )
    assert "Bedrock explanation live" in live
    fallback = format_bedrock_display_model({"model_id": "x", "fallback_used": True})
    assert "deterministic fallback" in fallback
    assert "SECRET" not in format_safe_api_error(RuntimeError("aws_session_token=SECRET_X"))
PYEOF

# ------------------------------------------------------------
# 8. streamlit_ui/README.md
# ------------------------------------------------------------
cat > "$STREAMLIT_DIR/README.md" <<'MDEOF'
# Streamlit Command Center UI

Presentation-only dashboard for the Network Resilience Command Center Backend API.

- Risk, governance, approval, execution, and verification states are read from the backend.
- The UI never calculates risk or overrides governance decisions.
- All data comes from `POST /api/runs`, `GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/approve`,
  `POST /api/runs/{run_id}/reject`, and `GET /api/runs/{run_id}/evidence`.

## Quick Start on EC2

```bash
cd ~/cohort3-network-resilience
./streamlit_ui/scripts/deploy_streamlit.sh

# Then open http://<EC2_PUBLIC_IP>:8501
```

## Environment Variables

- `COMMAND_CENTER_API_BASE_URL=http://127.0.0.1:8000`
- `COMMAND_CENTER_DATA_MODE=aws_live`
- `STREAMLIT_HOST=0.0.0.0`
- `STREAMLIT_PORT=8501`
MDEOF

# ------------------------------------------------------------
# 9. Run the existing bootstrap_streamlit.sh (venv + deps + no-raw-JSON check)
# ------------------------------------------------------------
chmod +x "$SCRIPTS_DIR/bootstrap_streamlit.sh" "$SCRIPTS_DIR/build_streamlit.sh" "$SCRIPTS_DIR/deploy_streamlit.sh" "$SCRIPTS_DIR/streamlit.sh"

bash "$SCRIPTS_DIR/bootstrap_streamlit.sh"

# ------------------------------------------------------------
# 10. Validate API client tests and compile dashboard.py
# ------------------------------------------------------------
bash "$SCRIPTS_DIR/build_streamlit.sh"

echo "============================================================"
echo "BOOTSTRAP_STREAMLIT_EC2=PASSED"
echo "Streamlit UI directory: $STREAMLIT_DIR"
echo "Start the dashboard with: $SCRIPTS_DIR/streamlit.sh start"
echo "============================================================"
