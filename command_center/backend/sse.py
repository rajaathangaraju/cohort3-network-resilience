"""Workflow-level Server-Sent Events (SSE) store, event builder, and streaming helpers.

Streams validated workflow progress and domain events without streaming arbitrary LLM tokens.
Agent 1 and Agent 2 continue returning complete typed JSON contracts (NetworkRiskEvent
and Agent2Result).
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from common.logging import log_agent_operation
from common.tracing import generate_prefixed_id
from contracts.action_result import (
    Agent2Result,
    ExecutionStatus,
    VerificationStatus,
    WorkflowSSEEvent,
)
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from contracts.network_risk_event import NetworkRiskEvent
from integrations.agentcore import sanitize_runtime_error_message

SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse_frame(event: WorkflowSSEEvent) -> str:
    """Format a typed WorkflowSSEEvent as a standard HTTP Server-Sent Event frame."""
    data_json = event.model_dump_json()
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {data_json}\n\n"


def format_heartbeat_comment(comment: str = "heartbeat") -> str:
    """Format a transport-only SSE heartbeat comment (does not affect sequence numbers)."""
    return f": {comment}\n\n"


class WorkflowEventStore(Protocol):
    """Lightweight event store interface for SSE replay and Last-Event-ID reconnection."""

    def append(self, event: WorkflowSSEEvent) -> None: ...

    def list_by_run_id(self, run_id: str) -> list[WorkflowSSEEvent]: ...

    def get_after_sequence(self, run_id: str, sequence: int) -> list[WorkflowSSEEvent]: ...

    def resolve_sequence_for_last_event_id(self, run_id: str, last_event_id: str) -> int: ...

    def mark_complete(self, run_id: str) -> None: ...

    def is_complete(self, run_id: str) -> bool: ...


class InMemoryWorkflowEventStore:
    """In-memory implementation of WorkflowEventStore for replay and reconnection."""

    def __init__(self) -> None:
        self._events: dict[str, list[WorkflowSSEEvent]] = {}
        self._completed: set[str] = set()

    def append(self, event: WorkflowSSEEvent) -> None:
        bucket = self._events.setdefault(event.run_id, [])
        bucket.append(event)
        if event.terminal:
            self._completed.add(event.run_id)

    def list_by_run_id(self, run_id: str) -> list[WorkflowSSEEvent]:
        return list(self._events.get(run_id, []))

    def get_after_sequence(self, run_id: str, sequence: int) -> list[WorkflowSSEEvent]:
        return [ev for ev in self._events.get(run_id, []) if ev.sequence > sequence]

    def resolve_sequence_for_last_event_id(self, run_id: str, last_event_id: str) -> int:
        cleaned = last_event_id.strip()
        if not cleaned:
            return 0
        if cleaned.isdigit():
            return int(cleaned)
        for ev in self._events.get(run_id, []):
            if ev.event_id == cleaned:
                return ev.sequence
        return 0

    def mark_complete(self, run_id: str) -> None:
        self._completed.add(run_id)

    def is_complete(self, run_id: str) -> bool:
        return run_id in self._completed


class WorkflowSSEBuilder:
    """Builds strictly ordered WorkflowSSEEvent objects and logs each business transition."""

    def __init__(
        self,
        *,
        run_id: str,
        trace_id: str,
        store: WorkflowEventStore,
        schema_version: str = "0.1",
        start_sequence: int = 0,
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self.store = store
        self.schema_version = schema_version
        self._sequence = start_sequence
        self.logger = structlog.get_logger("workflow_sse")

    @property
    def current_sequence(self) -> int:
        return self._sequence

    def emit(
        self,
        *,
        event_type: str,
        stage: str,
        status: str,
        source: str,
        message: str,
        data: dict[str, Any] | None = None,
        terminal: bool = False,
        synthetic: bool = True,
    ) -> WorkflowSSEEvent:
        self._sequence += 1
        event_id = generate_prefixed_id("EVT")
        event = WorkflowSSEEvent(
            schema_version=self.schema_version,
            event_id=event_id,
            event_type=event_type,
            sequence=self._sequence,
            run_id=self.run_id,
            trace_id=self.trace_id,
            timestamp=datetime.now(UTC),
            stage=stage,
            status=status,
            source=source,
            message=message,
            data=data or {},
            terminal=terminal,
            synthetic=synthetic,
        )
        self.store.append(event)
        log_agent_operation(
            self.logger,
            run_id=self.run_id,
            trace_id=self.trace_id,
            agent_name="workflow_sse_orchestrator",
            operation="sse_event_emitted",
            result=status,
            event_id=event_id,
            event_type=event_type,
            sequence=self._sequence,
            stage=stage,
            status=status,
        )
        return event


def emit_agent1_completion_events(
    builder: WorkflowSSEBuilder,
    risk_event: NetworkRiskEvent,
) -> Iterator[WorkflowSSEEvent]:
    """Emit ordered Agent 1 stage completion events from a validated NetworkRiskEvent."""
    yield builder.emit(
        event_type="weather_completed",
        stage="WEATHER",
        status="COMPLETED",
        source="weather_network_intelligence",
        message=f"Weather alert '{risk_event.weather_event}' ({risk_event.severity}) validated.",
        data={
            "weather_event": risk_event.weather_event,
            "severity": risk_event.severity,
            "affected_region": risk_event.affected_region,
        },
    )
    yield builder.emit(
        event_type="geo_correlation_completed",
        stage="GEO_CORRELATION",
        status="COMPLETED",
        source="weather_network_intelligence",
        message=f"Geographic correlation completed for region '{risk_event.affected_region}'.",
        data={
            "affected_region": risk_event.affected_region,
            "affected_area": (
                risk_event.affected_area.model_dump(mode="json")
                if risk_event.affected_area
                else None
            ),
        },
    )
    yield builder.emit(
        event_type="asset_mapping_completed",
        stage="ASSET_IMPACT",
        status="COMPLETED",
        source="weather_network_intelligence",
        message=(
            f"Mapped {len(risk_event.affected_network_elements)} network element(s) "
            f"in '{risk_event.affected_region}'."
        ),
        data={
            "affected_ne_count": len(risk_event.affected_network_elements),
            "affected_network_elements": list(risk_event.affected_network_elements),
        },
    )
    yield builder.emit(
        event_type="agent1_completed",
        stage="ASSET_IMPACT",
        status="COMPLETED",
        source="weather_network_intelligence",
        message="Agent 1 completed NetworkRiskEvent generation.",
        data={
            "weather_event": risk_event.weather_event,
            "severity": risk_event.severity,
            "affected_region": risk_event.affected_region,
            "affected_ne_count": len(risk_event.affected_network_elements),
            "affected_network_elements": list(risk_event.affected_network_elements),
        },
    )


def emit_agent2_completion_events(
    builder: WorkflowSSEBuilder,
    result: Agent2Result,
    *,
    cloudwatch_event_count: int = 0,
    include_pre_governance: bool = True,
) -> Iterator[WorkflowSSEEvent]:
    """Translate authoritative Agent2Result sections into ordered WorkflowSSEEvent objects."""
    if include_pre_governance:
        yield builder.emit(
            event_type="inventory_enriched",
            stage="ASSET_IMPACT",
            status=(
                "COMPLETED" if not result.unresolved_network_elements else "PARTIAL_UNRESOLVED"
            ),
            source="network_resilience",
            message=(
                f"Enriched {len(result.affected_assets)} network element(s); "
                f"unresolved={result.unresolved_network_elements}."
            ),
            data={
                "resolved_count": len(result.affected_assets),
                "unresolved_network_elements": list(result.unresolved_network_elements),
            },
        )
        yield builder.emit(
            event_type="service_impact_completed",
            stage="ASSET_IMPACT",
            status="COMPLETED",
            source="network_resilience",
            message=result.service_impact.impact_summary,
            data={
                "affected_service_count": result.service_impact.affected_service_count,
                "affected_services": list(result.service_impact.affected_services),
                "critical_network_element_count": (
                    result.service_impact.critical_network_element_count
                ),
                "service_risk": result.service_impact.service_risk,
            },
        )
        yield builder.emit(
            event_type="risk_assessed",
            stage="RISK_ASSESSMENT",
            status="COMPLETED",
            source="network_resilience",
            message=(
                f"Risk assessment completed: overall_risk={result.risk_assessment.overall_risk} "
                f"(score={result.risk_assessment.risk_score})."
            ),
            data={
                "risk_score": result.risk_assessment.risk_score,
                "overall_risk": result.risk_assessment.overall_risk,
                "weather_risk": result.risk_assessment.weather_risk,
                "network_risk": result.risk_assessment.network_risk,
                "service_risk": result.risk_assessment.service_risk,
                "risk_reasons": list(result.risk_assessment.risk_reasons),
            },
        )

        if result.ai_reasoning is not None:
            ai = result.ai_reasoning
            ai_msg = (
                "AI explanation used deterministic fallback."
                if ai.fallback_used
                else f"Bedrock reasoning completed via {ai.model_id}."
            )
            ai_data: dict[str, Any] = {
                "model_id": ai.model_id,
                "result_status": ai.result_status,
                "fallback_used": ai.fallback_used,
                "situation_summary": ai.reasoning.situation_summary,
                "risk_explanation": ai.reasoning.risk_explanation,
                "recommendation_rationale": ai.reasoning.recommendation_rationale,
                "operator_summary": ai.reasoning.operator_summary,
            }
            if ai.error_reason:
                ai_data["error_reason"] = ai.error_reason
            yield builder.emit(
                event_type="bedrock_reasoning_completed",
                stage="RECOMMENDATION",
                status="COMPLETED",
                source="network_resilience",
                message=ai_msg,
                data=ai_data,
            )

        rec = result.recommendation
        yield builder.emit(
            event_type="recommendation_generated",
            stage="RECOMMENDATION",
            status="COMPLETED",
            source="network_resilience",
            message=f"Generated preventive recommendation '{rec.action}'.",
            data={
                "recommendation_id": rec.recommendation_id,
                "action": rec.action,
                "target_network_elements": list(rec.target_network_elements),
                "rationale": rec.rationale,
                "expected_outcome": rec.expected_outcome,
                "state_changing": rec.state_changing,
                "human_approval_required": rec.human_approval_required,
                "confidence": rec.confidence,
            },
        )

        gov_decision = result.governance.decision
        gov_status_map = {
            GovernanceDecision.DENY: "BLOCKED",
            GovernanceDecision.ALLOW: "COMPLETED",
            GovernanceDecision.PAUSE: "PAUSED",
        }
        yield builder.emit(
            event_type="governance_decided",
            stage="GOVERNANCE",
            status=gov_status_map[gov_decision],
            source="PolicyEngine",
            message=f"Governance decision: {gov_decision.value} ({result.governance.reason})",
            data={
                "decision": gov_decision.value,
                "reason": result.governance.reason,
                "matched_policy_rules": list(result.governance.matched_policy_rules),
            },
        )

    gov_decision = result.governance.decision
    apr_status = result.approval.status
    exe_status = result.execution.status
    ver_status = result.verification.status

    # Emit only the approval/action/verification events that actually occurred
    if gov_decision == GovernanceDecision.DENY:
        yield builder.emit(
            event_type="action_blocked",
            stage="ACTION",
            status="BLOCKED",
            source="MockRemediationAdapter",
            message=result.execution.result_message or "Action blocked by governance DENY.",
            data={
                "approval": apr_status.value,
                "execution": exe_status.value,
                "action": result.execution.action,
            },
        )
        yield builder.emit(
            event_type="verification_blocked",
            stage="VERIFICATION",
            status="BLOCKED",
            source="SyntheticVerificationService",
            message="Post-action verification blocked by governance DENY.",
            data={
                "verification": ver_status.value,
                "reasons": list(result.verification.verification_reasons),
            },
        )
    elif gov_decision == GovernanceDecision.ALLOW:
        yield builder.emit(
            event_type="action_started",
            stage="ACTION",
            status="IN_PROGRESS",
            source="MockRemediationAdapter",
            message=f"Starting controlled mock action '{result.execution.action}'.",
            data={
                "action_id": result.execution.action_id,
                "action": result.execution.action,
                "target_network_elements": list(result.execution.target_network_elements),
            },
        )
        if exe_status == ExecutionStatus.EXECUTED:
            yield builder.emit(
                event_type="action_executed",
                stage="ACTION",
                status="COMPLETED",
                source="MockRemediationAdapter",
                message=result.execution.result_message or "Controlled mock action executed.",
                data={
                    "action_id": result.execution.action_id,
                    "action": result.execution.action,
                    "execution": exe_status.value,
                    "changed_state": result.execution.changed_state,
                },
            )
        else:
            yield builder.emit(
                event_type="action_blocked",
                stage="ACTION",
                status="BLOCKED",
                source="MockRemediationAdapter",
                message=result.execution.result_message or "Action did not execute.",
                data={"execution": exe_status.value},
            )

        yield builder.emit(
            event_type="verification_started",
            stage="VERIFICATION",
            status="IN_PROGRESS",
            source="SyntheticVerificationService",
            message="Starting synthetic post-action verification.",
            data={"verification_id": result.verification.verification_id},
        )
        if ver_status == VerificationStatus.PASSED:
            yield builder.emit(
                event_type="verification_passed",
                stage="VERIFICATION",
                status="COMPLETED",
                source="SyntheticVerificationService",
                message="Synthetic post-action verification passed.",
                data={
                    "verification": ver_status.value,
                    "asset_state": result.verification.asset_state,
                    "post_action_check": result.verification.post_action_check,
                },
            )
        else:
            yield builder.emit(
                event_type="verification_blocked",
                stage="VERIFICATION",
                status="BLOCKED",
                source="SyntheticVerificationService",
                message="Synthetic verification did not pass.",
                data={"verification": ver_status.value},
            )
    else:
        # PAUSE governance flow
        if apr_status == ApprovalStatus.PENDING:
            yield builder.emit(
                event_type="approval_pending",
                stage="APPROVAL",
                status="PAUSED",
                source="ApprovalWorkflow",
                message="Workflow paused awaiting explicit human approval.",
                data={
                    "approval_id": result.approval.approval_id,
                    "status": apr_status.value,
                },
            )
            yield builder.emit(
                event_type="action_blocked",
                stage="ACTION",
                status="BLOCKED",
                source="MockRemediationAdapter",
                message="Action execution is blocked while human approval is PENDING.",
                data={
                    "approval": apr_status.value,
                    "execution": exe_status.value,
                    "verification": ver_status.value,
                },
            )
        elif apr_status == ApprovalStatus.APPROVED:
            yield builder.emit(
                event_type="approval_approved",
                stage="APPROVAL",
                status="COMPLETED",
                source="ApprovalWorkflow",
                message=(
                    f"Explicit human approval granted by "
                    f"'{result.approval.approver_reference or 'demo-approver'}'."
                ),
                data={
                    "approval_id": result.approval.approval_id,
                    "status": apr_status.value,
                    "approver_reference": result.approval.approver_reference,
                },
            )
            yield builder.emit(
                event_type="action_started",
                stage="ACTION",
                status="IN_PROGRESS",
                source="MockRemediationAdapter",
                message=f"Starting controlled mock action '{result.execution.action}'.",
                data={
                    "action_id": result.execution.action_id,
                    "action": result.execution.action,
                    "target_network_elements": list(result.execution.target_network_elements),
                },
            )
            yield builder.emit(
                event_type="action_executed",
                stage="ACTION",
                status="COMPLETED",
                source="MockRemediationAdapter",
                message=result.execution.result_message or "Controlled mock action executed.",
                data={
                    "action_id": result.execution.action_id,
                    "action": result.execution.action,
                    "execution": exe_status.value,
                    "changed_state": result.execution.changed_state,
                },
            )
            yield builder.emit(
                event_type="verification_started",
                stage="VERIFICATION",
                status="IN_PROGRESS",
                source="SyntheticVerificationService",
                message="Starting synthetic post-action verification.",
                data={"verification_id": result.verification.verification_id},
            )
            yield builder.emit(
                event_type="verification_passed",
                stage="VERIFICATION",
                status="COMPLETED",
                source="SyntheticVerificationService",
                message="Synthetic post-action verification passed.",
                data={
                    "verification": ver_status.value,
                    "asset_state": result.verification.asset_state,
                    "post_action_check": result.verification.post_action_check,
                },
            )
        else:
            # REJECTED or EXPIRED
            yield builder.emit(
                event_type="approval_rejected",
                stage="APPROVAL",
                status="BLOCKED",
                source="ApprovalWorkflow",
                message=f"Human approval was {apr_status.value}; execution remains blocked.",
                data={
                    "approval_id": result.approval.approval_id,
                    "status": apr_status.value,
                    "approver_reference": result.approval.approver_reference,
                },
            )
            yield builder.emit(
                event_type="action_blocked",
                stage="ACTION",
                status="BLOCKED",
                source="MockRemediationAdapter",
                message=result.execution.result_message
                or f"Execution blocked because approval status is {apr_status.value}.",
                data={
                    "approval": apr_status.value,
                    "execution": exe_status.value,
                },
            )
            yield builder.emit(
                event_type="verification_blocked",
                stage="VERIFICATION",
                status="BLOCKED",
                source="SyntheticVerificationService",
                message=f"Verification blocked because approval status is {apr_status.value}.",
                data={
                    "verification": ver_status.value,
                    "reasons": list(result.verification.verification_reasons),
                },
            )

    yield builder.emit(
        event_type="evidence_captured",
        stage="EVIDENCE",
        status="COMPLETED",
        source="AuditRecorder",
        message=f"Captured {len(result.evidence)} ordered workflow evidence record(s).",
        data={
            "evidence_count": len(result.evidence),
            "cloudwatch_event_count": cloudwatch_event_count,
        },
    )
    yield builder.emit(
        event_type="agent2_completed",
        stage="EVIDENCE",
        status="COMPLETED",
        source="network_resilience",
        message="Agent 2 completed resilience analysis and governance evaluation.",
        data={
            "overall_risk": result.risk_assessment.overall_risk,
            "risk_score": result.risk_assessment.risk_score,
            "governance_decision": gov_decision.value,
            "approval_status": apr_status.value,
            "execution_status": exe_status.value,
            "verification_status": ver_status.value,
        },
    )

    workflow_status = (
        "AWAITING_APPROVAL"
        if (gov_decision == GovernanceDecision.PAUSE and apr_status == ApprovalStatus.PENDING)
        else (
            result.command_center.header.workflow_status if result.command_center else "COMPLETED"
        )
    )

    yield builder.emit(
        event_type="run_completed",
        stage="EVIDENCE",
        status=workflow_status,
        source="workflow_sse_orchestrator",
        message=f"Workflow stream completed with status={workflow_status}.",
        data={
            "run_id": result.run_id,
            "trace_id": result.trace_id,
            "workflow_status": workflow_status,
            "overall_risk": result.risk_assessment.overall_risk,
            "governance_decision": gov_decision.value,
            "approval_status": apr_status.value,
            "execution_status": exe_status.value,
            "verification_status": ver_status.value,
            "approval": apr_status.value,
            "execution": exe_status.value,
            "verification": ver_status.value,
            "evidence_count": len(result.evidence),
            "cloudwatch_event_count": cloudwatch_event_count,
        },
        terminal=True,
    )


def emit_failure_terminal_event(
    builder: WorkflowSSEBuilder,
    *,
    exc: Exception,
    failed_stage: str,
    retryable: bool = False,
) -> WorkflowSSEEvent:
    """Emit a sanitized terminal run_failed SSE event without leaking stack traces or secrets."""
    safe_msg = sanitize_runtime_error_message(str(exc))
    return builder.emit(
        event_type="run_failed",
        stage=failed_stage,
        status="FAILED",
        source="workflow_sse_orchestrator",
        message=f"Workflow failed at stage {failed_stage}: {safe_msg}",
        data={
            "error_type": type(exc).__name__,
            "safe_message": safe_msg,
            "failed_stage": failed_stage,
            "retryable": retryable,
        },
        terminal=True,
    )


async def encode_sse_stream(
    events: AsyncIterator[WorkflowSSEEvent],
    *,
    include_initial_heartbeat: bool = False,
) -> AsyncIterator[bytes]:
    """Encode an async iterator of WorkflowSSEEvent objects into UTF-8 SSE wire bytes."""
    if include_initial_heartbeat:
        yield format_heartbeat_comment().encode("utf-8")
    async for ev in events:
        yield format_sse_frame(ev).encode("utf-8")


__all__ = [
    "SSE_HEADERS",
    "InMemoryWorkflowEventStore",
    "WorkflowEventStore",
    "WorkflowSSEBuilder",
    "emit_agent1_completion_events",
    "emit_agent2_completion_events",
    "emit_failure_terminal_event",
    "encode_sse_stream",
    "format_heartbeat_comment",
    "format_sse_frame",
]
