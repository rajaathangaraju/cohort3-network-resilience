"""Integration tests for Agent 1 -> Agent 2 contract handoff and payload processing."""

from typing import Any

from agents.network_resilience.agent import NetworkResilienceAgent
from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent
from contracts.action_result import ExecutionStatus, VerificationStatus
from contracts.governance_decision import ApprovalStatus, GovernanceDecision

EXACT_AGENT1_INTEGRATION_PAYLOAD: dict[str, Any] = {
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
    "affected_network_elements": [
        "NE-101",
        "NE-102",
    ],
}


def test_synthetic_weather_to_network_risk_event() -> None:
    event = WeatherNetworkIntelligenceAgent().process(
        {
            "weather_event": "SEVERE_STORM",
            "severity": "HIGH",
            "affected_region": "Region-A",
        }
    )
    assert event.model_dump()["affected_network_elements"] == [
        "NE-101",
        "NE-102",
        "NE-103",
    ]


def test_exact_agent1_payload_to_agent2_pause_pending_blocked() -> None:
    """Integration test 1:
    Exact Agent 1 response -> Agent 2 validation -> Mock inventory enrichment ->
    Service impact -> Risk assessment -> Recommendation -> Governance PAUSE ->
    PENDING approval -> BLOCKED execution -> NOT_STARTED verification -> Evidence records.
    """
    agent2 = NetworkResilienceAgent()
    result = agent2.process(EXACT_AGENT1_INTEGRATION_PAYLOAD)

    # Preserved metadata & input context
    assert result.schema_version == "0.1"
    assert result.team_code == "TEAM_PENDING"
    assert result.run_id == "WX-46329ae2b74b"
    assert result.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    assert result.data_source == "synthetic"
    assert result.input_context.weather_event == "SEVERE_STORM"
    assert result.input_context.severity == "HIGH"
    assert result.input_context.affected_region == "Demo-Region-A"
    assert result.input_context.affected_network_elements == ["NE-101", "NE-102"]

    # Enriched assets
    assert len(result.affected_assets) == 2
    assert [a.ne_id for a in result.affected_assets] == ["NE-101", "NE-102"]
    assert result.unresolved_network_elements == []

    # Deterministic risk calculation:
    # HIGH weather (40) + CRITICAL NE-101 (20) + 2 elements (10) + 4 services (15) = 85
    assert result.risk_assessment.risk_score == 85
    assert result.risk_assessment.overall_risk == "CRITICAL"
    assert result.risk_assessment.network_risk == "HIGH"
    assert result.risk_assessment.service_risk == "HIGH"

    # Recommendation
    assert result.recommendation.action == "REROUTE_TRAFFIC"
    assert result.recommendation.target_network_elements == ["NE-101", "NE-102"]
    assert result.recommendation.state_changing is True
    assert result.recommendation.human_approval_required is True

    # Governance, Approval, Execution, Verification
    assert result.governance.decision == GovernanceDecision.PAUSE
    assert result.approval.status == ApprovalStatus.PENDING
    assert result.execution.status == ExecutionStatus.BLOCKED
    assert result.execution.synthetic is True
    assert result.verification.status == VerificationStatus.NOT_STARTED
    assert result.verification.synthetic is True

    # Evidence for all 10 stages
    expected_stages = [
        "input_received",
        "input_validation",
        "inventory_enrichment",
        "service_impact_analysis",
        "risk_assessment",
        "recommendation",
        "governance_decision",
        "human_approval",
        "action_execution",
        "verification",
    ]
    assert [e.stage for e in result.evidence] == expected_stages
    assert all(e.run_id == "WX-46329ae2b74b" for e in result.evidence)
    assert all(e.trace_id == "7a4106b675e4419f87b9b84d30a67ca4" for e in result.evidence)
