"""Phase 3 End-to-End Orchestration and Command Center Backend Tests.

Covers:
1. Agent 1 -> Agent 2 propagation
2. run_id preservation
3. trace_id preservation
4. ALLOW scenario
5. DENY scenario
6. PAUSE scenario
7. PAUSE -> APPROVED -> execution
8. PAUSE -> REJECTED -> blocked
9. evidence generation
10. Command Center aggregation
11. workflow-stage transitions
"""

from command_center.backend import CommandCenterBackendService
from contracts.action_result import (
    ORDERED_WORKFLOW_STAGES,
    ExecutionStatus,
    VerificationStatus,
    WorkflowStage,
    WorkflowStageStatus,
)
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from orchestration.workflow import DemoScenario, EndToEndOrchestrator


def test_agent1_to_agent2_propagation_and_id_preservation() -> None:
    """Tests 1, 2, 3: Agent 1 -> Agent 2 propagation, run_id, trace_id, schema_version."""
    orchestrator = EndToEndOrchestrator()
    result = orchestrator.run_scenario(DemoScenario.PAUSE)

    assert result.schema_version == "0.1"
    assert result.team_code == "TEAM_PENDING"
    assert result.run_id == "WX-46329ae2b74b"
    assert result.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"

    cc = result.to_command_center_response()
    assert cc.header.run_id == "WX-46329ae2b74b"
    assert cc.header.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    assert cc.header.schema_version == "0.1"
    assert cc.header.team_code == "TEAM_PENDING"

    for evd in result.evidence:
        assert evd.run_id == "WX-46329ae2b74b"
        assert evd.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"


def test_scenario_allow_end_to_end() -> None:
    """Test 4: ALLOW scenario (non-state-changing safe monitoring action)."""
    orchestrator = EndToEndOrchestrator()
    result = orchestrator.run_scenario(DemoScenario.ALLOW)

    assert result.run_id == "WX-SCENARIO-ALLOW"
    assert result.trace_id == "TRACE-SCENARIO-ALLOW-0002"
    assert result.recommendation.action == "INCREASE_MONITORING"
    assert result.recommendation.state_changing is False
    assert result.governance.decision == GovernanceDecision.ALLOW
    assert result.approval.status == ApprovalStatus.NOT_REQUIRED
    assert result.execution.status == ExecutionStatus.EXECUTED
    assert result.verification.status == VerificationStatus.PASSED

    stages = {s.stage: s.status for s in result.workflow_stages}
    assert stages[WorkflowStage.GOVERNANCE] == WorkflowStageStatus.COMPLETED
    assert stages[WorkflowStage.APPROVAL] == WorkflowStageStatus.COMPLETED
    assert stages[WorkflowStage.ACTION] == WorkflowStageStatus.COMPLETED
    assert stages[WorkflowStage.VERIFICATION] == WorkflowStageStatus.COMPLETED
    assert stages[WorkflowStage.EVIDENCE] == WorkflowStageStatus.COMPLETED


def test_scenario_deny_end_to_end() -> None:
    """Test 5: DENY scenario (Recommendation -> DENY -> BLOCKED execution & verification)."""
    orchestrator = EndToEndOrchestrator()
    result = orchestrator.run_scenario(DemoScenario.DENY)

    assert result.run_id == "WX-SCENARIO-DENY"
    assert result.trace_id == "TRACE-SCENARIO-DENY-0001"
    assert result.governance.decision == GovernanceDecision.DENY
    assert result.approval.status == ApprovalStatus.NOT_REQUIRED
    assert result.execution.status == ExecutionStatus.BLOCKED
    assert result.verification.status == VerificationStatus.BLOCKED

    cc = result.to_command_center_response()
    assert cc.status_card is not None
    assert cc.status_card.policy_decision == GovernanceDecision.DENY
    assert cc.status_card.execution == ExecutionStatus.BLOCKED

    stages = {s.stage: s.status for s in result.workflow_stages}
    assert stages[WorkflowStage.GOVERNANCE] == WorkflowStageStatus.BLOCKED
    assert stages[WorkflowStage.ACTION] == WorkflowStageStatus.BLOCKED
    assert stages[WorkflowStage.VERIFICATION] == WorkflowStageStatus.BLOCKED
    assert stages[WorkflowStage.EVIDENCE] == WorkflowStageStatus.COMPLETED


def test_scenario_pause_and_explicit_approval_or_rejection() -> None:
    """Tests 6, 7, 8:
    - PAUSE scenario: state-changing recommendation -> PAUSE -> PENDING -> BLOCKED
    - PAUSE -> APPROVED -> EXECUTED -> PASSED verification
    - PAUSE -> REJECTED -> BLOCKED
    """
    orchestrator = EndToEndOrchestrator()
    paused_result = orchestrator.run_scenario(DemoScenario.PAUSE)

    # Verify initial PAUSE state before human approval
    assert paused_result.recommendation.state_changing is True
    assert paused_result.governance.decision == GovernanceDecision.PAUSE
    assert paused_result.approval.status == ApprovalStatus.PENDING
    assert paused_result.execution.status == ExecutionStatus.BLOCKED
    assert paused_result.verification.status == VerificationStatus.NOT_STARTED

    cc_paused = paused_result.to_command_center_response()
    assert cc_paused.status_card is not None
    assert cc_paused.status_card.policy_decision == GovernanceDecision.PAUSE
    assert cc_paused.status_card.human_approval == ApprovalStatus.PENDING
    assert cc_paused.status_card.execution == ExecutionStatus.BLOCKED

    stages_paused = {s.stage: s.status for s in paused_result.workflow_stages}
    assert stages_paused[WorkflowStage.GOVERNANCE] == WorkflowStageStatus.PAUSED
    assert stages_paused[WorkflowStage.APPROVAL] == WorkflowStageStatus.PAUSED
    assert stages_paused[WorkflowStage.ACTION] == WorkflowStageStatus.BLOCKED
    assert stages_paused[WorkflowStage.VERIFICATION] == WorkflowStageStatus.PENDING

    # Test 8: PAUSE -> REJECTED -> remains BLOCKED
    rejected_result = orchestrator.submit_human_approval(
        run_id=paused_result.run_id,
        decision=ApprovalStatus.REJECTED,
        approver_reference="demo-approver",
        comments="Rejected in end-to-end test.",
    )
    assert rejected_result.governance.decision == GovernanceDecision.PAUSE
    assert rejected_result.approval.status == ApprovalStatus.REJECTED
    assert rejected_result.execution.status == ExecutionStatus.BLOCKED
    assert rejected_result.verification.status == VerificationStatus.BLOCKED

    # Test 7: PAUSE -> APPROVED -> EXECUTED -> PASSED
    approved_result = orchestrator.submit_human_approval(
        run_id=paused_result.run_id,
        decision=ApprovalStatus.APPROVED,
        approver_reference="demo-approver",
        comments="Approved in end-to-end test.",
    )
    assert approved_result.governance.decision == GovernanceDecision.PAUSE
    assert approved_result.approval.status == ApprovalStatus.APPROVED
    assert approved_result.execution.status == ExecutionStatus.EXECUTED
    assert approved_result.verification.status == VerificationStatus.PASSED

    cc_approved = approved_result.to_command_center_response()
    assert cc_approved.status_card is not None
    assert cc_approved.status_card.policy_decision == GovernanceDecision.PAUSE
    assert cc_approved.status_card.human_approval == ApprovalStatus.APPROVED
    assert cc_approved.status_card.execution == ExecutionStatus.EXECUTED


def test_command_center_backend_aggregation_evidence_and_stage_transitions() -> None:
    """Tests 9, 10, 11: Evidence generation, Command Center, and workflow stages."""
    backend = CommandCenterBackendService()
    cc_view = backend.run_scenario(DemoScenario.PAUSE)

    assert cc_view.header.run_id == "WX-46329ae2b74b"
    assert cc_view.header.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    assert cc_view.header.workflow_status == "PAUSED_AWAITING_HUMAN_APPROVAL"
    assert [s.stage for s in cc_view.workflow_stages] == list(ORDERED_WORKFLOW_STAGES)

    # VIEW EVIDENCE filtered by run_id and trace_id
    evidence = backend.view_evidence(
        run_id="WX-46329ae2b74b",
        trace_id="7a4106b675e4419f87b9b84d30a67ca4",
    )
    assert len(evidence) == 10
    assert all(ev.synthetic is True for ev in evidence)
    assert all(ev.evidence_id.startswith("EVD-") for ev in evidence)

    # Mismatched trace_id returns no evidence
    assert (
        backend.view_evidence(
            run_id="WX-46329ae2b74b",
            trace_id="WRONG-TRACE-ID",
        )
        == []
    )

    # Approve through Command Center Backend and verify updated state
    approved_view = backend.submit_approval(
        run_id="WX-46329ae2b74b",
        decision=ApprovalStatus.APPROVED,
        approver_reference="demo-approver",
    )
    assert approved_view.header.workflow_status == "COMPLETED_SYNTHETIC_EXECUTION"
    assert approved_view.approval.status == ApprovalStatus.APPROVED
    assert approved_view.execution.status == ExecutionStatus.EXECUTED
    assert approved_view.verification.status == VerificationStatus.PASSED
