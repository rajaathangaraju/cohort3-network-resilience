"""Controlled mock remediation adapter boundary.

This adapter is strictly SYNTHETIC and NEVER makes real network changes.
"""

from datetime import UTC, datetime
from typing import Protocol

from common.constants import (
    RECOMMENDATION_TO_SIMULATED_ACTION,
    SUPPORTED_SIMULATED_ACTIONS,
)
from common.tracing import generate_prefixed_id
from contracts.action_result import ActionResult, ExecutionStatus
from contracts.governance_decision import ApprovalStatus, GovernanceDecision


class RemediationApiProvider(Protocol):
    """Protocol for controlled remediation adapters."""

    def execute_remediation(
        self,
        *,
        run_id: str,
        trace_id: str,
        recommendation_id: str,
        action: str,
        target_network_elements: list[str],
        governance_decision: GovernanceDecision,
        approval_status: ApprovalStatus,
        state_changing: bool,
        human_approval_required: bool,
    ) -> ActionResult: ...


class MockRemediationAdapter:
    """Controlled mock remediation adapter that simulates actions without touching real networks."""

    def __init__(self, *, simulate_failure: bool = False) -> None:
        self.simulate_failure = simulate_failure

    def execute_remediation(
        self,
        *,
        run_id: str,
        trace_id: str,
        recommendation_id: str,
        action: str,
        target_network_elements: list[str],
        governance_decision: GovernanceDecision,
        approval_status: ApprovalStatus,
        state_changing: bool,
        human_approval_required: bool,
    ) -> ActionResult:
        action_id = generate_prefixed_id("ACT")
        simulated_action = RECOMMENDATION_TO_SIMULATED_ACTION.get(action, action)

        # Enforce governance & approval gate invariants
        if governance_decision == GovernanceDecision.DENY:
            return ActionResult(
                action_id=action_id,
                run_id=run_id,
                trace_id=trace_id,
                recommendation_id=recommendation_id,
                action=simulated_action,
                target_network_elements=list(target_network_elements),
                governance_decision=governance_decision,
                execution_status=ExecutionStatus.BLOCKED,
                started_at=None,
                completed_at=None,
                result_message="Execution blocked by DENY governance decision.",
                changed_state=False,
                synthetic=True,
            )

        requires_gate = (
            state_changing
            or human_approval_required
            or governance_decision == GovernanceDecision.PAUSE
        )
        if requires_gate and approval_status != ApprovalStatus.APPROVED:
            return ActionResult(
                action_id=action_id,
                run_id=run_id,
                trace_id=trace_id,
                recommendation_id=recommendation_id,
                action=simulated_action,
                target_network_elements=list(target_network_elements),
                governance_decision=governance_decision,
                execution_status=ExecutionStatus.BLOCKED,
                started_at=None,
                completed_at=None,
                result_message=(
                    f"Execution blocked: human approval status is {approval_status.value}."
                ),
                changed_state=False,
                synthetic=True,
            )

        if simulated_action not in SUPPORTED_SIMULATED_ACTIONS:
            started_at = datetime.now(UTC)
            return ActionResult(
                action_id=action_id,
                run_id=run_id,
                trace_id=trace_id,
                recommendation_id=recommendation_id,
                action=simulated_action,
                target_network_elements=list(target_network_elements),
                governance_decision=governance_decision,
                execution_status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                result_message=f"Unsupported simulated action '{simulated_action}'.",
                changed_state=False,
                synthetic=True,
            )

        started_at = datetime.now(UTC)
        if self.simulate_failure:
            return ActionResult(
                action_id=action_id,
                run_id=run_id,
                trace_id=trace_id,
                recommendation_id=recommendation_id,
                action=simulated_action,
                target_network_elements=list(target_network_elements),
                governance_decision=governance_decision,
                execution_status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                result_message="Simulated controlled remediation failure (synthetic).",
                changed_state=False,
                synthetic=True,
            )

        completed_at = datetime.now(UTC)
        return ActionResult(
            action_id=action_id,
            run_id=run_id,
            trace_id=trace_id,
            recommendation_id=recommendation_id,
            action=simulated_action,
            target_network_elements=list(target_network_elements),
            governance_decision=governance_decision,
            execution_status=ExecutionStatus.EXECUTED,
            started_at=started_at,
            completed_at=completed_at,
            result_message=(
                f"Synthetic action '{simulated_action}' executed for targets "
                f"{list(target_network_elements)}."
            ),
            changed_state=state_changing,
            synthetic=True,
        )


MockRemediationApiClient = MockRemediationAdapter
