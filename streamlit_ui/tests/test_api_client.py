"""Unit tests for the Streamlit Command Center UI API client and state mappers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_client import (
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
        {
            "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "fallback_used": False,
            "result_status": "SUCCEEDED",
        }
    )
    assert "Bedrock explanation live" in live
    fallback = format_bedrock_display_model({"model_id": "x", "fallback_used": True})
    assert "deterministic fallback" in fallback
    assert "SECRET" not in format_safe_api_error(RuntimeError("aws_session_token=SECRET_X"))
