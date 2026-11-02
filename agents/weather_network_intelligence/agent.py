"""Agent 1 orchestration; this module has no dependency on Agent 2."""

import structlog

from common.constants import AGENT1_NAME
from common.logging import log_agent_operation
from contracts.network_risk_event import NetworkRiskEvent
from integrations.inventory_api import InventoryAdapter, MockInventoryAdapter

from .inventory_service import InventoryService
from .network_mapper import build_network_risk_event
from .weather_service import WeatherService


class WeatherNetworkIntelligenceAgent:
    def __init__(self, inventory_adapter: InventoryAdapter | None = None) -> None:
        self.inventory = InventoryService(inventory_adapter or MockInventoryAdapter())
        self.weather = WeatherService()
        self.logger = structlog.get_logger(AGENT1_NAME)

    def process(self, payload: dict[str, object]) -> NetworkRiskEvent:
        alert = self.weather.normalize(payload)
        log_agent_operation(
            self.logger,
            run_id=alert.run_id,
            trace_id=alert.trace_id,
            agent_name=AGENT1_NAME,
            operation="validate_weather_event",
            result="success",
        )
        elements = self.inventory.find_affected_elements(alert.affected_region)
        log_agent_operation(
            self.logger,
            run_id=alert.run_id,
            trace_id=alert.trace_id,
            agent_name=AGENT1_NAME,
            operation="query_network_inventory",
            result="success",
            element_count=len(elements),
        )
        event = build_network_risk_event(alert, elements)
        log_agent_operation(
            self.logger,
            run_id=event.run_id,
            trace_id=event.trace_id,
            agent_name=AGENT1_NAME,
            operation="build_network_risk_event",
            result="success",
        )
        return event
