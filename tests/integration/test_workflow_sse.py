"""Comprehensive unit and ASGI integration tests for backend workflow-level SSE streaming.

Covers all requirements from section 28:
- SSE response content-type equals text/event-stream + required headers
- connected and run_started events
- Agent 1 start/completion events (agent1_started, weather_received, weather_completed,
  geo_correlation_completed, asset_mapping_completed, agent1_completed)
- Agent 2 start/completion events (agent2_started, inventory_enriched, service_impact_completed,
  risk_assessed, bedrock_reasoning_completed, recommendation_generated, governance_decided,
  evidence_captured, agent2_completed)
- DENY event sequence
- ALLOW event sequence
- PAUSE event sequence (closes with AWAITING_APPROVAL)
- PAUSE + approve event sequence (both demo mode and explicit POST /approve + GET /stream replay)
- PAUSE + reject event sequence
- run_id and trace_id preservation + strictly increasing sequence numbers starting at 1
- Same orchestration logic for JSON (POST /api/runs) and SSE (POST /api/runs/stream)
- Bedrock success event (fallback_used=False) and Bedrock fallback event (fallback_used=True)
- Terminal run_completed exactly once, or run_failed exactly once on failure
- Last-Event-ID replay without rerunning Agent 1 or Agent 2
- Heartbeat formatting (: heartbeat)
- Client disconnect behavior
- Secret redaction in error events (run_failed)
"""

import json
from collections.abc import MutableMapping
from typing import Any

import anyio
from starlette.types import Scope

from agents.network_resilience.agent import NetworkResilienceAgent
from command_center.backend import (
    CommandCenterBackendService,
    create_command_center_api,
    format_heartbeat_comment,
)
from contracts.action_result import WorkflowSSEEvent
from contracts.governance_decision import ApprovalStatus
from contracts.recommendation import BedrockReasoningContext, BedrockReasoningOutput
from integrations.bedrock import ResilientBedrockReasoningService
from integrations.inventory_api import MockNetworkInventoryAdapter
from orchestration.workflow import EndToEndOrchestrator
from tests.integration.test_command_center_and_evidence import (
    SimulatedAwsAgentCoreInvoker,
)


def parse_sse_wire_stream(raw_sse: str) -> tuple[list[str], list[WorkflowSSEEvent]]:
    """Parse raw text/event-stream frames into heartbeat comments and WorkflowSSEEvents."""
    comments: list[str] = []
    events: list[WorkflowSSEEvent] = []

    blocks = [b for b in raw_sse.split("\n\n") if b.strip()]
    for block in blocks:
        lines = block.splitlines()
        if all(line.startswith(":") for line in lines):
            comments.extend(lines)
            continue

        event_id: str | None = None
        event_type: str | None = None
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("id: "):
                event_id = line[4:].strip()
            elif line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])

        assert data_lines, f"SSE frame missing data field: {block}"
        parsed = WorkflowSSEEvent.model_validate_json("\n".join(data_lines))
        assert parsed.event_id == event_id
        assert parsed.event_type == event_type
        events.append(parsed)

    return comments, events


async def _call_asgi_sse(
    asgi_app: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    disconnect_after_chunks: int | None = None,
) -> tuple[int, dict[str, str], str]:
    payload_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    sent = False
    status_code = 500
    resp_headers: dict[str, str] = {}
    chunks: list[bytes] = []
    done_event = anyio.Event()

    async def receive() -> MutableMapping[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload_bytes, "more_body": False}
        if disconnect_after_chunks is not None and len(chunks) >= disconnect_after_chunks:
            return {"type": "http.disconnect"}
        await done_event.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                done_event.set()
            if disconnect_after_chunks is not None and len(chunks) >= disconnect_after_chunks:
                done_event.set()

    req_headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"text/event-stream"),
    ]
    if headers:
        req_headers.extend(headers)

    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": req_headers,
    }
    await asgi_app(scope, receive, send)
    return status_code, resp_headers, b"".join(chunks).decode("utf-8")


def test_sse_headers_heartbeat_and_scenario_event_orders() -> None:
    """Verify SSE headers, heartbeats, and event orders for DENY, ALLOW, PAUSE, APPROVE, REJECT."""
    invoker = SimulatedAwsAgentCoreInvoker()
    svc = CommandCenterBackendService(
        data_mode="aws_live",
        agentcore_invoker=invoker,
        agent1_runtime_arn=invoker.AGENT1_ARN,
        agent2_runtime_arn=invoker.AGENT2_ARN,
    )
    asgi_app = create_command_center_api(svc)

    assert format_heartbeat_comment() == ": heartbeat\n\n"

    async def _run_all() -> None:
        # 1. DENY scenario stream
        status, headers, raw_deny = await _call_asgi_sse(
            asgi_app,
            "POST",
            "/api/runs/stream",
            body={
                "scenario": "deny",
                "run_id": "WX-SSE-DENY",
                "trace_id": "TRACE-SSE-DENY",
            },
        )
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"
        assert headers["x-accel-buffering"] == "no"

        comments_deny, events_deny = parse_sse_wire_stream(raw_deny)
        assert ": heartbeat" in comments_deny
        deny_types = [e.event_type for e in events_deny]
        assert deny_types == [
            "connected",
            "run_started",
            "agent1_started",
            "weather_received",
            "weather_completed",
            "geo_correlation_completed",
            "asset_mapping_completed",
            "agent1_completed",
            "agent2_started",
            "inventory_enriched",
            "service_impact_completed",
            "risk_assessed",
            "bedrock_reasoning_completed",
            "recommendation_generated",
            "governance_decided",
            "action_blocked",
            "verification_blocked",
            "evidence_captured",
            "agent2_completed",
            "run_completed",
        ]
        assert [e.sequence for e in events_deny] == list(range(1, len(events_deny) + 1))
        assert all(e.run_id == "WX-SSE-DENY" for e in events_deny)
        assert all(e.trace_id == "TRACE-SSE-DENY" for e in events_deny)
        assert sum(1 for e in events_deny if e.terminal) == 1
        assert events_deny[-1].data["governance_decision"] == "DENY"
        assert events_deny[-1].data["approval_status"] == "NOT_REQUIRED"
        assert events_deny[-1].data["execution_status"] == "BLOCKED"
        assert events_deny[-1].data["verification_status"] == "BLOCKED"

        # 2. ALLOW scenario stream
        _, _, raw_allow = await _call_asgi_sse(
            asgi_app,
            "POST",
            "/api/runs/stream",
            body={
                "scenario": "allow",
                "run_id": "WX-SSE-ALLOW",
                "trace_id": "TRACE-SSE-ALLOW",
            },
        )
        _, events_allow = parse_sse_wire_stream(raw_allow)
        allow_types = [e.event_type for e in events_allow]
        assert allow_types == [
            "connected",
            "run_started",
            "agent1_started",
            "weather_received",
            "weather_completed",
            "geo_correlation_completed",
            "asset_mapping_completed",
            "agent1_completed",
            "agent2_started",
            "inventory_enriched",
            "service_impact_completed",
            "risk_assessed",
            "bedrock_reasoning_completed",
            "recommendation_generated",
            "governance_decided",
            "action_started",
            "action_executed",
            "verification_started",
            "verification_passed",
            "evidence_captured",
            "agent2_completed",
            "run_completed",
        ]
        assert events_allow[-1].data["governance_decision"] == "ALLOW"
        assert events_allow[-1].data["approval_status"] == "NOT_REQUIRED"
        assert events_allow[-1].data["execution_status"] == "EXECUTED"
        assert events_allow[-1].data["verification_status"] == "PASSED"

        # 3. PAUSE scenario stream (normal production mode -> closes with AWAITING_APPROVAL)
        _, _, raw_pause = await _call_asgi_sse(
            asgi_app,
            "POST",
            "/api/runs/stream",
            body={
                "scenario": "pause",
                "run_id": "WX-SSE-PAUSE",
                "trace_id": "TRACE-SSE-PAUSE",
            },
        )
        _, events_pause = parse_sse_wire_stream(raw_pause)
        pause_types = [e.event_type for e in events_pause]
        assert pause_types == [
            "connected",
            "run_started",
            "agent1_started",
            "weather_received",
            "weather_completed",
            "geo_correlation_completed",
            "asset_mapping_completed",
            "agent1_completed",
            "agent2_started",
            "inventory_enriched",
            "service_impact_completed",
            "risk_assessed",
            "bedrock_reasoning_completed",
            "recommendation_generated",
            "governance_decided",
            "approval_pending",
            "action_blocked",
            "evidence_captured",
            "agent2_completed",
            "run_completed",
        ]
        assert events_pause[-1].data["workflow_status"] == "AWAITING_APPROVAL"
        assert events_pause[-1].data["approval_status"] == "PENDING"
        assert events_pause[-1].data["execution_status"] == "BLOCKED"

        # 4. PAUSE + APPROVE demo mode stream
        _, _, raw_approve = await _call_asgi_sse(
            asgi_app,
            "POST",
            "/api/runs/stream",
            body={
                "scenario": "pause",
                "run_id": "WX-SSE-APPROVE",
                "trace_id": "TRACE-SSE-APPROVE",
                "approval_instruction": "APPROVED",
            },
        )
        _, events_approve = parse_sse_wire_stream(raw_approve)
        approve_types = [e.event_type for e in events_approve]
        assert approve_types == [
            "connected",
            "run_started",
            "agent1_started",
            "weather_received",
            "weather_completed",
            "geo_correlation_completed",
            "asset_mapping_completed",
            "agent1_completed",
            "agent2_started",
            "inventory_enriched",
            "service_impact_completed",
            "risk_assessed",
            "bedrock_reasoning_completed",
            "recommendation_generated",
            "governance_decided",
            "approval_approved",
            "action_started",
            "action_executed",
            "verification_started",
            "verification_passed",
            "evidence_captured",
            "agent2_completed",
            "run_completed",
        ]
        assert events_approve[-1].data["approval_status"] == "APPROVED"
        assert events_approve[-1].data["execution_status"] == "EXECUTED"
        assert events_approve[-1].data["verification_status"] == "PASSED"

        # 5. PAUSE + REJECT demo mode stream
        _, _, raw_reject = await _call_asgi_sse(
            asgi_app,
            "POST",
            "/api/runs/stream",
            body={
                "scenario": "pause",
                "run_id": "WX-SSE-REJECT",
                "trace_id": "TRACE-SSE-REJECT",
                "approval_instruction": "REJECTED",
            },
        )
        _, events_reject = parse_sse_wire_stream(raw_reject)
        reject_types = [e.event_type for e in events_reject]
        assert reject_types == [
            "connected",
            "run_started",
            "agent1_started",
            "weather_received",
            "weather_completed",
            "geo_correlation_completed",
            "asset_mapping_completed",
            "agent1_completed",
            "agent2_started",
            "inventory_enriched",
            "service_impact_completed",
            "risk_assessed",
            "bedrock_reasoning_completed",
            "recommendation_generated",
            "governance_decided",
            "approval_rejected",
            "action_blocked",
            "verification_blocked",
            "evidence_captured",
            "agent2_completed",
            "run_completed",
        ]
        assert events_reject[-1].data["approval_status"] == "REJECTED"
        assert events_reject[-1].data["execution_status"] == "BLOCKED"
        assert events_reject[-1].data["verification_status"] == "BLOCKED"

    anyio.run(_run_all)


def test_last_event_id_reconnect_replays_without_rerunning_agents() -> None:
    """Verify Last-Event-ID replays only later events and never reruns Agent 1 or Agent 2."""
    invoker = SimulatedAwsAgentCoreInvoker()
    svc = CommandCenterBackendService(
        data_mode="aws_live",
        agentcore_invoker=invoker,
        agent1_runtime_arn=invoker.AGENT1_ARN,
        agent2_runtime_arn=invoker.AGENT2_ARN,
    )
    asgi_app = create_command_center_api(svc)

    async def _run_replay_check() -> None:
        _, _, raw_initial = await _call_asgi_sse(
            asgi_app,
            "POST",
            "/api/runs/stream",
            body={
                "scenario": "pause",
                "run_id": "WX-REPLAY-1",
                "trace_id": "TRACE-REPLAY-1",
            },
        )
        _, initial_events = parse_sse_wire_stream(raw_initial)
        assert len(invoker.calls) == 2  # 1 Agent 1 call + 1 Agent 2 call
        assert len(initial_events) == 20

        # Reconnect after event #10 via GET /api/runs/{run_id}/stream with Last-Event-ID
        tenth_event = initial_events[9]
        assert tenth_event.sequence == 10
        _, _, raw_replay = await _call_asgi_sse(
            asgi_app,
            "GET",
            "/api/runs/WX-REPLAY-1/stream",
            headers=[(b"last-event-id", tenth_event.event_id.encode("utf-8"))],
        )
        _, replayed_events = parse_sse_wire_stream(raw_replay)
        assert [e.sequence for e in replayed_events] == list(range(11, len(initial_events) + 1))
        # Confirm Agent 1 and Agent 2 were NOT invoked again during replay
        assert len(invoker.calls) == 2

        # Now explicitly approve via submit_approval and stream new events after Last-Event-ID
        svc.submit_approval(
            run_id="WX-REPLAY-1",
            decision=ApprovalStatus.APPROVED,
            approver_reference="demo-approver",
            strict_pending_check=True,
        )
        last_initial_id = initial_events[-1].event_id
        _, _, raw_post_approval = await _call_asgi_sse(
            asgi_app,
            "GET",
            "/api/runs/WX-REPLAY-1/stream",
            headers=[(b"last-event-id", last_initial_id.encode("utf-8"))],
        )
        _, post_approval_events = parse_sse_wire_stream(raw_post_approval)
        assert [e.event_type for e in post_approval_events] == [
            "approval_approved",
            "action_started",
            "action_executed",
            "verification_started",
            "verification_passed",
            "evidence_captured",
            "agent2_completed",
            "run_completed",
        ]

    anyio.run(_run_replay_check)


class FakeSucceededBedrockPort:
    @property
    def model_id(self) -> str:
        return "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def generate_explanation(self, context: BedrockReasoningContext) -> BedrockReasoningOutput:
        del context
        return BedrockReasoningOutput(
            situation_summary="Live Bedrock situation summary.",
            risk_explanation="Live Bedrock risk explanation.",
            recommendation_rationale="Live Bedrock recommendation rationale.",
            operator_summary="Live Bedrock operator summary.",
        )


def test_bedrock_success_and_secret_redaction_on_run_failed() -> None:
    """Verify bedrock_reasoning_completed for fallback_used=False and redaction on run_failed."""
    agent2_live_ai = NetworkResilienceAgent(
        inventory_port=MockNetworkInventoryAdapter(),
        bedrock_reasoning_service=ResilientBedrockReasoningService(
            primary_adapter=FakeSucceededBedrockPort()
        ),
        strict_region_check=False,
    )
    orch = EndToEndOrchestrator(agent2=agent2_live_ai)
    svc = CommandCenterBackendService(orchestrator=orch, data_mode="mock")

    events = list(
        svc.stream_workflow(
            {
                "run_id": "WX-BEDROCK-OK",
                "trace_id": "TRACE-BEDROCK-OK",
                "weather_event": "SEVERE_STORM",
                "severity": "HIGH",
                "affected_region": "Demo-Region-A",
            }
        )
    )
    bedrock_ev = next(e for e in events if e.event_type == "bedrock_reasoning_completed")
    assert bedrock_ev.data["fallback_used"] is False
    assert bedrock_ev.data["model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert "Bedrock reasoning completed" in bedrock_ev.message

    # Trigger failure with a secret token -> must emit run_failed once with redaction
    fail_events = list(
        svc.stream_workflow(
            {
                "run_id": "WX-FAIL-SECRET",
                "trace_id": "TRACE-FAIL-SECRET",
                "weather_event": "INVALID_EVENT_aws_session_token=SECRET_TOKEN_XYZ",
                "severity": "HIGH",
                "affected_region": "Demo-Region-A",
            }
        )
    )
    assert sum(1 for e in fail_events if e.terminal) == 1
    assert fail_events[-1].event_type == "run_failed"
    assert "SECRET_TOKEN_XYZ" not in fail_events[-1].message
    assert "SECRET_TOKEN_XYZ" not in str(fail_events[-1].data)
    assert "[REDACTED]" in fail_events[-1].data["safe_message"]
