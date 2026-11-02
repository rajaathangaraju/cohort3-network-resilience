"""Deterministic preventive action recommendation engine for Agent 2.

Recommendation generation and action execution are strictly separated.
"""

from datetime import UTC, datetime

from common.tracing import generate_prefixed_id
from contracts.recommendation import (
    EnrichmentResult,
    PreventiveRecommendation,
    RiskAssessment,
    ServiceImpact,
)


class RecommendationEngine:
    """Produces deterministic PreventiveRecommendation contracts from risk and inventory."""

    def recommend(
        self,
        *,
        requested_elements: list[str],
        enrichment: EnrichmentResult,
        service_impact: ServiceImpact,
        risk_assessment: RiskAssessment,
    ) -> PreventiveRecommendation:
        rec_id = generate_prefixed_id("REC")
        now = datetime.now(UTC)
        targets = (
            [e.ne_id for e in enrichment.resolved_elements]
            if enrichment.resolved_elements
            else list(requested_elements)
        )

        # Rule 1: Incomplete inventory data -> NO_AUTOMATED_ACTION
        if enrichment.unresolved_element_ids or not enrichment.resolved_elements:
            return PreventiveRecommendation(
                recommendation_id=rec_id,
                action="NO_AUTOMATED_ACTION",
                target_network_elements=list(requested_elements),
                rationale=(
                    "Inventory data is incomplete for element(s) "
                    f"{enrichment.unresolved_element_ids}; "
                    "automated state-changing actions are withheld pending operator review."
                ),
                expected_outcome=(
                    "Prevent unverified automated changes while operators inspect "
                    "missing inventory."
                ),
                state_changing=False,
                human_approval_required=True,
                confidence=0.5,
                generated_at=now,
            )

        overall_risk = risk_assessment.overall_risk
        redundancy_all = service_impact.redundancy_available_for_all

        # Rule 2: LOW risk -> CONTINUE_MONITORING
        if overall_risk == "LOW":
            return PreventiveRecommendation(
                recommendation_id=rec_id,
                action="CONTINUE_MONITORING",
                target_network_elements=targets,
                rationale=(
                    f"Overall risk is LOW (score={risk_assessment.risk_score}); standard telemetry "
                    "monitoring is sufficient."
                ),
                expected_outcome="Maintain visibility across affected region without state change.",
                state_changing=False,
                human_approval_required=False,
                confidence=0.95,
                generated_at=now,
            )

        # Rule 3: MEDIUM risk -> INCREASE_MONITORING
        if overall_risk == "MEDIUM":
            return PreventiveRecommendation(
                recommendation_id=rec_id,
                action="INCREASE_MONITORING",
                target_network_elements=targets,
                rationale=(
                    f"Overall risk is MEDIUM (score={risk_assessment.risk_score}); "
                    "heightened polling "
                    "and alarm sensitivity recommended for affected network elements."
                ),
                expected_outcome=(
                    "Detect early degradation on exposed network elements without state change."
                ),
                state_changing=False,
                human_approval_required=False,
                confidence=0.9,
                generated_at=now,
            )

        # Rule 4: HIGH risk with full redundancy -> PREPARE_TRAFFIC_REROUTE
        if overall_risk == "HIGH" and redundancy_all:
            return PreventiveRecommendation(
                recommendation_id=rec_id,
                action="PREPARE_TRAFFIC_REROUTE",
                target_network_elements=targets,
                rationale=(
                    f"Overall risk is HIGH (score={risk_assessment.risk_score}) "
                    "with redundancy available "
                    "across all resolved elements; pre-stage traffic reroute paths."
                ),
                expected_outcome=(
                    "Stage redundant failover paths for rapid traffic shift if "
                    "storm impact escalates."
                ),
                state_changing=True,
                human_approval_required=True,
                confidence=0.9,
                generated_at=now,
            )

        # Rule 5: HIGH risk without full redundancy -> ESCALATE_TO_NETWORK_OPERATIONS
        if overall_risk == "HIGH" and not redundancy_all:
            return PreventiveRecommendation(
                recommendation_id=rec_id,
                action="ESCALATE_TO_NETWORK_OPERATIONS",
                target_network_elements=targets,
                rationale=(
                    f"Overall risk is HIGH (score={risk_assessment.risk_score}) "
                    "and redundancy is missing "
                    "on one or more affected elements; escalate to Network Operations "
                    "for manual planning."
                ),
                expected_outcome=(
                    "Engage NOC engineers to coordinate field or manual protection measures."
                ),
                state_changing=False,
                human_approval_required=True,
                confidence=0.88,
                generated_at=now,
            )

        # Rule 6: CRITICAL risk -> REROUTE_TRAFFIC or PROTECT_NETWORK_ELEMENT
        action = "REROUTE_TRAFFIC" if redundancy_all else "PROTECT_NETWORK_ELEMENT"
        return PreventiveRecommendation(
            recommendation_id=rec_id,
            action=action,
            target_network_elements=targets,
            rationale=(
                f"Overall risk is CRITICAL (score={risk_assessment.risk_score}); "
                "immediate preventive "
                f"action '{action}' is recommended for {targets}."
            ),
            expected_outcome=(
                "Proactively isolate or reroute critical services away from severe weather impact."
            ),
            state_changing=True,
            human_approval_required=True,
            confidence=0.92,
            generated_at=now,
        )
