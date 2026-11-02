"""Unit tests for Agent 1 (Weather & Network Intelligence Agent)."""

from typing import Any

import pytest

from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent
from common.exceptions import (
    EmptyNetworkElementResultError,
    InvalidWeatherEventError,
    UnknownRegionError,
    UnsupportedSeverityError,
)
from integrations.inventory_api import MockInventoryAdapter


def payload(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "run_id": "WX-1",
        "trace_id": "TRACE-1",
        "weather_event": "SEVERE_STORM",
        "severity": "HIGH",
        "affected_region": "Region-A",
    }
    value.update(changes)
    return value


def test_region_a_successful_mapping() -> None:
    assert WeatherNetworkIntelligenceAgent().process(payload()).affected_network_elements == [
        "NE-101",
        "NE-102",
        "NE-103",
    ]


def test_region_b_successful_mapping() -> None:
    assert WeatherNetworkIntelligenceAgent().process(
        payload(affected_region="Region-B")
    ).affected_network_elements == ["NE-201", "NE-202"]


def test_unknown_region() -> None:
    with pytest.raises(UnknownRegionError):
        WeatherNetworkIntelligenceAgent().process(payload(affected_region="Region-Z"))


def test_invalid_severity() -> None:
    with pytest.raises(UnsupportedSeverityError):
        WeatherNetworkIntelligenceAgent().process(payload(severity="EXTREME"))


def test_invalid_weather_event() -> None:
    with pytest.raises(InvalidWeatherEventError):
        WeatherNetworkIntelligenceAgent().process(payload(weather_event="ALIENS"))


def test_correlation_ids_propagate_and_generate() -> None:
    result = WeatherNetworkIntelligenceAgent().process(payload())
    assert (result.run_id, result.trace_id) == ("WX-1", "TRACE-1")
    generated = WeatherNetworkIntelligenceAgent().process(payload(run_id=None, trace_id=None))
    assert generated.run_id.startswith("WX-")
    assert generated.trace_id.startswith("TRACE-")


def test_empty_inventory_result() -> None:
    adapter = MockInventoryAdapter({"Region-Empty": []})
    with pytest.raises(EmptyNetworkElementResultError):
        WeatherNetworkIntelligenceAgent(adapter).process(payload(affected_region="Region-Empty"))
