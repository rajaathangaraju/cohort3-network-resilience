"""Unit tests for governance policy engine, deny rules, and human approval workflow."""

import pytest

from common.exceptions import GovernanceError
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from contracts.recommendation import PreventiveRecommendation
from governance.approval import ApprovalWorkflow
from governance.decisions import can_execute_action, requires_approval
from governance.policy_engine import PolicyEngine


def test_policy_engine_allow_non_state_changing_action() -> None:
    engine = PolicyEngine()
    rec = PreventiveRecommendation(
        action="CONTINUE_MONITORING",
        target_network_elements=["NE-201"],
        rationale="Low risk monitoring.",
        state_changing=False,
        human_approval_required=False,
    )
    eval_result = engine.evaluate_recommendation(
        run_id="WX-1",
        trace_id="TRACE-1",
        recommendation=rec,
    )
    assert eval_result.governance_decision == GovernanceDecision.ALLOW
    assert eval_result.human_approval_required is False
    assert "POLICY-ALLOW-NON-STATE-CHANGING-201" in eval_result.matched_policy_rules


def test_policy_engine_pause_state_changing_action() -> None:
    engine = PolicyEngine()
    rec = PreventiveRecommendation(
        action="PREPARE_TRAFFIC_REROUTE",
        target_network_elements=["NE-101", "NE-102"],
        rationale="High risk with redundancy.",
        state_changing=True,
        human_approval_required=True,
    )
    eval_result = engine.evaluate_recommendation(
        run_id="WX-1",
        trace_id="TRACE-1",
        recommendation=rec,
    )
    assert eval_result.governance_decision == GovernanceDecision.PAUSE
    assert eval_result.human_approval_required is True
    assert "POLICY-PAUSE-STATE-CHANGING-101" in eval_result.matched_policy_rules


def test_policy_engine_deny_unsupported_action_or_unresolved_state_change() -> None:
    engine = PolicyEngine()
    rec = PreventiveRecommendation(
        action="REROUTE_TRAFFIC",
        target_network_elements=["NE-101", "NE-999"],
        rationale="Attempt reroute with unresolved element.",
        state_changing=True,
        human_approval_required=True,
    )
    eval_result = engine.evaluate_recommendation(
        run_id="WX-1",
        trace_id="TRACE-1",
        recommendation=rec,
        unresolved_elements=["NE-999"],
    )
    assert eval_result.governance_decision == GovernanceDecision.DENY
    assert "POLICY-DENY-UNRESOLVED-STATE-CHANGE-003" in eval_result.matched_policy_rules


def test_state_changing_action_cannot_execute_without_approval() -> None:
    assert (
        can_execute_action(
            GovernanceDecision.ALLOW,
            ApprovalStatus.NOT_REQUIRED,
            state_changing=True,
            human_approval_required=True,
        )
        is False
    )
    assert (
        can_execute_action(
            GovernanceDecision.PAUSE,
            ApprovalStatus.PENDING,
            state_changing=True,
            human_approval_required=True,
        )
        is False
    )
    assert (
        can_execute_action(
            GovernanceDecision.PAUSE,
            ApprovalStatus.APPROVED,
            state_changing=True,
            human_approval_required=True,
        )
        is True
    )
    assert requires_approval(GovernanceDecision.PAUSE, state_changing=True) is True
    assert requires_approval(GovernanceDecision.DENY, state_changing=True) is False


def test_approval_workflow_transitions() -> None:
    workflow = ApprovalWorkflow()
    pending_record = workflow.create_for_governance(
        run_id="WX-1",
        trace_id="TRACE-1",
        recommendation_id="REC-1",
        governance_decision=GovernanceDecision.PAUSE,
    )
    assert pending_record.decision == ApprovalStatus.PENDING
    assert pending_record.decided_at is None

    approved_record = workflow.apply_decision(
        pending_record,
        decision=ApprovalStatus.APPROVED,
        approver_reference="demo-approver",
        comments="Approved in synthetic test.",
    )
    assert approved_record.decision == ApprovalStatus.APPROVED
    assert approved_record.approver_reference == "demo-approver"
    assert approved_record.decided_at is not None

    rejected_record = workflow.apply_decision(
        pending_record,
        decision=ApprovalStatus.REJECTED,
        approver_reference="demo-approver",
    )
    assert rejected_record.decision == ApprovalStatus.REJECTED

    expired_record = workflow.apply_decision(
        pending_record,
        decision=ApprovalStatus.EXPIRED,
        approver_reference="demo-approver",
    )
    assert expired_record.decision == ApprovalStatus.EXPIRED

    with pytest.raises(GovernanceError):
        workflow.apply_decision(
            pending_record,
            decision=ApprovalStatus.APPROVED,
            approver_reference="   ",
        )
