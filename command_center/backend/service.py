"""Command Center Backend Aggregation Service, SSE Workflow Orchestrator, & ASGI API Server.

Backend-only orchestration, SSE streaming, & evidence API layer:
- Supports configurable data modes via COMMAND_CENTER_DATA_MODE: "mock"/"local" or "aws_live"
- Exposes HTTP endpoints:
  - POST /api/runs
  - POST /api/runs/stream
  - GET  /api/runs/{run_id}
  - GET  /api/runs/{run_id}/stream
  - POST /api/runs/{run_id}/approve
  - POST /api/runs/{run_id}/reject
  - GET  /api/runs/{run_id}/evidence

Enforces strict governance invariants:
- Approval/rejection is allowed ONLY when governance == PAUSE and approval == PENDING.
- Never approves DENY or already-decided runs.
- Never recalculates risk or overrides governance outcomes.
- Never exposes credentials, session tokens, authorization headers, or private keys.
"""

import argparse
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from command_center.backend.sse import (
    SSE_HEADERS,
    InMemoryWorkflowEventStore,
    WorkflowEventStore,
    WorkflowSSEBuilder,
    emit_agent1_completion_events,
    emit_agent2_completion_events,
    emit_failure_terminal_event,
    format_heartbeat_comment,
    format_sse_frame,
)
from common.config import Settings
from common.exceptions import ApplicationError, GovernanceError
from common.logging import configure_logging
from common.tracing import ensure_run_id, ensure_trace_id
from contracts.action_result import (
    Agent2Result,
    CommandCenterView,
    EvidenceRecord,
    WorkflowSSEEvent,
    WorkflowStageState,
)
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from contracts.network_risk_event import NetworkRiskEvent, validate_agent2_input
from integrations.agentcore import (
    DEFAULT_AGENT1_RUNTIME_ARN,
    DEFAULT_AGENT2_RUNTIME_NAME,
    AgentCoreInvokerPort,
    BotoAgentCoreRuntimeClient,
    generate_runtime_session_id,
    sanitize_runtime_error_message,
)
from integrations.cloudwatch import (
    AGENT1_DEFAULT_LOG_GROUP,
    build_evidence_index,
    discover_agent2_log_group,
    fetch_correlated_cloudwatch_events,
    write_run_evidence_bundle,
)
from orchestration.workflow import (
    SCENARIO_INPUTS,
    DemoScenario,
    EndToEndOrchestrator,
)


class CommandCenterDataMode(StrEnum):
    MOCK = "mock"
    LOCAL = "local"
    AWS_LIVE = "aws_live"


class CommandCenterBackendService:
    """Backend service exposing orchestrated Agent 1 + Agent 2 state, SSE streams, and evidence."""

    def __init__(
        self,
        orchestrator: EndToEndOrchestrator | None = None,
        *,
        data_mode: str | None = None,
        settings: Settings | None = None,
        agentcore_invoker: AgentCoreInvokerPort | None = None,
        agent1_runtime_arn: str | None = None,
        agent2_runtime_arn: str | None = None,
        evidence_root: Path | None = None,
        event_store: WorkflowEventStore | None = None,
    ) -> None:
        self.settings = settings or Settings()
        raw_mode = (
            (
                data_mode
                or os.getenv("COMMAND_CENTER_DATA_MODE")
                or self.settings.command_center_data_mode
                or "mock"
            )
            .strip()
            .lower()
        )
        if raw_mode not in {"mock", "local", "aws_live"}:
            raise ApplicationError(
                f"Unsupported COMMAND_CENTER_DATA_MODE '{raw_mode}'. "
                "Supported: mock, local, aws_live."
            )
        self.data_mode = CommandCenterDataMode(raw_mode)
        self.orchestrator = orchestrator or EndToEndOrchestrator()
        self.event_store: WorkflowEventStore = event_store or InMemoryWorkflowEventStore()
        self._agentcore_invoker = agentcore_invoker
        self._agent1_runtime_arn = (
            agent1_runtime_arn
            or os.getenv("AGENT1_RUNTIME_ARN")
            or self.settings.agent1_runtime_arn
            or DEFAULT_AGENT1_RUNTIME_ARN
        )
        self._agent2_runtime_arn = (
            agent2_runtime_arn
            or os.getenv("AGENT2_RUNTIME_ARN")
            or self.settings.agent2_runtime_arn
        )
        self.evidence_root = evidence_root or (Path(__file__).resolve().parents[2] / "evidence")
        self._agent1_events: dict[str, NetworkRiskEvent] = {}
        self._agent2_results: dict[str, Agent2Result] = {}
        self._runtime_metadata: dict[str, dict[str, Any]] = {}
        self._cloudwatch_events: dict[str, list[dict[str, Any]]] = {}

    @property
    def is_aws_live(self) -> bool:
        return self.data_mode == CommandCenterDataMode.AWS_LIVE

    def _get_invoker(self) -> AgentCoreInvokerPort:
        if self._agentcore_invoker is None:
            self._agentcore_invoker = BotoAgentCoreRuntimeClient(
                region_name=self.settings.aws_region
            )
        return self._agentcore_invoker

    def _resolve_agent2_arn(self) -> str:
        if self._agent2_runtime_arn and self._agent2_runtime_arn.strip():
            return self._agent2_runtime_arn.strip()
        invoker = self._get_invoker()
        if isinstance(invoker, BotoAgentCoreRuntimeClient):
            self._agent2_runtime_arn = invoker.discover_runtime_arn(DEFAULT_AGENT2_RUNTIME_NAME)
            return self._agent2_runtime_arn
        raise ApplicationError(
            "AGENT2_RUNTIME_ARN must be configured when using a custom AgentCoreInvokerPort."
        )

    def _store_run_artifacts(
        self,
        *,
        event: NetworkRiskEvent,
        result: Agent2Result,
        agent1_session_id: str | None = None,
        agent2_session_id: str | None = None,
    ) -> None:
        run_id = result.run_id
        self._agent1_events[run_id] = event
        self._agent2_results[run_id] = result
        self.orchestrator._pending_events[run_id] = event
        self.orchestrator._results[run_id] = result
        ai_trace = result.ai_reasoning
        self._runtime_metadata[run_id] = {
            "run_id": run_id,
            "trace_id": result.trace_id,
            "data_mode": self.data_mode.value,
            "aws_region": self.settings.aws_region,
            "agent1_runtime_arn": (
                self._agent1_runtime_arn if self.is_aws_live else "local-agent1"
            ),
            "agent2_runtime_arn": (
                self._agent2_runtime_arn if self.is_aws_live else "local-agent2"
            ),
            "agent1_session_id": agent1_session_id,
            "agent2_session_id": agent2_session_id,
            "bedrock_model_id": ai_trace.model_id if ai_trace else None,
            "bedrock_result_status": ai_trace.result_status if ai_trace else None,
            "bedrock_fallback_used": ai_trace.fallback_used if ai_trace else None,
            "bedrock_error_reason": ai_trace.error_reason if ai_trace else None,
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    def _invoke_agent1_step(
        self, normalized_payload: dict[str, Any]
    ) -> tuple[NetworkRiskEvent, str | None]:
        if self.is_aws_live:
            sess1 = generate_runtime_session_id()
            invoker = self._get_invoker()
            agent1_raw = invoker.invoke_runtime(
                runtime_arn=self._agent1_runtime_arn,
                payload=normalized_payload,
                session_id=sess1,
            )
            return validate_agent2_input(agent1_raw), sess1
        return self.orchestrator.agent1.process(normalized_payload), None

    def _invoke_agent2_step(
        self,
        risk_event: NetworkRiskEvent,
        *,
        simulated_approval_decision: ApprovalStatus | None = None,
        approver_reference: str = "demo-approver",
        approval_comments: str | None = None,
        force_deny: bool = False,
        force_deny_reason: str | None = None,
    ) -> tuple[Agent2Result, str | None]:
        if self.is_aws_live:
            sess2 = generate_runtime_session_id()
            agent2_payload: dict[str, Any] = risk_event.model_dump(mode="json")
            agent2_payload.pop("occurred_at", None)
            if force_deny:
                agent2_payload["force_deny"] = True
                if force_deny_reason:
                    agent2_payload["force_deny_reason"] = force_deny_reason
            if simulated_approval_decision is not None:
                agent2_payload["simulated_approval_decision"] = simulated_approval_decision.value
                agent2_payload["approver_reference"] = approver_reference
                if approval_comments:
                    agent2_payload["approval_comments"] = approval_comments
            invoker = self._get_invoker()
            agent2_raw = invoker.invoke_runtime(
                runtime_arn=self._resolve_agent2_arn(),
                payload=agent2_payload,
                session_id=sess2,
            )
            return Agent2Result.model_validate(agent2_raw), sess2

        result = self.orchestrator.agent2.process(
            risk_event,
            simulated_approval_decision=simulated_approval_decision,
            approver_reference=approver_reference,
            approval_comments=approval_comments,
            force_deny=force_deny,
            force_deny_reason=force_deny_reason,
        )
        return result, None

    def _prepare_request_payload(
        self, raw_request: dict[str, Any]
    ) -> tuple[dict[str, Any], bool, str | None, ApprovalStatus | None, str]:
        """Normalize incoming run or scenario payload and extract optional demo instructions."""
        body = dict(raw_request)
        scenario_raw = body.pop("scenario", None)
        approval_inst_raw = body.pop(
            "approval_instruction",
            body.pop("simulated_approval_decision", None),
        )
        approver_ref = str(body.pop("approver_reference", "demo-approver"))
        force_deny = bool(body.pop("force_deny", False))
        force_deny_reason = (
            str(body.pop("force_deny_reason")) if body.get("force_deny_reason") else None
        )

        if isinstance(scenario_raw, str) and scenario_raw.strip():
            resolved = DemoScenario(scenario_raw.strip().lower())
            base_input = dict(SCENARIO_INPUTS[resolved])
            if resolved == DemoScenario.DENY and self.is_aws_live:
                base_input["affected_region"] = "Region-C"
                force_deny = True
                force_deny_reason = (
                    force_deny_reason or "Explicit governance DENY scenario policy rule."
                )
            for k in (
                "run_id",
                "trace_id",
                "weather_event",
                "severity",
                "affected_region",
            ):
                if body.get(k):
                    base_input[k] = body[k]
            normalized = base_input
        else:
            normalized = body

        normalized["run_id"] = ensure_run_id(
            str(normalized["run_id"]) if normalized.get("run_id") else None
        )
        normalized["trace_id"] = ensure_trace_id(
            str(normalized["trace_id"]) if normalized.get("trace_id") else None
        )

        approval_decision: ApprovalStatus | None = None
        if isinstance(approval_inst_raw, str) and approval_inst_raw.strip():
            upper_inst = approval_inst_raw.strip().upper()
            if upper_inst not in {"NONE", "PENDING"}:
                approval_decision = ApprovalStatus(upper_inst)

        return (
            normalized,
            force_deny,
            force_deny_reason,
            approval_decision,
            approver_ref,
        )

    def stream_workflow(self, raw_request: dict[str, Any]) -> Iterator[WorkflowSSEEvent]:
        """Execute Agent 1 -> Agent 2 and yield ordered WorkflowSSEEvent objects.

        Also records every event in self.event_store for Last-Event-ID replay.
        """
        try:
            (
                normalized,
                force_deny,
                force_deny_reason,
                approval_decision,
                approver_ref,
            ) = self._prepare_request_payload(raw_request)
        except (ValueError, KeyError, ApplicationError) as exc:
            fallback_run_id = ensure_run_id(
                str(raw_request.get("run_id")) if raw_request.get("run_id") else None
            )
            fallback_trace_id = ensure_trace_id(
                str(raw_request.get("trace_id")) if raw_request.get("trace_id") else None
            )
            err_builder = WorkflowSSEBuilder(
                run_id=fallback_run_id,
                trace_id=fallback_trace_id,
                store=self.event_store,
            )
            yield err_builder.emit(
                event_type="connected",
                stage="WEATHER",
                status="IN_PROGRESS",
                source="workflow_sse_orchestrator",
                message="SSE workflow stream connected.",
            )
            yield emit_failure_terminal_event(
                err_builder,
                exc=exc,
                failed_stage="WEATHER",
                retryable=False,
            )
            return

        run_id = str(normalized["run_id"])
        trace_id = str(normalized["trace_id"])
        existing_events = self.event_store.list_by_run_id(run_id)
        start_seq = existing_events[-1].sequence if existing_events else 0
        builder = WorkflowSSEBuilder(
            run_id=run_id,
            trace_id=trace_id,
            store=self.event_store,
            start_sequence=start_seq,
        )

        # 1. Connection & run start events
        yield builder.emit(
            event_type="connected",
            stage="WEATHER",
            status="IN_PROGRESS",
            source="workflow_sse_orchestrator",
            message="SSE workflow stream connected.",
            data={"run_id": run_id, "trace_id": trace_id},
        )
        yield builder.emit(
            event_type="run_started",
            stage="WEATHER",
            status="IN_PROGRESS",
            source="workflow_sse_orchestrator",
            message=f"Workflow run '{run_id}' started in mode={self.data_mode.value}.",
            data={
                "run_id": run_id,
                "trace_id": trace_id,
                "data_mode": self.data_mode.value,
            },
        )

        # 2. Agent 1 invocation & events
        yield builder.emit(
            event_type="agent1_started",
            stage="WEATHER",
            status="IN_PROGRESS",
            source="weather_network_intelligence",
            message="Invoking Agent 1 (Weather & Network Intelligence Agent).",
            data={
                "weather_event": normalized.get("weather_event"),
                "severity": normalized.get("severity"),
                "affected_region": normalized.get("affected_region"),
            },
        )
        yield builder.emit(
            event_type="weather_received",
            stage="WEATHER",
            status="IN_PROGRESS",
            source="weather_network_intelligence",
            message="Weather event payload received by Agent 1.",
            data={
                "weather_event": normalized.get("weather_event"),
                "severity": normalized.get("severity"),
                "affected_region": normalized.get("affected_region"),
            },
        )

        try:
            risk_event, sess1 = self._invoke_agent1_step(normalized)
        except (ApplicationError, ValidationError, RuntimeError, OSError, ValueError) as exc:
            yield emit_failure_terminal_event(
                builder,
                exc=exc,
                failed_stage="WEATHER",
                retryable=False,
            )
            return

        yield from emit_agent1_completion_events(builder, risk_event)

        # 3. Agent 2 invocation & events
        yield builder.emit(
            event_type="agent2_started",
            stage="ASSET_IMPACT",
            status="IN_PROGRESS",
            source="network_resilience",
            message="Invoking Agent 2 (Network Resilience & Governed Action Agent).",
            data={
                "affected_network_elements": list(risk_event.affected_network_elements),
            },
        )

        try:
            result, sess2 = self._invoke_agent2_step(
                risk_event,
                simulated_approval_decision=approval_decision,
                approver_reference=approver_ref,
                force_deny=force_deny,
                force_deny_reason=force_deny_reason,
            )
        except (ApplicationError, ValidationError, RuntimeError, OSError, ValueError) as exc:
            yield emit_failure_terminal_event(
                builder,
                exc=exc,
                failed_stage="RISK_ASSESSMENT",
                retryable=False,
            )
            return

        self._store_run_artifacts(
            event=risk_event,
            result=result,
            agent1_session_id=sess1,
            agent2_session_id=sess2,
        )
        cw_count = len(self._cloudwatch_events.get(result.run_id, []))
        yield from emit_agent2_completion_events(
            builder,
            result,
            cloudwatch_event_count=cw_count,
            include_pre_governance=True,
        )

    def _validate_pending_pause_transition(self, existing: Agent2Result, run_id: str) -> None:
        if existing.governance.decision != GovernanceDecision.PAUSE:
            raise GovernanceError(
                f"Run '{run_id}' cannot be approved or rejected because governance decision "
                f"is {existing.governance.decision.value} (must be PAUSE)."
            )
        if existing.approval.status != ApprovalStatus.PENDING:
            raise GovernanceError(
                f"Run '{run_id}' cannot transition because approval status is already "
                f"{existing.approval.status.value} (must be PENDING)."
            )

    def run_scenario(
        self,
        scenario: DemoScenario | str,
        *,
        approve_after_pause: bool = False,
        approval_decision: ApprovalStatus = ApprovalStatus.APPROVED,
        approver_reference: str = "demo-approver",
    ) -> CommandCenterView:
        resolved = (
            scenario if isinstance(scenario, DemoScenario) else DemoScenario(scenario.lower())
        )
        req: dict[str, Any] = {"scenario": resolved.value}
        if resolved == DemoScenario.PAUSE and approve_after_pause:
            req["approval_instruction"] = approval_decision.value
            req["approver_reference"] = approver_reference

        for _ in self.stream_workflow(req):
            pass

        base_run_id = str(SCENARIO_INPUTS[resolved]["run_id"])
        return self.get_dashboard_state(base_run_id)

    def ingest_weather_event(self, payload: dict[str, Any]) -> CommandCenterView:
        normalized = dict(payload)
        normalized["run_id"] = ensure_run_id(
            str(normalized["run_id"]) if normalized.get("run_id") else None
        )
        normalized["trace_id"] = ensure_trace_id(
            str(normalized["trace_id"]) if normalized.get("trace_id") else None
        )
        run_id = str(normalized["run_id"])
        for _ in self.stream_workflow(normalized):
            pass
        return self.get_dashboard_state(run_id)

    def submit_approval(
        self,
        *,
        run_id: str,
        decision: ApprovalStatus,
        approver_reference: str = "demo-approver",
        comments: str | None = None,
        strict_pending_check: bool = False,
    ) -> CommandCenterView:
        existing = self._agent2_results.get(run_id) or self.orchestrator.get_result(run_id)
        if existing is None:
            raise ApplicationError(f"Unknown run_id '{run_id}' for approval continuation.")

        if strict_pending_check:
            self._validate_pending_pause_transition(existing, run_id)
        elif existing.governance.decision == GovernanceDecision.DENY:
            raise GovernanceError(
                f"Cannot approve or execute run '{run_id}' because governance decision is DENY."
            )

        pending_event = self._agent1_events.get(run_id) or self.orchestrator._pending_events.get(
            run_id
        )
        if pending_event is None:
            raise ApplicationError(f"Missing Agent 1 event for run_id '{run_id}'.")

        updated_result, sess2 = self._invoke_agent2_step(
            pending_event,
            simulated_approval_decision=decision,
            approver_reference=approver_reference,
            approval_comments=comments,
        )
        prev_meta = self._runtime_metadata.get(run_id, {})
        self._store_run_artifacts(
            event=pending_event,
            result=updated_result,
            agent1_session_id=prev_meta.get("agent1_session_id"),
            agent2_session_id=sess2,
        )

        # Append post-approval SSE events to the run's event stream for reconnect/replay
        prior_events = self.event_store.list_by_run_id(run_id)
        last_seq = prior_events[-1].sequence if prior_events else 0
        builder = WorkflowSSEBuilder(
            run_id=run_id,
            trace_id=updated_result.trace_id,
            store=self.event_store,
            start_sequence=last_seq,
        )
        cw_count = len(self._cloudwatch_events.get(run_id, []))
        for _ in emit_agent2_completion_events(
            builder,
            updated_result,
            cloudwatch_event_count=cw_count,
            include_pre_governance=False,
        ):
            pass

        return updated_result.to_command_center_response()

    def replay_stream_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
    ) -> list[WorkflowSSEEvent]:
        """Return stored SSE events for a run_id after Last-Event-ID without rerunning agents."""
        events = self.event_store.list_by_run_id(run_id)
        if not events:
            raise ApplicationError(f"No SSE event stream found for run_id '{run_id}'.")
        if last_event_id and last_event_id.strip():
            after_seq = self.event_store.resolve_sequence_for_last_event_id(run_id, last_event_id)
            return self.event_store.get_after_sequence(run_id, after_seq)
        return events

    def get_dashboard_state(self, run_id: str) -> CommandCenterView:
        res = self._agent2_results.get(run_id)
        if res is not None:
            return res.to_command_center_response()
        return self.orchestrator.get_command_center_view(run_id)

    def get_enriched_run_response(self, run_id: str) -> dict[str, Any]:
        """Return the Command Center contract enriched with Agent 1/Agent 2 results & CW counts."""
        view = self.get_dashboard_state(run_id)
        payload: dict[str, Any] = json.loads(view.model_dump_json())

        a1_event = self._agent1_events.get(run_id) or self.orchestrator._pending_events.get(run_id)
        a2_result = self._agent2_results.get(run_id) or self.orchestrator.get_result(run_id)
        cw_events = self._cloudwatch_events.get(run_id, [])

        a1_dict: dict[str, Any] | None = None
        if a1_event is not None:
            a1_dict = json.loads(a1_event.model_dump_json())
            a1_dict.pop("occurred_at", None)

        a2_dict: dict[str, Any] | None = None
        if a2_result is not None:
            a2_dict = json.loads(a2_result.model_dump_json())

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

        payload["run_id"] = view.header.run_id
        payload["trace_id"] = view.header.trace_id
        payload["overall_status"] = view.header.workflow_status
        payload["agent1_result"] = a1_dict
        payload["agent2_result"] = a2_dict
        payload["cloudwatch_event_counts"] = {
            "total": len(cw_events),
            "agent1": a1_cw,
            "agent2": a2_cw,
        }
        payload["evidence_links"] = {
            "evidence_api": f"/api/runs/{run_id}/evidence",
            "stream_api": f"/api/runs/{run_id}/stream",
            "evidence_bundle_dir": f"evidence/{run_id}/",
            "evidence_ids": [ev.evidence_id for ev in view.evidence],
        }
        return payload

    def get_evidence_bundle(
        self,
        *,
        run_id: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the complete sanitized evidence bundle for GET /api/runs/{run_id}/evidence."""
        records = self.view_evidence(run_id=run_id, trace_id=trace_id)
        if trace_id is not None and not records:
            return {
                "run_id": run_id,
                "trace_id": trace_id,
                "evidence_count": 0,
                "evidence": [],
                "normalized_evidence": [],
                "agent1_response": None,
                "agent2_response": None,
                "cloudwatch_events": [],
                "runtime_metadata": None,
                "workflow_summary": None,
            }

        a1_event = self._agent1_events.get(run_id) or self.orchestrator._pending_events.get(run_id)
        a2_result = self._agent2_results.get(run_id) or self.orchestrator.get_result(run_id)
        if a2_result is None:
            raise ApplicationError(f"No workflow result found for run_id '{run_id}'.")

        resolved_trace_id = trace_id or a2_result.trace_id
        a1_dict: dict[str, Any] = (
            json.loads(a1_event.model_dump_json()) if a1_event is not None else {}
        )
        a1_dict.pop("occurred_at", None)
        a2_dict: dict[str, Any] = json.loads(a2_result.model_dump_json())

        cw_events = self._cloudwatch_events.get(run_id, [])
        runtime_meta = self._runtime_metadata.get(
            run_id,
            {
                "run_id": run_id,
                "trace_id": resolved_trace_id,
                "data_mode": self.data_mode.value,
                "aws_region": self.settings.aws_region,
            },
        )

        normalized_index = build_evidence_index(
            run_id=run_id,
            trace_id=resolved_trace_id,
            agent1_response=a1_dict if a1_dict else None,
            agent2_response=a2_dict,
        )

        workflow_summary = {
            "run_id": run_id,
            "trace_id": resolved_trace_id,
            "overall_status": (
                a2_result.command_center.header.workflow_status
                if a2_result.command_center
                else "COMPLETED"
            ),
            "risk_assessment": a2_dict.get("risk_assessment"),
            "ai_reasoning": a2_dict.get("ai_reasoning"),
            "recommendation": a2_dict.get("recommendation"),
            "governance": a2_dict.get("governance"),
            "approval": a2_dict.get("approval"),
            "execution": a2_dict.get("execution"),
            "verification": a2_dict.get("verification"),
            "evidence_count": len(normalized_index),
            "cloudwatch_event_count": len(cw_events),
            "synthetic": True,
        }

        serialized_records = [json.loads(r.model_dump_json()) for r in records]
        return {
            "run_id": run_id,
            "trace_id": resolved_trace_id,
            "evidence_count": len(serialized_records),
            "evidence": serialized_records,
            "normalized_evidence": normalized_index,
            "agent1_response": a1_dict,
            "agent2_response": a2_dict,
            "cloudwatch_events": cw_events,
            "runtime_metadata": runtime_meta,
            "workflow_summary": workflow_summary,
        }

    def view_evidence(
        self,
        *,
        run_id: str,
        trace_id: str | None = None,
    ) -> list[EvidenceRecord]:
        """Return evidence records strictly associated with the selected run_id and trace_id."""
        res = self._agent2_results.get(run_id)
        if res is not None:
            if trace_id is not None and res.trace_id != trace_id:
                return []
            return list(res.evidence)
        return self.orchestrator.get_evidence(run_id=run_id, trace_id=trace_id)

    def get_workflow_stages(self, run_id: str) -> list[WorkflowStageState]:
        res = self._agent2_results.get(run_id)
        if res is not None:
            return list(res.workflow_stages)
        return self.orchestrator.get_workflow_stages(run_id)


AwsWorkflowOrchestrator = CommandCenterBackendService


def create_command_center_api(
    service: CommandCenterBackendService | None = None,
) -> Starlette:
    """Create the Starlette ASGI API application for the Command Center Backend."""
    backend_service = service or CommandCenterBackendService()

    async def create_run_endpoint(request: Request) -> JSONResponse:
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON body."},
                status_code=400,
            )
        try:
            scenario = body.get("scenario")
            if isinstance(scenario, str) and scenario.strip():
                view = backend_service.run_scenario(scenario.strip())
            else:
                view = backend_service.ingest_weather_event(body)
            enriched = backend_service.get_enriched_run_response(view.header.run_id)
            return JSONResponse(enriched, status_code=200)
        except ApplicationError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=400,
            )

    async def stream_create_run_endpoint(request: Request) -> StreamingResponse | JSONResponse:
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON body."},
                status_code=400,
            )

        last_event_id = request.headers.get("last-event-id")
        requested_run_id = body.get("run_id")

        async def _event_generator() -> AsyncIterator[bytes]:
            yield format_heartbeat_comment().encode("utf-8")
            # If reconnecting with Last-Event-ID for an already-recorded run, replay stored events
            # without rerunning Agent 1 or Agent 2.
            if (
                last_event_id
                and isinstance(requested_run_id, str)
                and backend_service.event_store.list_by_run_id(requested_run_id)
            ):
                replayed = backend_service.replay_stream_events(
                    requested_run_id, last_event_id=last_event_id
                )
                for ev in replayed:
                    if await request.is_disconnected():
                        break
                    yield format_sse_frame(ev).encode("utf-8")
                return

            for ev in backend_service.stream_workflow(body):
                if await request.is_disconnected():
                    break
                yield format_sse_frame(ev).encode("utf-8")

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers=dict(SSE_HEADERS),
        )

    async def stream_replay_run_endpoint(request: Request) -> StreamingResponse | JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        last_event_id = request.headers.get("last-event-id") or request.query_params.get(
            "last_event_id"
        )
        try:
            events = backend_service.replay_stream_events(run_id, last_event_id=last_event_id)
        except ApplicationError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=404,
            )

        async def _replay_generator() -> AsyncIterator[bytes]:
            yield format_heartbeat_comment().encode("utf-8")
            for ev in events:
                if await request.is_disconnected():
                    break
                yield format_sse_frame(ev).encode("utf-8")

        return StreamingResponse(
            _replay_generator(),
            media_type="text/event-stream",
            headers=dict(SSE_HEADERS),
        )

    async def get_run_endpoint(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        try:
            enriched = backend_service.get_enriched_run_response(run_id)
            return JSONResponse(enriched, status_code=200)
        except ApplicationError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=404,
            )

    async def approve_run_endpoint(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            body = {}
        approver = str(body.get("approver_reference", "demo-approver"))
        comments = str(body["comments"]) if body.get("comments") else None
        try:
            backend_service.submit_approval(
                run_id=run_id,
                decision=ApprovalStatus.APPROVED,
                approver_reference=approver,
                comments=comments,
                strict_pending_check=True,
            )
            enriched = backend_service.get_enriched_run_response(run_id)
            return JSONResponse(enriched, status_code=200)
        except GovernanceError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=409,
            )
        except ApplicationError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=404,
            )

    async def reject_run_endpoint(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        try:
            body: dict[str, Any] = await request.json()
        except ValueError:
            body = {}
        approver = str(body.get("approver_reference", "demo-approver"))
        comments = str(body["comments"]) if body.get("comments") else None
        try:
            backend_service.submit_approval(
                run_id=run_id,
                decision=ApprovalStatus.REJECTED,
                approver_reference=approver,
                comments=comments,
                strict_pending_check=True,
            )
            enriched = backend_service.get_enriched_run_response(run_id)
            return JSONResponse(enriched, status_code=200)
        except GovernanceError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=409,
            )
        except ApplicationError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=404,
            )

    async def get_run_evidence_endpoint(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        trace_id = request.query_params.get("trace_id")
        try:
            bundle = backend_service.get_evidence_bundle(
                run_id=run_id,
                trace_id=trace_id,
            )
            return JSONResponse(bundle, status_code=200)
        except ApplicationError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": sanitize_runtime_error_message(str(exc)),
                },
                status_code=404,
            )

    async def openapi_schema_endpoint(request: Request) -> JSONResponse:
        del request
        schema: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": "Network Resilience Command Center Backend API",
                "version": "0.1.0",
                "description": (
                    "Amazon Bedrock AgentCore multi-agent orchestration, governance, "
                    "SSE workflow streaming, and CloudWatch evidence API."
                ),
            },
            "paths": {
                "/api/runs": {
                    "post": {
                        "summary": "Create and execute a weather resilience workflow run (JSON)",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "example": {
                                        "scenario": "pause",
                                        "weather_event": "SEVERE_STORM",
                                        "severity": "HIGH",
                                        "affected_region": "Demo-Region-A",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {"description": "Enriched CommandCenterView JSON response"}
                        },
                    }
                },
                "/api/runs/stream": {
                    "post": {
                        "summary": "Stream workflow progress via Server-Sent Events (SSE)",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "example": {
                                        "scenario": "pause",
                                        "weather_event": "SEVERE_STORM",
                                        "severity": "HIGH",
                                        "affected_region": "Demo-Region-A",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {"description": "text/event-stream of WorkflowSSEEvent frames"}
                        },
                    }
                },
                "/api/runs/{run_id}": {
                    "get": {
                        "summary": "Get enriched workflow run state by run_id",
                        "parameters": [
                            {
                                "name": "run_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "Enriched run state"}},
                    }
                },
                "/api/runs/{run_id}/stream": {
                    "get": {
                        "summary": "Replay SSE workflow events (supports Last-Event-ID header)",
                        "parameters": [
                            {
                                "name": "run_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "last_event_id",
                                "in": "query",
                                "required": False,
                                "schema": {"type": "string"},
                            },
                        ],
                        "responses": {
                            "200": {"description": "text/event-stream replay of WorkflowSSEEvent"}
                        },
                    }
                },
                "/api/runs/{run_id}/approve": {
                    "post": {
                        "summary": "Approve a PAUSED + PENDING workflow run and execute action",
                        "parameters": [
                            {
                                "name": "run_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "requestBody": {
                            "required": False,
                            "content": {
                                "application/json": {
                                    "example": {
                                        "approver_reference": "demo-approver",
                                        "comments": "Approved by NOC operator",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {"description": "Updated run state after approval"},
                            "409": {"description": "Run is not in PAUSE + PENDING state"},
                        },
                    }
                },
                "/api/runs/{run_id}/reject": {
                    "post": {
                        "summary": "Reject a PAUSED + PENDING workflow run and keep action BLOCKED",
                        "parameters": [
                            {
                                "name": "run_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "requestBody": {
                            "required": False,
                            "content": {
                                "application/json": {
                                    "example": {
                                        "approver_reference": "demo-approver",
                                        "comments": "Rejected by NOC operator",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {"description": "Updated run state after rejection"},
                            "409": {"description": "Run is not in PAUSE + PENDING state"},
                        },
                    }
                },
                "/api/runs/{run_id}/evidence": {
                    "get": {
                        "summary": "Get full sanitized evidence bundle and CloudWatch logs",
                        "parameters": [
                            {
                                "name": "run_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "trace_id",
                                "in": "query",
                                "required": False,
                                "schema": {"type": "string"},
                            },
                        ],
                        "responses": {"200": {"description": "Complete evidence bundle"}},
                    }
                },
                "/health": {
                    "get": {
                        "summary": "Backend health and runtime data mode check",
                        "responses": {"200": {"description": "Health check response"}},
                    }
                },
            },
        }
        return JSONResponse(schema, status_code=200)

    async def health_endpoint(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "service": "command_center_backend",
                "data_mode": backend_service.data_mode.value,
                "aws_region": backend_service.settings.aws_region,
            },
            status_code=200,
        )

    async def swagger_ui_endpoint(request: Request) -> HTMLResponse:
        del request
        html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Network Resilience Command Center API - Swagger UI</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true
      });
    };
  </script>
</body>
</html>"""
        return HTMLResponse(html, status_code=200)

    routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/api/health", health_endpoint, methods=["GET"]),
        Route("/docs", swagger_ui_endpoint, methods=["GET"]),
        Route("/openapi.json", openapi_schema_endpoint, methods=["GET"]),
        Route("/api/runs", create_run_endpoint, methods=["POST"]),
        Route("/api/runs/stream", stream_create_run_endpoint, methods=["POST"]),
        Route("/api/runs/{run_id}", get_run_endpoint, methods=["GET"]),
        Route("/api/runs/{run_id}/stream", stream_replay_run_endpoint, methods=["GET"]),
        Route("/api/runs/{run_id}/approve", approve_run_endpoint, methods=["POST"]),
        Route("/api/runs/{run_id}/reject", reject_run_endpoint, methods=["POST"]),
        Route("/api/runs/{run_id}/evidence", get_run_evidence_endpoint, methods=["GET"]),
    ]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    return Starlette(routes=routes, middleware=middleware)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the Network Resilience Command Center Backend API server."
    )
    parser.add_argument(
        "--host",
        default=os.getenv("COMMAND_CENTER_HOST", "127.0.0.1"),
        help="Host to bind (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("COMMAND_CENTER_PORT", "8000")),
        help="Port to bind (default: 8000).",
    )
    parser.add_argument(
        "--data-mode",
        choices=["mock", "local", "aws_live"],
        default=os.getenv("COMMAND_CENTER_DATA_MODE", "mock"),
        help="Backend data mode: mock/local or aws_live.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    svc = CommandCenterBackendService(data_mode=args.data_mode)
    asgi_app = create_command_center_api(svc)
    uvicorn.run(asgi_app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()


__all__ = [
    "AGENT1_DEFAULT_LOG_GROUP",
    "AwsWorkflowOrchestrator",
    "CommandCenterBackendService",
    "CommandCenterDataMode",
    "create_command_center_api",
    "discover_agent2_log_group",
    "fetch_correlated_cloudwatch_events",
    "write_run_evidence_bundle",
]
