"""Local/mock human approval workflow boundary for PAUSE governance decisions.

No real personal information is stored; synthetic approver references (e.g., 'demo-approver')
are used. Approval must always be explicit—no default approval is permitted.
"""

from datetime import UTC, datetime

from common.exceptions import GovernanceError
from common.tracing import generate_prefixed_id
from contracts.governance_decision import (
    ApprovalOutcomeSummary,
    ApprovalRecord,
    ApprovalStatus,
    GovernanceDecision,
)


class ApprovalWorkflow:
    """Manages explicit human approval records for governed recommendations."""

    def create_for_governance(
        self,
        *,
        run_id: str,
        trace_id: str,
        recommendation_id: str,
        governance_decision: GovernanceDecision,
    ) -> ApprovalRecord:
        """Initialize the approval record corresponding to a governance decision."""
        approval_id = generate_prefixed_id("APR")
        now = datetime.now(UTC)

        if governance_decision in {GovernanceDecision.DENY, GovernanceDecision.ALLOW}:
            return ApprovalRecord(
                approval_id=approval_id,
                run_id=run_id,
                trace_id=trace_id,
                recommendation_id=recommendation_id,
                requested_at=now,
                decided_at=now,
                decision=ApprovalStatus.NOT_REQUIRED,
                approver_reference=None,
                comments="Human approval is not required for this governance decision.",
            )

        return ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            trace_id=trace_id,
            recommendation_id=recommendation_id,
            requested_at=now,
            decided_at=None,
            decision=ApprovalStatus.PENDING,
            approver_reference=None,
            comments="Awaiting explicit human approval.",
        )

    def apply_decision(
        self,
        record: ApprovalRecord,
        *,
        decision: ApprovalStatus,
        approver_reference: str = "demo-approver",
        comments: str | None = None,
    ) -> ApprovalRecord:
        """Record an explicit human decision (APPROVED, REJECTED, EXPIRED) on a record."""
        if decision not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.PENDING,
        }:
            raise GovernanceError(f"Invalid explicit approval decision: {decision}")

        if decision == ApprovalStatus.PENDING:
            return record

        if not approver_reference or not approver_reference.strip():
            raise GovernanceError("approver_reference must not be empty when recording a decision")

        return ApprovalRecord(
            approval_id=record.approval_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            recommendation_id=record.recommendation_id,
            requested_at=record.requested_at,
            decided_at=datetime.now(UTC),
            decision=decision,
            approver_reference=approver_reference.strip(),
            comments=comments
            or f"Explicit {decision.value} recorded by synthetic approver '{approver_reference}'.",
        )

    @staticmethod
    def to_summary(record: ApprovalRecord) -> ApprovalOutcomeSummary:
        return ApprovalOutcomeSummary(
            status=record.decision,
            approval_id=record.approval_id,
            requested_at=record.requested_at,
            decided_at=record.decided_at,
            approver_reference=record.approver_reference,
            comments=record.comments,
        )


ApprovalGate = ApprovalWorkflow
