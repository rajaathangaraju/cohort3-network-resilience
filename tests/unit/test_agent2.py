"""Comprehensive unit tests for Agent 2 (Network Resilience & Governed Action Agent)."""

from typing import Any

import pytest

from agents.network_resilience.action_service import (
    ActionService,
    SyntheticVerificationService,
)
from agents.network_resilience.agent import NetworkResilienceAgent
from agents.network_resilience.recommendation_engine import RecommendationEngine
from agents.network_resilience.risk_analyzer import RiskAnalyzer
from agents.network_resilience.service_impact import ServiceImpactAnalyzer
from common.exceptions import InventoryLookupError, RegionMismatchError
from contracts.action_result import ExecutionStatus, VerificationStatus
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from contracts.recommendation import EnrichedNetworkElement
from integrations.inventory_api import (
    MockNetworkInventoryAdapter,
    enrich_network_elements,
)
from integrations.remediation_api import MockRemediationAdapter


def make_agent1_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": "WX-46329ae2b74b",
        "trace_id": "7a4106b675e4419f87b9b84d30a67ca4",
        "created_at": "2026-11-02T00:26:32.732111+00:00",
        "data_source": "synthetic",
        "weather_event": "SEVERE_STORM",
        "severity": "HIGH",
        "affected_region": "Demo-Region-A",
        "forecast": "Synthetic storm scenario for integration testing.",
        "affected_area": {
            "south": 12.8,
            "north": 13.2,
            "west": 77.4,
            "east": 77.8,
        },
        "affected_network_elements": ["NE-101", "NE-102"],
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------
# Inventory Enrichment Tests
# ------------------------------------------------------------
def test_inventory_all_elements_resolved() -> None:
    adapter = MockNetworkInventoryAdapter()
    result = enrich_network_elements(adapter, ["NE-101", "NE-102"], "Demo-Region-A")
    assert len(result.resolved_elements) == 2
    assert result.unresolved_element_ids == []


def test_inventory_one_unresolved_element_preserved() -> None:
    adapter = MockNetworkInventoryAdapter()
    result = enrich_network_elements(
        adapter,
        ["NE-101", "NE-UNKNOWN-999"],
        "Demo-Region-A",
    )
    assert [e.ne_id for e in result.resolved_elements] == ["NE-101"]
    assert result.unresolved_element_ids == ["NE-UNKNOWN-999"]


def test_inventory_adapter_failure_raises() -> None:
    adapter = MockNetworkInventoryAdapter(simulate_failure=True)
    with pytest.raises(InventoryLookupError):
        enrich_network_elements(adapter, ["NE-101"], "Demo-Region-A")


def test_inventory_region_mismatch_raises() -> None:
    adapter = MockNetworkInventoryAdapter()
    with pytest.raises(RegionMismatchError):
        enrich_network_elements(
            adapter,
            ["NE-201"],  # belongs to Region-B
            "Demo-Region-A",
            strict_region_check=True,
        )


# ------------------------------------------------------------
# Risk Assessment Tests (LOW, MEDIUM, HIGH, CRITICAL, Cap, Determinism)
# ------------------------------------------------------------
def test_risk_levels_low_medium_high_critical_and_cap() -> None:
    analyzer = RiskAnalyzer()
    impact_analyzer = ServiceImpactAnalyzer()
    adapter = MockNetworkInventoryAdapter()

    # 1. Baseline LOW weather (10) + LOW asset (5) + 1 elem (5) + 1 svc (5) = 25 (MEDIUM)
    low_catalog = {
        "NE-LOW": EnrichedNetworkElement(
            ne_id="NE-LOW",
            type="SENSOR",
            region="Region-Low",
            criticality="LOW",
            services=["TELEMETRY"],
            redundancy_available=True,
        )
    }
    enrichment_low = enrich_network_elements(
        MockNetworkInventoryAdapter(low_catalog), ["NE-LOW"], "Region-Low"
    )
    impact_low = impact_analyzer.analyze(weather_severity="LOW", enrichment=enrichment_low)
    # 10 (LOW weather) + 5 (LOW asset) + 5 (1 elem) + 5 (1 svc) = 25 (MEDIUM)
    assess_medium_from_low = analyzer.assess(
        weather_severity="LOW",
        total_requested_elements=1,
        enrichment=enrichment_low,
        service_impact=impact_low,
    )
    assert assess_medium_from_low.risk_score == 25
    assert assess_medium_from_low.overall_risk == "MEDIUM"

    # MEDIUM risk: Region-B with NE-201, NE-202 under MEDIUM weather:
    # weather MEDIUM (25) + crit MEDIUM (10) + 2 elems (10) + 2 svcs (10) = 55 -> HIGH
    # Under LOW weather: 10 + 10 + 10 + 10 = 40 -> MEDIUM
    enrichment_b = enrich_network_elements(adapter, ["NE-201", "NE-202"], "Region-B")
    impact_b = impact_analyzer.analyze(weather_severity="LOW", enrichment=enrichment_b)
    assess_medium = analyzer.assess(
        weather_severity="LOW",
        total_requested_elements=2,
        enrichment=enrichment_b,
        service_impact=impact_b,
    )
    assert assess_medium.risk_score == 40
    assert assess_medium.overall_risk == "MEDIUM"

    # HIGH risk: 1 element NE-102 (15) + HIGH weather (40) + 1 elem (5) + 2 svcs (10) = 70
    enrichment_high = enrich_network_elements(adapter, ["NE-102"], "Demo-Region-A")
    impact_high = impact_analyzer.analyze(weather_severity="HIGH", enrichment=enrichment_high)
    assess_high = analyzer.assess(
        weather_severity="HIGH",
        total_requested_elements=1,
        enrichment=enrichment_high,
        service_impact=impact_high,
    )
    assert assess_high.risk_score == 70
    assert assess_high.overall_risk == "HIGH"

    # CRITICAL risk & score cap at 100:
    # CRITICAL weather (55) + CRITICAL asset (20) + 4 elems (15) + 4 svcs (15)
    # + missing redundancy (10) + unresolved (10) = 125 -> capped at 100
    cap_catalog = {
        f"NE-C{i}": EnrichedNetworkElement(
            ne_id=f"NE-C{i}",
            type="CORE_ROUTER",
            region="Region-Cap",
            criticality="CRITICAL",
            services=["S1", "S2", "S3", "S4"],
            redundancy_available=False,
        )
        for i in range(1, 5)
    }
    enrichment_cap = enrich_network_elements(
        MockNetworkInventoryAdapter(cap_catalog),
        ["NE-C1", "NE-C2", "NE-C3", "NE-C4", "NE-MISSING"],
        "Region-Cap",
    )
    impact_cap = impact_analyzer.analyze(weather_severity="CRITICAL", enrichment=enrichment_cap)
    assess_cap = analyzer.assess(
        weather_severity="CRITICAL",
        total_requested_elements=5,
        enrichment=enrichment_cap,
        service_impact=impact_cap,
    )
    assert assess_cap.risk_score == 100
    assert assess_cap.overall_risk == "CRITICAL"

    # Deterministic repeatability
    assess_cap_repeat = analyzer.assess(
        weather_severity="CRITICAL",
        total_requested_elements=5,
        enrichment=enrichment_cap,
        service_impact=impact_cap,
    )
    assert assess_cap.model_dump() == assess_cap_repeat.model_dump()


# ------------------------------------------------------------
# Recommendation Engine Tests
# ------------------------------------------------------------
def test_recommendation_monitoring_state_changing_and_incomplete_data() -> None:
    rec_engine = RecommendationEngine()
    impact_analyzer = ServiceImpactAnalyzer()
    risk_analyzer = RiskAnalyzer()
    adapter = MockNetworkInventoryAdapter()

    # Monitoring recommendation (MEDIUM risk)
    enrichment_med = enrich_network_elements(adapter, ["NE-201", "NE-202"], "Region-B")
    impact_med = impact_analyzer.analyze(weather_severity="LOW", enrichment=enrichment_med)
    risk_med = risk_analyzer.assess(
        weather_severity="LOW",
        total_requested_elements=2,
        enrichment=enrichment_med,
        service_impact=impact_med,
    )
    rec_med = rec_engine.recommend(
        requested_elements=["NE-201", "NE-202"],
        enrichment=enrichment_med,
        service_impact=impact_med,
        risk_assessment=risk_med,
    )
    assert rec_med.action == "INCREASE_MONITORING"
    assert rec_med.state_changing is False
    assert rec_med.human_approval_required is False

    # State-changing recommendation (CRITICAL risk on Demo-Region-A NE-101, NE-102)
    enrichment_crit = enrich_network_elements(adapter, ["NE-101", "NE-102"], "Demo-Region-A")
    impact_crit = impact_analyzer.analyze(weather_severity="HIGH", enrichment=enrichment_crit)
    risk_crit = risk_analyzer.assess(
        weather_severity="HIGH",
        total_requested_elements=2,
        enrichment=enrichment_crit,
        service_impact=impact_crit,
    )
    rec_crit = rec_engine.recommend(
        requested_elements=["NE-101", "NE-102"],
        enrichment=enrichment_crit,
        service_impact=impact_crit,
        risk_assessment=risk_crit,
    )
    assert rec_crit.action == "REROUTE_TRAFFIC"
    assert rec_crit.state_changing is True
    assert rec_crit.human_approval_required is True

    # Incomplete data recommendation
    enrichment_inc = enrich_network_elements(adapter, ["NE-101", "NE-MISSING"], "Demo-Region-A")
    impact_inc = impact_analyzer.analyze(weather_severity="HIGH", enrichment=enrichment_inc)
    risk_inc = risk_analyzer.assess(
        weather_severity="HIGH",
        total_requested_elements=2,
        enrichment=enrichment_inc,
        service_impact=impact_inc,
    )
    rec_inc = rec_engine.recommend(
        requested_elements=["NE-101", "NE-MISSING"],
        enrichment=enrichment_inc,
        service_impact=impact_inc,
        risk_assessment=risk_inc,
    )
    assert rec_inc.action == "NO_AUTOMATED_ACTION"
    assert rec_inc.state_changing is False
    assert rec_inc.human_approval_required is True


# ------------------------------------------------------------
# Approval, Action & Verification Unit Tests
# ------------------------------------------------------------
def test_pending_rejected_and_expired_approvals_block_execution() -> None:
    agent = NetworkResilienceAgent()
    payload = make_agent1_payload()

    # Default PAUSE -> PENDING -> BLOCKED
    res_pending = agent.process(payload)
    assert res_pending.governance.decision == GovernanceDecision.PAUSE
    assert res_pending.approval.status == ApprovalStatus.PENDING
    assert res_pending.execution.status == ExecutionStatus.BLOCKED
    assert res_pending.verification.status == VerificationStatus.NOT_STARTED

    # REJECTED -> BLOCKED
    res_rejected = agent.process(
        payload,
        simulated_approval_decision=ApprovalStatus.REJECTED,
        approver_reference="demo-approver",
    )
    assert res_rejected.approval.status == ApprovalStatus.REJECTED
    assert res_rejected.execution.status == ExecutionStatus.BLOCKED
    assert res_rejected.verification.status == VerificationStatus.BLOCKED

    # EXPIRED -> BLOCKED
    res_expired = agent.process(
        payload,
        simulated_approval_decision=ApprovalStatus.EXPIRED,
        approver_reference="demo-approver",
    )
    assert res_expired.approval.status == ApprovalStatus.EXPIRED
    assert res_expired.execution.status == ExecutionStatus.BLOCKED
    assert res_expired.verification.status == VerificationStatus.BLOCKED


def test_approved_action_permits_controlled_execution_and_synthetic_verification() -> None:
    agent = NetworkResilienceAgent()
    res_approved = agent.process(
        make_agent1_payload(),
        simulated_approval_decision=ApprovalStatus.APPROVED,
        approver_reference="demo-approver",
    )
    assert res_approved.governance.decision == GovernanceDecision.PAUSE
    assert res_approved.approval.status == ApprovalStatus.APPROVED
    assert res_approved.execution.status == ExecutionStatus.EXECUTED
    assert res_approved.execution.synthetic is True
    assert res_approved.verification.status == VerificationStatus.PASSED
    assert res_approved.verification.synthetic is True


def test_simulated_action_failure_and_verification_failure() -> None:
    # Simulated remediation failure
    failing_action_service = ActionService(
        remediation_adapter=MockRemediationAdapter(simulate_failure=True),
        verification_service=SyntheticVerificationService(),
    )
    agent_fail_exec = NetworkResilienceAgent(action_service=failing_action_service)
    res_fail_exec = agent_fail_exec.process(
        make_agent1_payload(),
        simulated_approval_decision=ApprovalStatus.APPROVED,
    )
    assert res_fail_exec.execution.status == ExecutionStatus.FAILED
    assert res_fail_exec.verification.status == VerificationStatus.FAILED

    # Simulated verification failure after successful execution
    failing_ver_service = ActionService(
        remediation_adapter=MockRemediationAdapter(simulate_failure=False),
        verification_service=SyntheticVerificationService(simulate_verification_failure=True),
    )
    agent_fail_ver = NetworkResilienceAgent(action_service=failing_ver_service)
    res_fail_ver = agent_fail_ver.process(
        make_agent1_payload(),
        simulated_approval_decision=ApprovalStatus.APPROVED,
    )
    assert res_fail_ver.execution.status == ExecutionStatus.EXECUTED
    assert res_fail_ver.verification.status == VerificationStatus.FAILED


def test_metadata_propagation_across_all_stages() -> None:
    agent = NetworkResilienceAgent()
    payload = make_agent1_payload()
    result = agent.process(payload)

    assert result.schema_version == "0.1"
    assert result.team_code == "TEAM_PENDING"
    assert result.run_id == "WX-46329ae2b74b"
    assert result.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    assert result.data_source == "synthetic"
    assert len(result.evidence) == 10
    for evd in result.evidence:
        assert evd.run_id == "WX-46329ae2b74b"
        assert evd.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
        assert evd.synthetic is True
