"""Integration tests for governed action execution flows:
1. PAUSE -> APPROVED -> Mock execution -> Synthetic verification -> Command Center response
2. DENY -> BLOCKED execution -> BLOCKED verification -> Audit evidence
3. ALLOW (non-state-changing) -> EXECUTED -> PASSED verification
"""

from typing import Any

from agents.network_resilience.agent import NetworkResilienceAgent
from contracts.action_result import ExecutionStatus, VerificationStatus
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from tests.integration.test_agent1_agent2 import EXACT_AGENT1_INTEGRATION_PAYLOAD


def test_pause_to_approved_to_mock_execution_and_command_center_response() -> None:
    agent2 = NetworkResilienceAgent()
    result = agent2.process(
        EXACT_AGENT1_INTEGRATION_PAYLOAD,
        simulated_approval_decision=ApprovalStatus.APPROVED,
        approver_reference="demo-approver",
        approval_comments="Approved synthetic traffic reroute in integration test.",
    )

    assert result.governance.decision == GovernanceDecision.PAUSE
    assert result.approval.status == ApprovalStatus.APPROVED
    assert result.approval.approver_reference == "demo-approver"
    assert result.execution.status == ExecutionStatus.EXECUTED
    assert result.execution.action == "SIMULATE_TRAFFIC_REROUTE"
    assert result.execution.changed_state is True
    assert result.execution.synthetic is True
    assert result.verification.status == VerificationStatus.PASSED
    assert result.verification.outcome_verified is True
    assert result.verification.synthetic is True

    # Command Center aggregation contract verification
    cc = result.to_command_center_response()
    assert cc.header.run_id == "WX-46329ae2b74b"
    assert cc.header.overall_risk == "CRITICAL"
    assert cc.header.workflow_status == "COMPLETED_SYNTHETIC_EXECUTION"
    assert cc.weather.event == "SEVERE_STORM"
    assert cc.weather.severity == "HIGH"
    assert cc.weather.forecast == "Synthetic storm scenario for integration testing."
    assert cc.geo.region == "Demo-Region-A"
    assert cc.geo.geo_match_status == "MATCHED"
    assert cc.geo.asset_mapping_status == "COMPLETE"
    assert cc.assets.affected_ne_count == 2
    assert cc.assets.critical_ne_count == 1
    assert cc.assets.affected_service_count == 4
    assert cc.risk.risk_score == 85
    assert cc.recommendation.action == "REROUTE_TRAFFIC"
    assert cc.governance.decision == GovernanceDecision.PAUSE
    assert cc.approval.status == ApprovalStatus.APPROVED
    assert cc.execution.status == ExecutionStatus.EXECUTED
    assert cc.verification.status == VerificationStatus.PASSED
    assert len(cc.evidence) == 10
    assert len(cc.audit_trace) == 10


def test_deny_to_blocked_execution_and_blocked_verification_with_audit_evidence() -> None:
    agent2 = NetworkResilienceAgent()
    # Incomplete inventory triggers NO_AUTOMATED_ACTION + DENY policy rule
    incomplete_payload: dict[str, Any] = dict(EXACT_AGENT1_INTEGRATION_PAYLOAD)
    incomplete_payload["affected_network_elements"] = ["NE-101", "NE-UNRESOLVED-999"]

    result = agent2.process(incomplete_payload)

    assert result.unresolved_network_elements == ["NE-UNRESOLVED-999"]
    assert result.recommendation.action == "NO_AUTOMATED_ACTION"
    assert result.governance.decision == GovernanceDecision.DENY
    assert "POLICY-DENY-INCOMPLETE-INVENTORY-004" in result.governance.matched_policy_rules
    assert result.approval.status == ApprovalStatus.NOT_REQUIRED
    assert result.execution.status == ExecutionStatus.BLOCKED
    assert result.execution.synthetic is True
    assert result.verification.status == VerificationStatus.BLOCKED
    assert result.verification.synthetic is True

    stages = {e.stage: e.status for e in result.evidence}
    assert stages["governance_decision"] == "DENY"
    assert stages["action_execution"] == "BLOCKED"
    assert stages["verification"] == "BLOCKED"


def test_allow_non_state_changing_monitoring_flow() -> None:
    agent2 = NetworkResilienceAgent()
    monitoring_payload: dict[str, Any] = dict(EXACT_AGENT1_INTEGRATION_PAYLOAD)
    monitoring_payload["severity"] = "LOW"
    monitoring_payload["affected_region"] = "Region-B"
    monitoring_payload["affected_network_elements"] = ["NE-201", "NE-202"]

    result = agent2.process(monitoring_payload)

    assert result.risk_assessment.overall_risk == "MEDIUM"
    assert result.recommendation.action == "INCREASE_MONITORING"
    assert result.recommendation.state_changing is False
    assert result.recommendation.human_approval_required is False
    assert result.governance.decision == GovernanceDecision.ALLOW
    assert result.approval.status == ApprovalStatus.NOT_REQUIRED
    assert result.execution.status == ExecutionStatus.EXECUTED
    assert result.execution.action == "START_ENHANCED_MONITORING"
    assert result.verification.status == VerificationStatus.PASSED
