"""Controlled action orchestration and synthetic post-action verification service for Agent 2.

All actions and verifications are strictly synthetic and never modify real network elements.
"""

from datetime import UTC, datetime

from common.tracing import generate_prefixed_id
from contracts.action_result import (
    ActionResult,
    ExecutionOutcomeSummary,
    ExecutionStatus,
    VerificationOutcomeSummary,
    VerificationResult,
    VerificationStatus,
)
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from contracts.recommendation import PreventiveRecommendation
from governance.decisions import can_execute_action
from integrations.remediation_api import MockRemediationAdapter, RemediationApiProvider


class SyntheticVerificationService:
    """Performs synthetic post-action verification without real network state inspection."""

    def __init__(self, *, simulate_verification_failure: bool = False) -> None:
        self.simulate_verification_failure = simulate_verification_failure

    def verify(
        self,
        *,
        run_id: str,
        trace_id: str,
        action_result: ActionResult,
        governance_decision: GovernanceDecision,
        approval_status: ApprovalStatus,
    ) -> VerificationResult:
        verification_id = generate_prefixed_id("VER")

        if governance_decision == GovernanceDecision.DENY:
            return VerificationResult(
                verification_id=verification_id,
                run_id=run_id,
                trace_id=trace_id,
                action_id=action_result.action_id,
                action_result_verified=False,
                asset_state="UNCHANGED_SYNTHETIC",
                post_action_check="SKIPPED_DUE_TO_GOVERNANCE_DENY",
                outcome_verified=False,
                verification_status=VerificationStatus.BLOCKED,
                verification_reasons=[
                    "Synthetic verification blocked because governance decision was DENY."
                ],
                verified_at=None,
                synthetic=True,
            )

        if approval_status in {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
            return VerificationResult(
                verification_id=verification_id,
                run_id=run_id,
                trace_id=trace_id,
                action_id=action_result.action_id,
                action_result_verified=False,
                asset_state="UNCHANGED_SYNTHETIC",
                post_action_check=f"SKIPPED_DUE_TO_APPROVAL_{approval_status.value}",
                outcome_verified=False,
                verification_status=VerificationStatus.BLOCKED,
                verification_reasons=[
                    (
                        "Synthetic verification blocked because human approval status "
                        f"was {approval_status.value}."
                    )
                ],
                verified_at=None,
                synthetic=True,
            )

        if (
            governance_decision == GovernanceDecision.PAUSE
            and approval_status == ApprovalStatus.PENDING
        ):
            return VerificationResult(
                verification_id=verification_id,
                run_id=run_id,
                trace_id=trace_id,
                action_id=action_result.action_id,
                action_result_verified=False,
                asset_state="AWAITING_APPROVAL_SYNTHETIC",
                post_action_check="NOT_STARTED_PENDING_HUMAN_APPROVAL",
                outcome_verified=False,
                verification_status=VerificationStatus.NOT_STARTED,
                verification_reasons=[
                    (
                        "Synthetic verification has not started because action is paused "
                        "awaiting human approval."
                    )
                ],
                verified_at=None,
                synthetic=True,
            )

        if action_result.execution_status == ExecutionStatus.BLOCKED:
            return VerificationResult(
                verification_id=verification_id,
                run_id=run_id,
                trace_id=trace_id,
                action_id=action_result.action_id,
                action_result_verified=False,
                asset_state="UNCHANGED_SYNTHETIC",
                post_action_check="BLOCKED_EXECUTION",
                outcome_verified=False,
                verification_status=VerificationStatus.BLOCKED,
                verification_reasons=[
                    "Synthetic verification blocked because action execution was BLOCKED."
                ],
                verified_at=None,
                synthetic=True,
            )

        if action_result.execution_status == ExecutionStatus.FAILED:
            return VerificationResult(
                verification_id=verification_id,
                run_id=run_id,
                trace_id=trace_id,
                action_id=action_result.action_id,
                action_result_verified=False,
                asset_state="DEGRADED_OR_ROLLBACK_SYNTHETIC",
                post_action_check="FAILED_ACTION_EXECUTION",
                outcome_verified=False,
                verification_status=VerificationStatus.FAILED,
                verification_reasons=[
                    (
                        "Synthetic verification failed because action execution failed: "
                        f"{action_result.result_message}"
                    )
                ],
                verified_at=datetime.now(UTC),
                synthetic=True,
            )

        if self.simulate_verification_failure:
            return VerificationResult(
                verification_id=verification_id,
                run_id=run_id,
                trace_id=trace_id,
                action_id=action_result.action_id,
                action_result_verified=True,
                asset_state="POST_ACTION_CHECK_FAILED_SYNTHETIC",
                post_action_check="SYNTHETIC_HEALTH_CHECK_FAILED",
                outcome_verified=False,
                verification_status=VerificationStatus.FAILED,
                verification_reasons=[
                    "Simulated post-action verification check failure on synthetic target elements."
                ],
                verified_at=datetime.now(UTC),
                synthetic=True,
            )

        return VerificationResult(
            verification_id=verification_id,
            run_id=run_id,
            trace_id=trace_id,
            action_id=action_result.action_id,
            action_result_verified=True,
            asset_state="STABLE_SYNTHETIC",
            post_action_check="SYNTHETIC_POST_CHECK_OK",
            outcome_verified=True,
            verification_status=VerificationStatus.PASSED,
            verification_reasons=[
                f"Synthetic post-action check passed for simulated action '{action_result.action}'."
            ],
            verified_at=datetime.now(UTC),
            synthetic=True,
        )

    @staticmethod
    def to_summary(result: VerificationResult) -> VerificationOutcomeSummary:
        return VerificationOutcomeSummary(
            status=result.verification_status,
            verification_id=result.verification_id,
            action_id=result.action_id,
            action_result_verified=result.action_result_verified,
            asset_state=result.asset_state,
            post_action_check=result.post_action_check,
            outcome_verified=result.outcome_verified,
            verification_reasons=list(result.verification_reasons),
            verified_at=result.verified_at,
            synthetic=True,
        )


class ActionService:
    """Coordinates governed action execution and synthetic verification."""

    def __init__(
        self,
        remediation_adapter: RemediationApiProvider | None = None,
        verification_service: SyntheticVerificationService | None = None,
    ) -> None:
        self.remediation_adapter: RemediationApiProvider = (
            remediation_adapter or MockRemediationAdapter()
        )
        self.verification_service = verification_service or SyntheticVerificationService()

    def execute(
        self,
        *,
        run_id: str,
        trace_id: str,
        recommendation: PreventiveRecommendation,
        governance_decision: GovernanceDecision,
        approval_status: ApprovalStatus,
    ) -> ActionResult:
        if not can_execute_action(
            governance_decision,
            approval_status,
            state_changing=recommendation.state_changing,
            human_approval_required=recommendation.human_approval_required,
        ):
            return self.remediation_adapter.execute_remediation(
                run_id=run_id,
                trace_id=trace_id,
                recommendation_id=recommendation.recommendation_id,
                action=recommendation.action,
                target_network_elements=recommendation.target_network_elements,
                governance_decision=governance_decision,
                approval_status=approval_status,
                state_changing=recommendation.state_changing,
                human_approval_required=recommendation.human_approval_required,
            )

        return self.remediation_adapter.execute_remediation(
            run_id=run_id,
            trace_id=trace_id,
            recommendation_id=recommendation.recommendation_id,
            action=recommendation.action,
            target_network_elements=recommendation.target_network_elements,
            governance_decision=governance_decision,
            approval_status=approval_status,
            state_changing=recommendation.state_changing,
            human_approval_required=recommendation.human_approval_required,
        )

    def verify(
        self,
        *,
        run_id: str,
        trace_id: str,
        action_result: ActionResult,
        governance_decision: GovernanceDecision,
        approval_status: ApprovalStatus,
    ) -> VerificationResult:
        return self.verification_service.verify(
            run_id=run_id,
            trace_id=trace_id,
            action_result=action_result,
            governance_decision=governance_decision,
            approval_status=approval_status,
        )

    @staticmethod
    def to_execution_summary(result: ActionResult) -> ExecutionOutcomeSummary:
        return ExecutionOutcomeSummary(
            status=result.execution_status,
            action_id=result.action_id,
            action=result.action,
            target_network_elements=list(result.target_network_elements),
            started_at=result.started_at,
            completed_at=result.completed_at,
            result_message=result.result_message,
            changed_state=result.changed_state,
            synthetic=True,
        )
