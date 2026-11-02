"""Deterministic network risk calculation using the documented 0-100 MVP scoring policy.

Note: This scoring model is an initial synthetic MVP scoring policy and is NOT production-approved.
"""

from common.constants import (
    ASSET_CRITICALITY_SCORE,
    ELEMENT_COUNT_SCORE_FOUR_PLUS,
    ELEMENT_COUNT_SCORE_SINGLE,
    ELEMENT_COUNT_SCORE_TWO_TO_THREE,
    MAX_RISK_SCORE,
    REDUNDANCY_ALL_AVAILABLE_SCORE,
    REDUNDANCY_MISSING_SCORE,
    RISK_LEVEL_ORDER,
    RISK_SCORE_HIGH_MAX,
    RISK_SCORE_LOW_MAX,
    RISK_SCORE_MEDIUM_MAX,
    SERVICE_EXPOSURE_SCORE_FOUR_PLUS,
    SERVICE_EXPOSURE_SCORE_TWO_TO_THREE,
    SERVICE_EXPOSURE_SCORE_ZERO_TO_ONE,
    UNRESOLVED_NONE_SCORE,
    UNRESOLVED_PRESENT_SCORE,
    WEATHER_SEVERITY_SCORE,
)
from contracts.recommendation import (
    AffectedAssetDetail,
    EnrichedNetworkElement,
    EnrichmentResult,
    RiskAssessment,
    RiskAssessmentSummary,
    ServiceImpact,
)


def map_score_to_risk_level(score: int) -> str:
    """Map a 0-100 score to LOW, MEDIUM, HIGH, or CRITICAL."""
    if score <= RISK_SCORE_LOW_MAX:
        return "LOW"
    if score <= RISK_SCORE_MEDIUM_MAX:
        return "MEDIUM"
    if score <= RISK_SCORE_HIGH_MAX:
        return "HIGH"
    return "CRITICAL"


def _compute_element_risk(elem: EnrichedNetworkElement, weather_severity: str) -> str:
    weather_rank = RISK_LEVEL_ORDER.get(weather_severity, 1)
    crit_rank = RISK_LEVEL_ORDER.get(elem.criticality, 1)

    if not elem.redundancy_available and crit_rank >= 3 and weather_rank >= 3:
        return "CRITICAL"
    if crit_rank == 4 and weather_rank == 4:
        return "CRITICAL"
    if crit_rank >= 3 and weather_rank >= 3:
        return "HIGH"
    if crit_rank >= 3 or weather_rank >= 3 or not elem.redundancy_available:
        return "MEDIUM"
    return "LOW"


def _compute_network_risk(
    resolved: list[EnrichedNetworkElement],
    unresolved: list[str],
    weather_severity: str,
) -> str:
    if not resolved and unresolved:
        return "HIGH"

    highest_crit_rank = max(
        (RISK_LEVEL_ORDER.get(e.criticality, 1) for e in resolved),
        default=1,
    )
    any_missing_redundancy = any(not e.redundancy_available for e in resolved)
    weather_rank = RISK_LEVEL_ORDER.get(weather_severity, 1)

    if highest_crit_rank == 4 and (any_missing_redundancy or weather_rank == 4):
        return "CRITICAL"
    if highest_crit_rank >= 3 and (weather_rank >= 3 or any_missing_redundancy or unresolved):
        return "HIGH"
    if highest_crit_rank >= 2 or weather_rank >= 2 or any_missing_redundancy or unresolved:
        return "MEDIUM"
    return "LOW"


class RiskAnalyzer:
    """Deterministic risk calculator implementing the synthetic MVP 0-100 scoring model."""

    def assess(
        self,
        *,
        weather_severity: str,
        total_requested_elements: int,
        enrichment: EnrichmentResult,
        service_impact: ServiceImpact,
    ) -> RiskAssessment:
        resolved = enrichment.resolved_elements
        unresolved = enrichment.unresolved_element_ids

        reasons: list[str] = []

        # 1. Weather severity score
        weather_points = WEATHER_SEVERITY_SCORE.get(weather_severity, 10)
        reasons.append(f"Weather severity {weather_severity} contributed +{weather_points} points.")

        # 2. Highest affected-asset criticality score (added once)
        if resolved:
            highest_elem = max(
                resolved,
                key=lambda e: ASSET_CRITICALITY_SCORE.get(e.criticality, 5),
            )
            crit_points = ASSET_CRITICALITY_SCORE.get(highest_elem.criticality, 5)
            reasons.append(
                f"Highest affected asset criticality {highest_elem.criticality} "
                f"({highest_elem.ne_id}) contributed +{crit_points} points."
            )
        else:
            crit_points = 0
            reasons.append("No resolved assets available for criticality scoring (+0 points).")

        # 3. Affected-element count score
        element_count = max(total_requested_elements, len(resolved) + len(unresolved))
        if element_count <= 1:
            count_points = ELEMENT_COUNT_SCORE_SINGLE
        elif element_count <= 3:
            count_points = ELEMENT_COUNT_SCORE_TWO_TO_THREE
        else:
            count_points = ELEMENT_COUNT_SCORE_FOUR_PLUS
        reasons.append(
            f"Affected network element count ({element_count}) contributed +{count_points} points."
        )

        # 4. Service exposure score
        svc_count = service_impact.affected_service_count
        if svc_count <= 1:
            service_points = SERVICE_EXPOSURE_SCORE_ZERO_TO_ONE
        elif svc_count <= 3:
            service_points = SERVICE_EXPOSURE_SCORE_TWO_TO_THREE
        else:
            service_points = SERVICE_EXPOSURE_SCORE_FOUR_PLUS
        reasons.append(
            f"Service exposure ({svc_count} affected service(s)) "
            f"contributed +{service_points} points."
        )

        # 5. Redundancy score
        if resolved and all(e.redundancy_available for e in resolved):
            redundancy_points = REDUNDANCY_ALL_AVAILABLE_SCORE
            reasons.append("Redundancy is available for all resolved elements (+0 points).")
        elif resolved:
            redundancy_points = REDUNDANCY_MISSING_SCORE
            reasons.append(
                "Redundancy is missing for at least one affected element "
                f"(+{redundancy_points} points)."
            )
        else:
            redundancy_points = REDUNDANCY_ALL_AVAILABLE_SCORE
            reasons.append("Redundancy could not be confirmed on unresolved elements (+0 points).")

        # 6. Unresolved inventory score
        if unresolved:
            unresolved_points = UNRESOLVED_PRESENT_SCORE
            reasons.append(
                f"Unresolved inventory element(s) {unresolved} "
                f"contributed +{unresolved_points} points."
            )
        else:
            unresolved_points = UNRESOLVED_NONE_SCORE
            reasons.append("All requested network elements resolved in inventory (+0 points).")

        raw_score = (
            weather_points
            + crit_points
            + count_points
            + service_points
            + redundancy_points
            + unresolved_points
        )
        capped_score = min(raw_score, MAX_RISK_SCORE)
        if raw_score > MAX_RISK_SCORE:
            reasons.append(f"Raw score {raw_score} was capped at {MAX_RISK_SCORE}.")

        overall_risk = map_score_to_risk_level(capped_score)
        weather_risk = weather_severity
        network_risk = _compute_network_risk(resolved, unresolved, weather_severity)

        asset_details = [
            AffectedAssetDetail(
                ne_id=elem.ne_id,
                type=elem.type,
                region=elem.region,
                criticality=elem.criticality,
                services=list(elem.services),
                risk=_compute_element_risk(elem, weather_severity),
                redundancy_available=elem.redundancy_available,
                operational_status=elem.operational_status,
            )
            for elem in resolved
        ]

        return RiskAssessment(
            weather_risk=weather_risk,
            network_risk=network_risk,
            service_risk=service_impact.service_risk,
            overall_risk=overall_risk,
            risk_score=capped_score,
            risk_reasons=reasons,
            affected_asset_details=asset_details,
            unresolved_network_elements=list(unresolved),
        )

    @staticmethod
    def to_summary(assessment: RiskAssessment) -> RiskAssessmentSummary:
        return RiskAssessmentSummary(
            risk_score=assessment.risk_score,
            overall_risk=assessment.overall_risk,
            weather_risk=assessment.weather_risk,
            network_risk=assessment.network_risk,
            service_risk=assessment.service_risk,
            risk_reasons=list(assessment.risk_reasons),
            unresolved_network_elements=list(assessment.unresolved_network_elements),
        )
