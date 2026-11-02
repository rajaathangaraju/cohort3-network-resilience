"""Inventory lookup service using dependency injection."""

from common.exceptions import (
    EmptyNetworkElementResultError,
    InventoryLookupError,
    UnknownRegionError,
)
from integrations.inventory_api import InventoryAdapter


class InventoryService:
    def __init__(self, adapter: InventoryAdapter) -> None:
        self.adapter = adapter

    def find_affected_elements(self, region: str) -> list[str]:
        try:
            if not self.adapter.is_known_region(region):
                raise UnknownRegionError(f"Unknown region: {region}")
            elements = self.adapter.get_elements_by_region(region)
        except UnknownRegionError:
            raise
        except Exception as exc:
            raise InventoryLookupError(f"Inventory lookup failed for {region}") from exc
        if not elements:
            raise EmptyNetworkElementResultError(f"No network elements found for {region}")
        return elements
