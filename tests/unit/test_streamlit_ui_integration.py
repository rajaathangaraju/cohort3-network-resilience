"""Unit tests for the Streamlit Command Center API client and view-mapping helpers."""

from collections.abc import Iterator
from typing import Any

import pytest
import requests

from command_center.backend import CommandCenterBackendService
from command_center.backend.sse import format_heartbeat_comment, format_sse_frame
from contracts.governance_decision import ApprovalStatus
from streamlit_ui.api_client import (
    RunApiClient,
    RunApiError,
    apply_sse_event_to_state,
    build_api_url,
    build_run_request,
    build_workflow_stage_bar,
    calculate_workflow_progress,
    empty_authoritative_state,
    format_bedrock_display_model,
    format_evidence_bundle_summary,
    format_safe_api_error,
    is_approval_actionable,
    iter_sse_events_from_lines,
    load_ui_config,
    map_event_to_high_level_stage,
    normalize_api_run,
    parse_sse_frame_lines,
    redact_secrets,
    should_accept_sse_event,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        json_body: Any = None,
        lines: list[str] | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self._lines = lines or []
        self.headers = {"Content-Type": content_type}
        self.content = (
            b"{}" if json_body is not None else b"".join(line.encode() for line in self._lines)
        )
        self.text = str(json_body)

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        del decode_unicode
        yield from self._lines

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeSession:
    def __init__(self, responses: dict[tuple[str, str], FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def _respond(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
        self.calls.append({"method": method, "path": "/" + path, **kwargs})
        result = self.responses[(method, "/" + path)]
        if isinstance(result, Exception):
            raise result
        return result

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond(method, url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond("GET", url, **kwargs)


def make_client(
    responses: dict[tuple[str, str], FakeResponse | Exception],
) -> tuple[RunApiClient, FakeSession]:
    session = FakeSession(responses)
    session_any: Any = session
    client = RunApiClient("http://backend.test:8000/", session=session_any)
    return client, session


def backend_wire_events(
    svc: CommandCenterBackendService, request: dict[str, Any]
) -> list[dict[str, Any]]:
    wire = format_heartbeat_comment() + "".join(
        format_sse_frame(ev) for ev in svc.stream_workflow(request)
    )
    return list(iter_sse_events_from_lines(wire.splitlines()))


def fold_events(
    events: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
    seen: set[str] | None = None,
    last_seq: int = 0,
) -> tuple[dict[str, Any], set[str], int, list[dict[str, Any]]]:
    current = state or empty_authoritative_state()
    seen_ids = seen if seen is not None else set()
    accepted: list[dict[str, Any]] = []
    for event in events:
        ok, last_seq = should_accept_sse_event(
            event, seen_event_ids=seen_ids, last_sequence=last_seq
        )
        if ok:
            accepted.append(event)
            current = apply_sse_event_to_state(current, event)
    return current, seen_ids, last_seq, accepted


# --- Configuration and URL construction ---------------------------------------------------


def test_load_ui_config_defaults() -> None:
    cfg = load_ui_config({})
    assert cfg.api_base_url == "http://127.0.0.1:8000"
    assert cfg.data_mode == "aws_live"
    assert cfg.sse_connect_timeout_seconds == 10.0
    assert cfg.sse_read_timeout_seconds == 300.0
    assert cfg.sse_reconnect_delay_seconds == 2.0


def test_load_ui_config_precedence_and_invalid_values() -> None:
    cfg = load_ui_config(
        {
            "COMMAND_CENTER_API_BASE_URL": "http://cc.internal:9000/",
            "NETWORK_RESILIENCE_API_URL": "http://legacy:8000",
            "COMMAND_CENTER_DATA_MODE": " MOCK ",
            "SSE_CONNECT_TIMEOUT_SECONDS": "abc",
            "SSE_READ_TIMEOUT_SECONDS": "-5",
            "SSE_RECONNECT_DELAY_SECONDS": "0.5",
        }
    )
    assert cfg.api_base_url == "http://cc.internal:9000"
    assert cfg.data_mode == "mock"
    assert cfg.sse_connect_timeout_seconds == 10.0
    assert cfg.sse_read_timeout_seconds == 300.0
    assert cfg.sse_reconnect_delay_seconds == 0.5


def test_load_ui_config_legacy_fallback() -> None:
    assert load_ui_config({"NETWORK_RESILIENCE_API_URL": "http://legacy:8000"}).api_base_url == (
        "http://legacy:8000"
    )


def test_build_api_url() -> None:
    assert build_api_url("http://h:8000/", "/api/runs/WX-1") == "http://h:8000/api/runs/WX-1"
    assert (
        build_api_url("http://h:8000", "api/runs/WX-1/evidence", {"trace_id": "T1", "x": ""})
        == "http://h:8000/api/runs/WX-1/evidence?trace_id=T1"
    )


def test_build_run_request_scenario_vs_custom() -> None:
    assert build_run_request(
        "PAUSE", weather_event="FLOOD", severity="low", affected_region="Region-B"
    ) == {"scenario": "pause"}
    assert build_run_request(
        "custom", weather_event="flood", severity="low", affected_region=" Region-B "
    ) == {"weather_event": "FLOOD", "severity": "LOW", "affected_region": "Region-B"}


# --- Health check and HTTP client -----------------------------------------------------------


def test_health_check_ok() -> None:
    body = {"status": "ok", "data_mode": "mock", "aws_region": "us-east-2"}
    client, session = make_client({("GET", "/health"): FakeResponse(json_body=body)})
    assert client.health_check() == body
    assert session.calls[0]["headers"]["Accept"] == "application/json"


def test_health_check_falls_back_to_openapi() -> None:
    client, _ = make_client(
        {
            ("GET", "/health"): FakeResponse(404, json_body={"detail": "Not Found"}),
            ("GET", "/openapi.json"): FakeResponse(json_body={"info": {"title": "CC API"}}),
        }
    )
    assert client.health_check() == {"status": "ok", "service": "CC API"}


def test_health_check_unavailable_is_retryable() -> None:
    client, _ = make_client({("GET", "/health"): requests.ConnectionError("refused")})
    with pytest.raises(RunApiError) as err:
        client.health_check()
    assert err.value.retryable is True
    assert "unavailable" in err.value.safe_message


def test_approve_and_reject_send_synthetic_approver_and_idempotency_key() -> None:
    client, session = make_client(
        {
            ("POST", "/api/runs/WX-1/approve"): FakeResponse(json_body={"run_id": "WX-1"}),
            ("POST", "/api/runs/WX-1/reject"): FakeResponse(json_body={"run_id": "WX-1"}),
        }
    )
    client.approve_run("WX-1")
    client.reject_run("WX-1", comments="Unsafe window")
    approve_call, reject_call = session.calls
    assert approve_call["json"] == {"approver_reference": "demo-approver"}
    assert reject_call["json"] == {
        "approver_reference": "demo-approver",
        "comments": "Unsafe window",
    }
    assert approve_call["headers"]["X-Idempotency-Key"]


def test_conflict_error_is_surfaced_safely() -> None:
    client, _ = make_client(
        {
            ("POST", "/api/runs/WX-1/approve"): FakeResponse(
                409, json_body={"message": "Run 'WX-1' must be PAUSE.\nTraceback ..."}
            )
        }
    )
    with pytest.raises(RunApiError) as err:
        client.approve_run("WX-1")
    assert err.value.status_code == 409
    assert "Traceback" not in err.value.safe_message


def test_state_changing_posts_are_never_auto_retried() -> None:
    adapter: Any = RunApiClient._build_session().get_adapter("http://x")
    retry = adapter.max_retries
    assert "POST" not in retry.allowed_methods
    assert "GET" in retry.allowed_methods


def test_stream_run_and_replay_headers() -> None:
    frame = [
        "id: EVT-1",
        "event: connected",
        'data: {"event_id": "EVT-1", "event_type": "connected", "sequence": 1, "run_id": "WX-1"}',
        "",
    ]
    client, session = make_client(
        {
            ("POST", "/api/runs/stream"): FakeResponse(
                lines=[": heartbeat", "", *frame], content_type="text/event-stream"
            ),
            ("GET", "/api/runs/WX-1/stream"): FakeResponse(
                lines=frame, content_type="text/event-stream; charset=utf-8"
            ),
        }
    )
    assert [e["event_type"] for e in client.stream_run({"scenario": "pause"})] == ["connected"]
    assert [e["event_id"] for e in client.replay_run_stream("WX-1", last_event_id="EVT-0")] == [
        "EVT-1"
    ]
    assert session.calls[0]["headers"]["Accept"] == "text/event-stream"
    assert session.calls[0]["stream"] is True
    assert session.calls[1]["headers"]["Last-Event-ID"] == "EVT-0"


def test_stream_run_rejects_non_sse_content_type() -> None:
    client, _ = make_client(
        {("POST", "/api/runs/stream"): FakeResponse(json_body={"status": "ok"})}
    )
    with pytest.raises(RunApiError, match="text/event-stream"):
        list(client.stream_run({"scenario": "pause"}))


# --- SSE parsing, dedup, ordering -------------------------------------------------------------


def test_parse_sse_frame_with_multiline_data_and_retry() -> None:
    event = parse_sse_frame_lines(
        [
            "id: EVT-9",
            "event: risk_assessed",
            "retry: 2000",
            'data: {"sequence": 9,',
            'data: "x": 1}',
        ]
    )
    assert event == {
        "sequence": 9,
        "x": 1,
        "event_id": "EVT-9",
        "event_type": "risk_assessed",
        "retry_ms": 2000,
    }


def test_heartbeat_and_empty_frames_are_ignored() -> None:
    assert parse_sse_frame_lines([": heartbeat"]) is None
    assert parse_sse_frame_lines([]) is None
    raw = [b": heartbeat\n", b"\n", b'data: {"event_id": "E1"}\n', b"\n"]
    assert list(iter_sse_events_from_lines(raw)) == [{"event_id": "E1"}]


def test_malformed_sse_payload_raises() -> None:
    with pytest.raises(RunApiError, match="Malformed"):
        parse_sse_frame_lines(["data: {not-json"])
    with pytest.raises(RunApiError, match="JSON object"):
        parse_sse_frame_lines(["data: [1, 2]"])


def test_should_accept_dedup_sequence_and_correlation() -> None:
    seen: set[str] = set()
    ok, seq = should_accept_sse_event(
        {"event_id": "E1", "sequence": 1, "run_id": "WX-1", "trace_id": "T-1"},
        seen_event_ids=seen,
        last_sequence=0,
    )
    assert (ok, seq) == (True, 1)
    assert should_accept_sse_event(
        {"event_id": "E1", "sequence": 2}, seen_event_ids=seen, last_sequence=1
    ) == (
        False,
        1,
    )
    assert should_accept_sse_event(
        {"event_id": "E0", "sequence": 1}, seen_event_ids=seen, last_sequence=1
    ) == (
        False,
        1,
    )
    assert not should_accept_sse_event(
        {"event_id": "E2", "sequence": 2, "run_id": "WX-OTHER"},
        seen_event_ids=seen,
        last_sequence=1,
        expected_run_id="WX-1",
    )[0]
    assert not should_accept_sse_event(
        {"event_id": "E3", "sequence": 2, "trace_id": "T-OTHER"},
        seen_event_ids=seen,
        last_sequence=1,
        expected_trace_id="T-1",
    )[0]
    assert seen == {"E1"}


@pytest.mark.parametrize(
    ("event_type", "stage", "expected"),
    [
        ("weather_received", None, "WEATHER"),
        ("geo_correlation_completed", None, "GEO"),
        ("inventory_enriched", None, "ASSET"),
        ("risk_assessed", None, "RISK"),
        ("governance_decided", None, "RISK"),
        ("approval_pending", None, "ACTION"),
        ("action_executed", None, "ACTION"),
        ("verification_passed", None, "VERIFY"),
        ("evidence_captured", None, "EVIDENCE"),
        ("run_completed", None, "EVIDENCE"),
        ("unknown_event", "GEO_CORRELATION", "GEO"),
        ("unknown_event", "VERIFICATION", "VERIFY"),
    ],
)
def test_event_to_seven_stage_mapping(event_type: str, stage: str | None, expected: str) -> None:
    assert map_event_to_high_level_stage(event_type, stage) == expected


# --- Governance state mappings from real backend SSE frames --------------------------------


@pytest.fixture
def backend() -> CommandCenterBackendService:
    return CommandCenterBackendService(data_mode="mock")


def stage_statuses(state: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, str]:
    bar = build_workflow_stage_bar(
        workflow_stages=state.get("workflow_stages"),
        sse_events=events,
        governance_decision=state.get("policy_decision"),
        approval_status=state.get("approval"),
        execution_status=state.get("execution"),
        verification_status=state.get("verification"),
    )
    return {item["stage"]: item["status"] for item in bar}


def test_deny_scenario_mapping(backend: CommandCenterBackendService) -> None:
    events = backend_wire_events(backend, {"scenario": "deny"})
    state, _, _, accepted = fold_events(events)
    assert (state["policy_decision"], state["execution"], state["verification"]) == (
        "DENY",
        "BLOCKED",
        "BLOCKED",
    )
    assert not is_approval_actionable(state["policy_decision"], state["approval"])
    statuses = stage_statuses(state, accepted)
    assert statuses["ACTION"] == "BLOCKED"
    ratio, label = calculate_workflow_progress(
        [{"stage": k, "status": v} for k, v in statuses.items()],
        workflow_status=state["status"],
        governance_decision=state["policy_decision"],
        approval_status=state["approval"],
    )
    assert ratio < 1.0
    assert "BLOCKED" in label


def test_allow_scenario_mapping(backend: CommandCenterBackendService) -> None:
    events = backend_wire_events(backend, {"scenario": "allow"})
    state, _, _, accepted = fold_events(events)
    assert state["policy_decision"] == "ALLOW"
    assert state["approval"] == "NOT_REQUIRED"
    assert state["execution"] == "EXECUTED"
    assert state["verification"] == "PASSED"
    statuses = stage_statuses(state, accepted)
    assert statuses["ACTION"] == "COMPLETED"
    assert statuses["VERIFY"] == "COMPLETED"
    assert statuses["EVIDENCE"] == "COMPLETED"


def test_pause_scenario_mapping_preserves_ids_and_never_completes(
    backend: CommandCenterBackendService,
) -> None:
    events = backend_wire_events(backend, {"scenario": "pause"})
    run_ids = {e["run_id"] for e in events}
    trace_ids = {e["trace_id"] for e in events}
    assert len(run_ids) == 1
    assert len(trace_ids) == 1
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))

    state, _, _, accepted = fold_events(events)
    assert state["run_id"] == next(iter(run_ids))
    assert state["trace_id"] == next(iter(trace_ids))
    assert state["status"] == "AWAITING_APPROVAL"
    assert is_approval_actionable(state["policy_decision"], state["approval"])
    statuses = stage_statuses(state, accepted)
    assert statuses["ACTION"] == "PAUSED"
    ratio, label = calculate_workflow_progress(
        [{"stage": k, "status": v} for k, v in statuses.items()],
        workflow_status=state["status"],
        governance_decision=state["policy_decision"],
        approval_status=state["approval"],
    )
    assert ratio < 1.0
    assert "PAUSED" in label


@pytest.mark.parametrize(
    ("decision", "approval", "execution", "verification"),
    [
        (ApprovalStatus.APPROVED, "APPROVED", "EXECUTED", "PASSED"),
        (ApprovalStatus.REJECTED, "REJECTED", "BLOCKED", "BLOCKED"),
    ],
)
def test_pause_then_decision_replay_mapping(
    backend: CommandCenterBackendService,
    decision: ApprovalStatus,
    approval: str,
    execution: str,
    verification: str,
) -> None:
    events = backend_wire_events(backend, {"scenario": "pause"})
    state, seen, last_seq, accepted = fold_events(events)
    run_id = state["run_id"]
    last_event_id = accepted[-1]["event_id"]

    backend.submit_approval(
        run_id=run_id,
        decision=decision,
        approver_reference="demo-approver",
        comments="test",
        strict_pending_check=True,
    )
    replay_wire = "".join(
        format_sse_frame(ev)
        for ev in backend.replay_stream_events(run_id, last_event_id=last_event_id)
    )
    replayed = list(iter_sse_events_from_lines(replay_wire.splitlines()))
    assert replayed
    assert all(e["sequence"] > last_seq for e in replayed)

    state, _, _, new_events = fold_events(replayed, state, seen, last_seq)
    assert (state["approval"], state["execution"], state["verification"]) == (
        approval,
        execution,
        verification,
    )
    assert not is_approval_actionable(state["policy_decision"], state["approval"])

    snapshot = normalize_api_run(backend.get_enriched_run_response(run_id))
    assert snapshot["run_id"] == run_id
    assert snapshot["trace_id"] == state["trace_id"]
    assert snapshot["policy_decision"] == "PAUSE"
    assert (snapshot["approval"], snapshot["execution"], snapshot["verification"]) == (
        approval,
        execution,
        verification,
    )
    statuses = stage_statuses(snapshot, accepted + new_events)
    assert statuses["ACTION"] == ("COMPLETED" if decision == ApprovalStatus.APPROVED else "BLOCKED")


def test_normalize_api_run_reads_backend_values_only(backend: CommandCenterBackendService) -> None:
    backend_wire_events(backend, {"scenario": "pause"})
    run_id = next(iter(backend._agent2_results))
    payload = backend.get_enriched_run_response(run_id)
    view = normalize_api_run(payload)
    assert view["overall_risk"] == payload["header"]["overall_risk"]
    assert view["risk_assessment"]["risk_score"] == payload["risk"]["risk_score"]
    assert view["policy_decision"] == payload["status_card"]["policy_decision"]
    assert view["agent1_result"] == payload["agent1_result"]
    assert view["agent2_result"] == payload["agent2_result"]
    assert normalize_api_run({})["run_id"] == "—"


def test_is_approval_actionable_truth_table() -> None:
    assert is_approval_actionable("PAUSE", "PENDING")
    assert is_approval_actionable("pause", "pending")
    assert not is_approval_actionable("PAUSE", "APPROVED")
    assert not is_approval_actionable("PAUSE", "REJECTED")
    assert not is_approval_actionable("ALLOW", "NOT_REQUIRED")
    assert not is_approval_actionable("DENY", "PENDING")
    assert not is_approval_actionable(None, None)


def test_run_failed_event_sets_sanitized_error() -> None:
    state = apply_sse_event_to_state(
        empty_authoritative_state(),
        {
            "event_type": "run_failed",
            "run_id": "WX-F",
            "data": {"safe_message": "Agent 2 failed with key AKIAABCDEFGHIJKLMNOP"},
        },
    )
    assert state["status"] == "FAILED"
    assert "AKIA" not in state["stream_error"]
    assert state["run_id"] == "WX-F"


# --- Bedrock and evidence display models ----------------------------------------------------


def test_bedrock_display_model_fallback_and_live() -> None:
    reasoning = {
        "situation_summary": "S",
        "risk_explanation": "R",
        "recommendation_rationale": "RR",
        "operator_summary": "O",
    }
    fallback = format_bedrock_display_model(
        {
            "model_id": "deterministic-fallback",
            "result_status": "FALLBACK_USED",
            "fallback_used": True,
            "error_reason": "AccessDenied",
            "reasoning": reasoning,
        }
    )
    assert fallback["fallback_used"] is True
    assert fallback["banner_message"] == "Deterministic fallback explanation used."
    assert fallback["error_reason"] == "AccessDenied"
    assert fallback["operator_summary"] == "O"

    live = format_bedrock_display_model(
        {
            "model_id": "us.anthropic.claude",
            "result_status": "SUCCEEDED",
            "fallback_used": False,
            "reasoning": reasoning,
        }
    )
    assert live["fallback_used"] is False
    assert "us.anthropic.claude" in live["banner_message"]
    assert format_bedrock_display_model(None)["available"] is False


def test_evidence_bundle_summary_counts_cloudwatch_by_agent() -> None:
    bundle = {
        "evidence_count": 3,
        "evidence": [{}, {}, {}],
        "normalized_evidence": [
            {"evidence_id": "EVD-1", "stage": "WEATHER", "status": "COMPLETED", "summary": "ok"},
            {"evidence_id": "EVD-2", "stage": "RISK", "status": "COMPLETED", "summary": "ok"},
        ],
        "cloudwatch_events": [
            {"logGroupName": "/aws/bedrock-agentcore/runtimes/weather_network_intelligence-x"},
            {"logGroupName": "/aws/bedrock-agentcore/runtimes/network_resilience-y"},
            {"logGroupName": "/other", "message": '{"agent_name": "network_resilience"}'},
        ],
    }
    summary = format_evidence_bundle_summary(bundle)
    assert summary["app_evidence_count"] == 3
    assert summary["normalized_evidence_count"] == 2
    assert summary["cloudwatch_total_count"] == 3
    assert summary["cloudwatch_agent1_count"] == 1
    assert summary["cloudwatch_agent2_count"] == 2
    assert summary["rows"][0]["Evidence ID"] == "EVD-1"


def test_evidence_bundle_summary_uses_fallback_counts() -> None:
    summary = format_evidence_bundle_summary(
        {},
        fallback_evidence=[{"evidence_id": "E1"}],
        fallback_cw_counts={"total": 4, "agent1": 1, "agent2": 3},
    )
    assert summary["app_evidence_count"] == 1
    assert (
        summary["cloudwatch_total_count"],
        summary["cloudwatch_agent1_count"],
        summary["cloudwatch_agent2_count"],
    ) == (4, 1, 3)


def test_backend_evidence_bundle_maps(backend: CommandCenterBackendService) -> None:
    backend_wire_events(backend, {"scenario": "allow"})
    run_id = next(iter(backend._agent2_results))
    summary = format_evidence_bundle_summary(backend.get_evidence_bundle(run_id=run_id))
    assert summary["app_evidence_count"] > 0
    assert summary["normalized_evidence_count"] > 0
    assert summary["cloudwatch_total_count"] == 0


# --- Error sanitization -------------------------------------------------------------------


def test_format_safe_api_error_strips_traceback_and_secrets() -> None:
    raw = (
        "Backend failed: aws_secret_access_key=abc123 Authorization: Bearer-token\n"
        "Traceback (most recent call last):\n  File x.py"
    )
    safe = format_safe_api_error(raw)
    assert "Traceback" not in safe
    assert "abc123" not in safe
    assert "[REDACTED]" in safe
    assert format_safe_api_error("key AKIAABCDEFGHIJKLMNOP") == "key [REDACTED]"
    assert format_safe_api_error(None) == "Command Center backend request failed."
    assert len(format_safe_api_error("x" * 1000)) == 360


def test_redact_secrets_recurses_without_truncation() -> None:
    nested = {
        "a": ["ASIAABCDEFGHIJKLMNOP", {"b": "x-api-key: secret123"}],
        "n": 5,
        "multi": "line1\nline2",
    }
    redacted = redact_secrets(nested)
    assert redacted["a"][0] == "[REDACTED]"
    assert "secret123" not in redacted["a"][1]["b"]
    assert redacted["n"] == 5
    assert redacted["multi"] == "line1\nline2"
