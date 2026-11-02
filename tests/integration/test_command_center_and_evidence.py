"""Tests for Phase 10-15:
- Agent 1 -> Agent 2 AWS-live handoff via AgentCoreInvokerPort
- DENY, ALLOW, PAUSE, PAUSE+APPROVE, PAUSE+REJECT in AWS-live mode
- CloudWatch evidence filtering & evidence bundle generation
- Command Center HTTP API (/api/runs, approve, reject, evidence)
- Command Center Frontend display models (GovernanceDisplayBanner, EvidenceDisplayRow)
"""

import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.types import Scope

from agents.network_resilience.agentcore_app import invoke as invoke_agent2
from agents.weather_network_intelligence.agentcore_app import invoke as invoke_agent1
from command_center.backend import (
    CommandCenterBackendService,
    CommandCenterDataMode,
    create_command_center_api,
)
from command_center.frontend import (
    DASHBOARD_TITLE,
    WORKFLOW_PIPELINE_LABELS,
    map_view_to_dashboard_model,
)
from common.exceptions import GovernanceError
from contracts.action_result import ExecutionStatus, VerificationStatus
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from integrations.cloudwatch import (
    build_evidence_index,
    discover_agent2_log_group,
    fetch_correlated_cloudwatch_events,
    filter_cloudwatch_events_by_correlation,
    write_run_evidence_bundle,
)
from orchestration.workflow import DemoScenario


class SimulatedAwsAgentCoreInvoker:
    """Simulates AWS AgentCore Runtime invocations by routing ARNs to Agent 1 and Agent 2."""

    AGENT1_ARN = (
        "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/"
        "cohort3_weather_network_intelligence-aRoVQk71fj"
    )
    AGENT2_ARN = (
        "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/"
        "cohort3_network_resilience-SIMULATED"
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_runtime(
        self,
        *,
        runtime_arn: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        del session_id
        self.calls.append((runtime_arn, dict(payload)))
        if runtime_arn == self.AGENT1_ARN:
            res1 = invoke_agent1(dict(payload))
            assert isinstance(res1, dict)
            return res1
        if runtime_arn == self.AGENT2_ARN:
            res2 = invoke_agent2(dict(payload))
            assert isinstance(res2, dict)
            return res2
        raise AssertionError(f"Unexpected runtime ARN: {runtime_arn}")


def test_command_center_aws_live_mode_scenarios_and_handoff() -> None:
    """Verify AWS_LIVE mode invokes Agent 1 -> Agent 2 across all governance scenarios."""
    invoker = SimulatedAwsAgentCoreInvoker()
    backend = CommandCenterBackendService(
        data_mode="aws_live",
        agentcore_invoker=invoker,
        agent1_runtime_arn=invoker.AGENT1_ARN,
        agent2_runtime_arn=invoker.AGENT2_ARN,
    )
    assert backend.data_mode == CommandCenterDataMode.AWS_LIVE
    assert backend.is_aws_live is True

    # 1. DENY scenario in AWS-live mode
    deny_view = backend.run_scenario(DemoScenario.DENY)
    assert deny_view.governance.decision == GovernanceDecision.DENY
    assert deny_view.approval.status == ApprovalStatus.NOT_REQUIRED
    assert deny_view.execution.status == ExecutionStatus.BLOCKED
    assert deny_view.verification.status == VerificationStatus.BLOCKED
    # Confirm DENY cannot be approved
    with pytest.raises(GovernanceError):
        backend.submit_approval(
            run_id=deny_view.header.run_id,
            decision=ApprovalStatus.APPROVED,
        )

    # 2. ALLOW scenario in AWS-live mode
    allow_view = backend.run_scenario(DemoScenario.ALLOW)
    assert allow_view.governance.decision == GovernanceDecision.ALLOW
    assert allow_view.approval.status == ApprovalStatus.NOT_REQUIRED
    assert allow_view.execution.status == ExecutionStatus.EXECUTED
    assert allow_view.verification.status == VerificationStatus.PASSED

    # 3. PAUSE scenario in AWS-live mode
    pause_view = backend.run_scenario(DemoScenario.PAUSE)
    assert pause_view.header.run_id == "WX-46329ae2b74b"
    assert pause_view.header.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    assert pause_view.governance.decision == GovernanceDecision.PAUSE
    assert pause_view.approval.status == ApprovalStatus.PENDING
    assert pause_view.execution.status == ExecutionStatus.BLOCKED

    # 4. PAUSE + REJECT in AWS-live mode
    rejected_view = backend.submit_approval(
        run_id=pause_view.header.run_id,
        decision=ApprovalStatus.REJECTED,
        approver_reference="demo-approver",
        comments="Rejected in AWS-live test.",
    )
    assert rejected_view.governance.decision == GovernanceDecision.PAUSE
    assert rejected_view.approval.status == ApprovalStatus.REJECTED
    assert rejected_view.execution.status == ExecutionStatus.BLOCKED
    assert rejected_view.verification.status == VerificationStatus.BLOCKED

    # 5. PAUSE + APPROVE in AWS-live mode
    approved_view = backend.submit_approval(
        run_id=pause_view.header.run_id,
        decision=ApprovalStatus.APPROVED,
        approver_reference="demo-approver",
        comments="Approved in AWS-live test.",
    )
    assert approved_view.governance.decision == GovernanceDecision.PAUSE
    assert approved_view.approval.status == ApprovalStatus.APPROVED
    assert approved_view.execution.status == ExecutionStatus.EXECUTED
    assert approved_view.verification.status == VerificationStatus.PASSED


def test_cloudwatch_evidence_filtering_and_bundle_writer(tmp_path: Path) -> None:
    raw_logs = [
        {
            "timestamp": 1001,
            "logStreamName": "stream-1",
            "logGroupName": "/aws/bedrock-agentcore/runtimes/agent1",
            "message": '{"run_id": "WX-TARGET", "trace_id": "TRACE-TARGET", "event": "ok"}',
        },
        {
            "timestamp": 1002,
            "logStreamName": "stream-2",
            "logGroupName": "/aws/bedrock-agentcore/runtimes/agent1",
            "message": '{"run_id": "WX-OTHER", "trace_id": "TRACE-OTHER", "event": "unrelated"}',
        },
        {
            "timestamp": 1003,
            "logStreamName": "stream-3",
            "logGroupName": "/aws/bedrock-agentcore/runtimes/agent2",
            "message": "run_id=WX-TARGET aws_session_token=SECRET_VALUE_999",
        },
    ]
    filtered = filter_cloudwatch_events_by_correlation(
        raw_logs,
        run_id="WX-TARGET",
        trace_id="TRACE-TARGET",
    )
    assert len(filtered) == 2
    assert all("WX-OTHER" not in item["message"] for item in filtered)
    assert "SECRET_VALUE_999" not in filtered[1]["message"]
    assert "[REDACTED]" in filtered[1]["message"]

    # Build and write full evidence bundle
    invoker = SimulatedAwsAgentCoreInvoker()
    backend = CommandCenterBackendService(
        data_mode="aws_live",
        agentcore_invoker=invoker,
        agent1_runtime_arn=invoker.AGENT1_ARN,
        agent2_runtime_arn=invoker.AGENT2_ARN,
    )
    view = backend.run_scenario(DemoScenario.PAUSE)
    a2_dict = json.loads(view.model_dump_json())
    a1_dict = {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": view.header.run_id,
        "trace_id": view.header.trace_id,
        "weather_event": view.weather.event,
        "severity": view.weather.severity,
        "affected_region": view.weather.affected_region,
        "affected_network_elements": ["NE-101", "NE-102"],
    }

    index = build_evidence_index(
        run_id=view.header.run_id,
        trace_id=view.header.trace_id,
        agent1_response=a1_dict,
        agent2_response=a2_dict,
    )
    stages_in_index = {item["stage"] for item in index}
    for required_stage in (
        "WEATHER",
        "GEO_CORRELATION",
        "ASSET_IMPACT",
        "RISK_ASSESSMENT",
        "RECOMMENDATION",
        "GOVERNANCE",
        "APPROVAL",
        "ACTION",
        "VERIFICATION",
    ):
        assert required_stage in stages_in_index

    bundle_dir = write_run_evidence_bundle(
        output_root=tmp_path,
        run_id=view.header.run_id,
        trace_id=view.header.trace_id,
        agent1_response=a1_dict,
        agent2_response=a2_dict,
        cloudwatch_events=filtered,
    )
    for expected_file in (
        "agent1-response.json",
        "agent2-response.json",
        "workflow-summary.json",
        "cloudwatch-events.json",
        "evidence-index.json",
        "runtime-metadata.json",
    ):
        assert (bundle_dir / expected_file).exists()


class FakeLogsInsightsClient:
    """Fake CloudWatch Logs client exercising newest-stream scan and Logs Insights fallback."""

    def __init__(self, *, use_insights_only: bool = False) -> None:
        self.use_insights_only = use_insights_only

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        if self.use_insights_only:
            return {"events": []}
        group = str(kwargs.get("logGroupName", ""))
        if kwargs.get("filterPattern") == '"WX-PAUSE-123"' and "network_resilience" in group:
            return {
                "events": [
                    {
                        "timestamp": 2002,
                        "logStreamName": "stream-b",
                        "message": (
                            '{"run_id": "WX-PAUSE-123", "trace_id": "TRACE-PAUSE-123", '
                            '"agent_name": "network_resilience", '
                            '"operation": "evaluate_governance", '
                            '"result": "PAUSE", "decision": "PAUSE"}'
                        ),
                    }
                ]
            }
        if "weather_network_intelligence" in group:
            return {
                "events": [
                    {
                        "timestamp": 2001,
                        "logStreamName": "stream-a",
                        "message": (
                            '{"run_id": "WX-PAUSE-123", "trace_id": "TRACE-PAUSE-123", '
                            '"agent_name": "weather_network_intelligence", '
                            '"operation": "build_network_risk_event", "result": "success"}'
                        ),
                    }
                ]
            }
        return {"events": []}

    def describe_log_streams(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"logStreams": [{"logStreamName": "stream-a"}]}

    def start_query(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"queryId": "query-001"}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {
            "status": "Complete",
            "results": [
                [
                    {"field": "@timestamp", "value": "2026-11-02 06:00:01.000"},
                    {
                        "field": "@log",
                        "value": "835156611439:/aws/bedrock-agentcore/runtimes/agent1",
                    },
                    {"field": "@logStream", "value": "stream-insights-1"},
                    {
                        "field": "@message",
                        "value": (
                            '{"run_id": "WX-INSIGHTS-1", "trace_id": "TRACE-INSIGHTS-1", '
                            '"agent_name": "weather_network_intelligence", '
                            '"operation": "agentcore_invoke", "result": "success"}'
                        ),
                    },
                ]
            ],
        }


def test_cloudwatch_logs_insights_fallback_and_dynamic_discovery(tmp_path: Path) -> None:
    """Verify 3-tier CloudWatch fetch (filterPattern, recent streams, and Insights)."""
    # 1. Dynamic Agent 2 log group discovery from runtime_info.json
    info_file = tmp_path / "agent2_runtime_info.json"
    expected_lg = "/aws/bedrock-agentcore/runtimes/cohort3_network_resilience-xyz987-DEFAULT"
    info_file.write_text(
        json.dumps(
            {
                "agentRuntimeId": "cohort3_network_resilience-xyz987",
                "agentRuntimeArn": (
                    "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/"
                    "cohort3_network_resilience-xyz987"
                ),
                "logGroup": expected_lg,
            }
        ),
        encoding="utf-8",
    )
    lg, arn = discover_agent2_log_group(runtime_info_path=info_file)
    assert lg == expected_lg
    assert arn is not None
    assert arn.endswith("cohort3_network_resilience-xyz987")

    # 2. Tier 1 + Tier 2 fetch with chronological sorting
    events_direct = fetch_correlated_cloudwatch_events(
        log_group_names=[
            "/aws/bedrock-agentcore/runtimes/weather_network_intelligence",
            "/aws/bedrock-agentcore/runtimes/network_resilience",
        ],
        run_id="WX-PAUSE-123",
        trace_id="TRACE-PAUSE-123",
        logs_client=FakeLogsInsightsClient(use_insights_only=False),
    )
    assert len(events_direct) == 2
    assert [e["timestamp"] for e in events_direct] == [2001, 2002]

    # 3. Tier 3 CloudWatch Logs Insights fallback when filter_log_events returns 0 events
    events_insights = fetch_correlated_cloudwatch_events(
        log_group_names=["/aws/bedrock-agentcore/runtimes/agent1"],
        run_id="WX-INSIGHTS-1",
        trace_id="TRACE-INSIGHTS-1",
        logs_client=FakeLogsInsightsClient(use_insights_only=True),
    )
    assert len(events_insights) == 1
    assert "WX-INSIGHTS-1" in events_insights[0]["message"]


def test_frontend_display_mapping_and_live_governance_banners() -> None:
    backend = CommandCenterBackendService(data_mode="mock")

    # DENY display
    deny_model = map_view_to_dashboard_model(
        backend.run_scenario(DemoScenario.DENY),
        api_base_url="http://127.0.0.1:8000/",
    )
    assert deny_model.title == DASHBOARD_TITLE
    assert deny_model.api_base_url == "http://127.0.0.1:8000"
    assert deny_model.pipeline_steps == list(WORKFLOW_PIPELINE_LABELS)
    assert deny_model.governance_banner.policy_decision_line == "Policy Decision: DENY"
    assert deny_model.governance_banner.approval_line == "Approval: NOT_REQUIRED"
    assert deny_model.governance_banner.execution_line == "Execution: BLOCKED"

    # ALLOW display
    allow_model = map_view_to_dashboard_model(backend.run_scenario(DemoScenario.ALLOW))
    assert allow_model.governance_banner.policy_decision_line == "Policy Decision: ALLOW"
    assert allow_model.governance_banner.approval_line == "Approval: NOT_REQUIRED"
    assert allow_model.governance_banner.execution_line == "Execution: EXECUTED"

    # PAUSE display
    pause_view = backend.run_scenario(DemoScenario.PAUSE)
    pause_model = map_view_to_dashboard_model(pause_view)
    assert pause_model.governance_banner.policy_decision_line == "Policy Decision: PAUSE"
    assert pause_model.governance_banner.approval_line == "Approval: PENDING"
    assert pause_model.governance_banner.execution_line == "Execution: BLOCKED"

    # PAUSE + APPROVED display
    approved_view = backend.submit_approval(
        run_id=pause_view.header.run_id,
        decision=ApprovalStatus.APPROVED,
    )
    approved_model = map_view_to_dashboard_model(approved_view)
    assert approved_model.governance_banner.policy_decision_line == "Policy Decision: PAUSE"
    assert approved_model.governance_banner.approval_line == "Approval: APPROVED"
    assert approved_model.governance_banner.execution_line == "Execution: EXECUTED"

    # PAUSE + REJECTED display
    rejected_view = backend.submit_approval(
        run_id=pause_view.header.run_id,
        decision=ApprovalStatus.REJECTED,
    )
    rejected_model = map_view_to_dashboard_model(rejected_view)
    assert rejected_model.governance_banner.policy_decision_line == "Policy Decision: PAUSE"
    assert rejected_model.governance_banner.approval_line == "Approval: REJECTED"
    assert rejected_model.governance_banner.execution_line == "Execution: BLOCKED"
    assert len(rejected_model.evidence_rows) == 10
    assert all(r.synthetic_indicator == "SYNTHETIC" for r in rejected_model.evidence_rows)


def test_command_center_asgi_api_endpoints() -> None:
    """Test POST /api/runs, GET /api/runs/{run_id}, approve, reject, and evidence routes."""
    svc = CommandCenterBackendService(data_mode="mock")
    asgi_app = create_command_center_api(svc)

    async def _call_asgi(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query_string: str = "",
    ) -> tuple[int, dict[str, Any]]:
        payload_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        sent = False
        status_code = 500
        chunks: list[bytes] = []

        async def receive() -> MutableMapping[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {
                    "type": "http.request",
                    "body": payload_bytes,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        scope: Scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query_string.encode("utf-8"),
            "headers": [(b"content-type", b"application/json")],
        }
        await asgi_app(scope, receive, send)
        raw_out = b"".join(chunks).decode("utf-8")
        return status_code, json.loads(raw_out)

    async def _run_sequence() -> None:
        # 1. POST /api/runs (create Pause run #1 for rejection test)
        code, created = await _call_asgi("POST", "/api/runs", {"scenario": "pause"})
        assert code == 200
        run_id = created["header"]["run_id"]
        trace_id = created["header"]["trace_id"]
        assert created["run_id"] == run_id
        assert created["trace_id"] == trace_id
        assert created["overall_status"] == "PAUSED_AWAITING_HUMAN_APPROVAL"
        assert created["agent1_result"] is not None
        assert created["agent2_result"] is not None
        assert "cloudwatch_event_counts" in created
        assert "evidence_links" in created
        assert created["governance"]["decision"] == "PAUSE"
        assert created["approval"]["status"] == "PENDING"
        assert created["execution"]["status"] == "BLOCKED"

        # 2. GET /api/runs/{run_id}
        code_get, fetched = await _call_asgi("GET", f"/api/runs/{run_id}")
        assert code_get == 200
        assert fetched["header"]["run_id"] == run_id
        assert fetched["ai_reasoning"] is not None

        # 3. POST /api/runs/{run_id}/reject (transitions PAUSE + PENDING -> REJECTED)
        code_rej, rejected = await _call_asgi(
            "POST",
            f"/api/runs/{run_id}/reject",
            {"approver_reference": "demo-approver"},
        )
        assert code_rej == 200
        assert rejected["approval"]["status"] == "REJECTED"
        assert rejected["execution"]["status"] == "BLOCKED"
        assert rejected["verification"]["status"] == "BLOCKED"

        # 4. Verify a REJECTED run cannot subsequently be approved (must be PENDING)
        code_re_app, _ = await _call_asgi(
            "POST",
            f"/api/runs/{run_id}/approve",
            {"approver_reference": "demo-approver"},
        )
        assert code_re_app == 409

        # 5. Create fresh PAUSE run and approve it (transitions PAUSE + PENDING -> APPROVED)
        code2, _ = await _call_asgi("POST", "/api/runs", {"scenario": "pause"})
        assert code2 == 200
        code_app, approved = await _call_asgi(
            "POST",
            f"/api/runs/{run_id}/approve",
            {"approver_reference": "demo-approver"},
        )
        assert code_app == 200
        assert approved["approval"]["status"] == "APPROVED"
        assert approved["execution"]["status"] == "EXECUTED"
        assert approved["verification"]["status"] == "PASSED"

        # 6. GET /api/runs/{run_id}/evidence (verify full evidence bundle fields)
        code_ev, ev_resp = await _call_asgi(
            "GET",
            f"/api/runs/{run_id}/evidence",
            query_string=f"trace_id={trace_id}",
        )
        assert code_ev == 200
        assert ev_resp["evidence_count"] == 10
        assert len(ev_resp["normalized_evidence"]) == 13
        assert ev_resp["agent1_response"] is not None
        assert ev_resp["agent2_response"] is not None
        assert ev_resp["runtime_metadata"] is not None
        assert ev_resp["workflow_summary"] is not None

    anyio.run(_run_sequence)
