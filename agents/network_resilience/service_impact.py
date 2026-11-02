"""Deterministic, rule-based service impact analysis for Agent 2.

Does not call an LLM and never fabricates customer counts, financial loss, or SLA penalties.
"""

from common.constants import RISK_LEVEL_ORDER
from contracts.recommendation import EnrichedNetworkElement, EnrichmentResult, ServiceImpact


def _elevate_risk(current: str, candidate: str) -> str:
    if RISK_LEVEL_ORDER.get(candidate, 1) > RISK_LEVEL_ORDER.get(current, 1):
        return candidate
    return current


class ServiceImpactAnalyzer:
    """Evaluates affected network elements to produce a deterministic ServiceImpact model."""

    def analyze(
        self,
        *,
        weather_severity: str,
        enrichment: EnrichmentResult,
    ) -> ServiceImpact:
        resolved: list[EnrichedNetworkElement] = enrichment.resolved_elements
        unresolved: list[str] = enrichment.unresolved_element_ids

        affected_services: list[str] = []
        seen_services: set[str] = set()
        critical_count = 0
        high_count = 0
        element_types: list[str] = []
        non_active_elements: list[str] = []

        for elem in resolved:
            if elem.type not in element_types:
                element_types.append(elem.type)
            if elem.criticality == "CRITICAL":
                critical_count += 1
            elif elem.criticality == "HIGH":
                high_count += 1
            if elem.operational_status != "ACTIVE":
                non_active_elements.append(f"{elem.ne_id}({elem.operational_status})")
            for svc in elem.services:
                if svc not in seen_services:
                    seen_services.add(svc)
                    affected_services.append(svc)

        redundancy_available_for_all = (
            bool(resolved)
            and not unresolved
            and all(elem.redundancy_available for elem in resolved)
        )

        reasons: list[str] = []
        reasons.append(
            f"Evaluated {len(resolved)} resolved network element(s) of types {element_types} "
            f"under {weather_severity} weather severity."
        )

        # Deterministic rule-based service_risk calculation
        service_risk = "LOW"

        if critical_count > 0:
            if not redundancy_available_for_all or weather_severity == "CRITICAL":
                service_risk = _elevate_risk(service_risk, "CRITICAL")
                reasons.append(
                    f"{critical_count} CRITICAL network element(s) exposed with "
                    f"weather={weather_severity} "
                    f"and redundancy_available_for_all={redundancy_available_for_all}."
                )
            else:
                service_risk = _elevate_risk(service_risk, "HIGH")
                reasons.append(
                    f"{critical_count} CRITICAL network element(s) exposed "
                    "with redundancy available."
                )

        if high_count > 0:
            if not redundancy_available_for_all and weather_severity in {"HIGH", "CRITICAL"}:
                service_risk = _elevate_risk(service_risk, "HIGH")
                reasons.append(
                    f"{high_count} HIGH criticality element(s) lack full redundancy "
                    f"during {weather_severity} weather."
                )
            elif weather_severity in {"HIGH", "CRITICAL"} or len(affected_services) >= 2:
                service_risk = _elevate_risk(service_risk, "HIGH")
                reasons.append(
                    f"{high_count} HIGH criticality element(s) and "
                    f"{len(affected_services)} service(s) exposed."
                )
            else:
                service_risk = _elevate_risk(service_risk, "MEDIUM")
                reasons.append(f"{high_count} HIGH criticality element(s) exposed.")

        if len(affected_services) >= 4:
            service_risk = _elevate_risk(service_risk, "HIGH")
            reasons.append(f"{len(affected_services)} synthetic service categories are exposed.")
        elif len(affected_services) >= 2 and service_risk == "LOW":
            service_risk = _elevate_risk(service_risk, "MEDIUM")
            reasons.append(f"{len(affected_services)} synthetic service categories are exposed.")

        if not redundancy_available_for_all:
            service_risk = _elevate_risk(service_risk, "HIGH" if resolved else "MEDIUM")
            reasons.append("Redundancy is not available across all affected network elements.")

        if non_active_elements:
            service_risk = _elevate_risk(service_risk, "MEDIUM")
            reasons.append(f"Non-ACTIVE operational status observed on: {non_active_elements}.")

        if unresolved:
            service_risk = _elevate_risk(service_risk, "HIGH")
            reasons.append(
                f"Inventory enrichment left {len(unresolved)} unresolved "
                f"network element ID(s): {unresolved}."
            )

        summary = (
            f"Service risk is {service_risk}: {len(affected_services)} affected service(s) "
            f"across {len(resolved)} resolved element(s) "
            f"({critical_count} CRITICAL, {high_count} HIGH; "
            f"redundancy_available_for_all={redundancy_available_for_all}; "
            f"unresolved={len(unresolved)})."
        )

        return ServiceImpact(
            affected_service_count=len(affected_services),
            affected_services=affected_services,
            critical_network_element_count=critical_count,
            high_criticality_network_element_count=high_count,
            redundancy_available_for_all=redundancy_available_for_all,
            service_risk=service_risk,
            impact_summary=summary,
            analysis_reasons=reasons,
            unresolved_network_elements=list(unresolved),
        )
