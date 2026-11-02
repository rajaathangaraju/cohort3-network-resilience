"""Governance decision helpers and transition predicates."""

from contracts.governance_decision import ApprovalStatus, GovernanceDecision


def requires_approval(
    decision: GovernanceDecision,
    *,
    state_changing: bool = True,
    human_approval_required: bool = False,
) -> bool:
    """Return True if an action requires human approval before execution."""
    if decision == GovernanceDecision.DENY:
        return False
    return state_changing or human_approval_required or decision == GovernanceDecision.PAUSE


def can_execute_action(
    decision: GovernanceDecision,
    approval_status: ApprovalStatus,
    *,
    state_changing: bool,
    human_approval_required: bool,
) -> bool:
    """Enforce the governance invariant:
    - DENY never executes.
    - State-changing or approval-requiring recommendations NEVER proceed directly from ALLOW
      to EXECUTED without explicit APPROVED human approval.
    - ALLOW executes immediately only when non-state-changing and human_approval_required is False.
    """
    if decision == GovernanceDecision.DENY:
        return False

    if state_changing or human_approval_required or decision == GovernanceDecision.PAUSE:
        return approval_status == ApprovalStatus.APPROVED

    return decision == GovernanceDecision.ALLOW and approval_status in {
        ApprovalStatus.NOT_REQUIRED,
        ApprovalStatus.APPROVED,
    }
