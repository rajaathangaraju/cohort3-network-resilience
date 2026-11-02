"""Inventory adapter boundaries and clearly synthetic local implementations.

All inventory data in this module is SYNTHETIC / MOCK DATA for development and integration testing.
It does NOT represent actual telecom, customer, network, or production data.
"""

from typing import ClassVar, Protocol

from common.exceptions import (
    EmptyNetworkElementResultError,
    InventoryLookupError,
    RegionMismatchError,
)
from contracts.recommendation import EnrichedNetworkElement, EnrichmentResult


class InventoryAdapter(Protocol):
    """Agent 1 region-to-element-ID lookup interface."""

    def is_known_region(self, region: str) -> bool: ...
    def get_elements_by_region(self, region: str) -> list[str]: ...


InventoryApiProvider = InventoryAdapter


class MockInventoryAdapter:
    """Synthetic/mock region inventory for Agent 1; not real network or customer data."""

    DEFAULT_DATA: ClassVar[dict[str, list[str]]] = {
        "Region-A": ["NE-101", "NE-102", "NE-103"],
        "Demo-Region-A": ["NE-101", "NE-102"],
        "Region-B": ["NE-201", "NE-202"],
        "Region-C": ["NE-301", "NE-302", "NE-303", "NE-304"],
    }

    def __init__(self, inventory_data: dict[str, list[str]] | None = None) -> None:
        self.inventory_data = (
            {k: list(v) for k, v in inventory_data.items()}
            if inventory_data is not None
            else {k: list(v) for k, v in self.DEFAULT_DATA.items()}
        )

    def is_known_region(self, region: str) -> bool:
        return region in self.inventory_data

    def get_elements_by_region(self, region: str) -> list[str]:
        return list(self.inventory_data[region])


class RealInventoryAPIAdapter:
    """Placeholder for future real inventory API integration."""

    def is_known_region(self, region: str) -> bool:
        raise NotImplementedError("Real inventory API is not integrated yet.")

    def get_elements_by_region(self, region: str) -> list[str]:
        raise NotImplementedError("Real inventory API is not integrated yet.")


class NetworkInventoryPort(Protocol):
    """Agent 2 element-enrichment port protocol."""

    def get_element_details(self, ne_id: str) -> EnrichedNetworkElement | None: ...


class MockNetworkInventoryAdapter:
    """SYNTHETIC / MOCK network element enrichment catalog for Agent 2.

    Do not treat this catalog as real telecom, customer, or production network data.
    """

    DEFAULT_CATALOG: ClassVar[dict[str, EnrichedNetworkElement]] = {
        "NE-101": EnrichedNetworkElement(
            ne_id="NE-101",
            type="CELL_SITE",
            region="Demo-Region-A",
            criticality="CRITICAL",
            services=["MOBILE_DATA", "VOICE"],
            redundancy_available=True,
            operational_status="ACTIVE",
            synthetic=True,
        ),
        "NE-102": EnrichedNetworkElement(
            ne_id="NE-102",
            type="ROUTER",
            region="Demo-Region-A",
            criticality="HIGH",
            services=["IP_TRANSIT", "ENTERPRISE_CONNECTIVITY"],
            redundancy_available=True,
            operational_status="ACTIVE",
            synthetic=True,
        ),
        "NE-103": EnrichedNetworkElement(
            ne_id="NE-103",
            type="FIBER_AGGREGATION",
            region="Demo-Region-A",
            criticality="MEDIUM",
            services=["BACKHAUL"],
            redundancy_available=True,
            operational_status="ACTIVE",
            synthetic=True,
        ),
        "NE-201": EnrichedNetworkElement(
            ne_id="NE-201",
            type="EDGE_SWITCH",
            region="Region-B",
            criticality="LOW",
            services=["INTERNAL_TELEMETRY"],
            redundancy_available=True,
            operational_status="ACTIVE",
            synthetic=True,
        ),
        "NE-202": EnrichedNetworkElement(
            ne_id="NE-202",
            type="MICROWAVE_LINK",
            region="Region-B",
            criticality="MEDIUM",
            services=["REGIONAL_VOICE"],
            redundancy_available=True,
            operational_status="ACTIVE",
            synthetic=True,
        ),
        "NE-301": EnrichedNetworkElement(
            ne_id="NE-301",
            type="CORE_ROUTER",
            region="Region-C",
            criticality="HIGH",
            services=["ENTERPRISE_VPN", "SIP_TRUNKING"],
            redundancy_available=False,
            operational_status="ACTIVE",
            synthetic=True,
        ),
    }

    def __init__(
        self,
        catalog: dict[str, EnrichedNetworkElement] | None = None,
        *,
        simulate_failure: bool = False,
    ) -> None:
        self._catalog: dict[str, EnrichedNetworkElement] = (
            dict(catalog) if catalog is not None else dict(self.DEFAULT_CATALOG)
        )
        self._simulate_failure = simulate_failure

    def get_element_details(self, ne_id: str) -> EnrichedNetworkElement | None:
        if self._simulate_failure:
            raise InventoryLookupError(
                f"Simulated synthetic inventory adapter failure while querying '{ne_id}'"
            )
        return self._catalog.get(ne_id)


def enrich_network_elements(
    port: NetworkInventoryPort,
    element_ids: list[str],
    expected_region: str,
    *,
    strict_region_check: bool = True,
    require_at_least_one_resolved: bool = False,
) -> EnrichmentResult:
    """Enrich network-element IDs via NetworkInventoryPort without discarding unresolved IDs."""
    deduped_ids: list[str] = []
    seen: set[str] = set()
    for ne_id in element_ids:
        if ne_id not in seen:
            seen.add(ne_id)
            deduped_ids.append(ne_id)

    resolved: list[EnrichedNetworkElement] = []
    unresolved: list[str] = []
    region_mismatches: list[str] = []

    for ne_id in deduped_ids:
        try:
            detail = port.get_element_details(ne_id)
        except InventoryLookupError:
            raise
        except Exception as exc:
            raise InventoryLookupError(
                f"Inventory adapter failed while enriching '{ne_id}': {exc}"
            ) from exc

        if detail is None:
            unresolved.append(ne_id)
            continue

        if detail.region != expected_region:
            region_mismatches.append(ne_id)
            if strict_region_check:
                raise RegionMismatchError(
                    f"Network element '{ne_id}' belongs to region '{detail.region}', "
                    f"which does not match event affected_region '{expected_region}'"
                )
            unresolved.append(ne_id)
            continue

        resolved.append(detail)

    if require_at_least_one_resolved and not resolved:
        raise EmptyNetworkElementResultError(
            f"No network elements could be resolved for region '{expected_region}'"
        )

    return EnrichmentResult(
        resolved_elements=resolved,
        unresolved_element_ids=unresolved,
        region_mismatches=region_mismatches,
        synthetic=True,
    )
