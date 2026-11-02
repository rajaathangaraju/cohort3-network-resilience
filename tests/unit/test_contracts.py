"""Unit tests for shared contracts and Agent 2 input validation."""

from typing import Any

import pytest

from common.exceptions import (
    EmptyNetworkElementResultError,
    InvalidBoundingBoxError,
    InvalidDataSourceError,
    InvalidNetworkRiskEventError,
    InvalidTimestampError,
    InvalidWeatherEventError,
    UnknownRegionError,
    UnsupportedSeverityError,
)
from contracts import (
    GovernanceDecision,
    NetworkRiskEvent,
    validate_agent2_input,
)


def make_valid_agent1_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
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
    base.update(overrides)
    return base


def test_contracts_preserve_correlation_ids() -> None:
    event = NetworkRiskEvent(
        run_id="WX-0042",
        trace_id="TRACE-0042",
        weather_event="SEVERE_STORM",
        severity="HIGH",
        affected_region="Region-A",
    )
    assert event.run_id == "WX-0042"
    assert GovernanceDecision.PAUSE.value == "PAUSE"


def test_valid_agent1_response_validation() -> None:
    event = validate_agent2_input(make_valid_agent1_payload())
    assert event.schema_version == "0.1"
    assert event.team_code == "TEAM_PENDING"
    assert event.run_id == "WX-46329ae2b74b"
    assert event.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    assert event.data_source == "synthetic"
    assert event.affected_network_elements == ["NE-101", "NE-102"]


def test_missing_run_id_rejected() -> None:
    payload = make_valid_agent1_payload(run_id="")
    with pytest.raises(InvalidNetworkRiskEventError):
        validate_agent2_input(payload)


def test_missing_trace_id_rejected() -> None:
    payload = make_valid_agent1_payload(trace_id="   ")
    with pytest.raises(InvalidNetworkRiskEventError):
        validate_agent2_input(payload)


def test_invalid_timestamp_rejected() -> None:
    # Naive timestamp without timezone
    with pytest.raises(InvalidTimestampError):
        validate_agent2_input(make_valid_agent1_payload(created_at="2026-11-02T00:26:32"))
    # Unparseable timestamp
    with pytest.raises(InvalidTimestampError):
        validate_agent2_input(make_valid_agent1_payload(created_at="not-a-timestamp"))


def test_invalid_data_source_rejected() -> None:
    with pytest.raises(InvalidDataSourceError):
        validate_agent2_input(make_valid_agent1_payload(data_source="production_live"))


def test_invalid_severity_rejected() -> None:
    with pytest.raises(UnsupportedSeverityError):
        validate_agent2_input(make_valid_agent1_payload(severity="CATASTROPHIC"))


def test_empty_weather_event_or_region_rejected() -> None:
    with pytest.raises(InvalidWeatherEventError):
        validate_agent2_input(make_valid_agent1_payload(weather_event=""))
    with pytest.raises(UnknownRegionError):
        validate_agent2_input(make_valid_agent1_payload(affected_region=""))


def test_invalid_bounds_rejected() -> None:
    # south >= north
    with pytest.raises(InvalidBoundingBoxError):
        validate_agent2_input(
            make_valid_agent1_payload(
                affected_area={"south": 14.0, "north": 13.2, "west": 77.4, "east": 77.8}
            )
        )
    # west >= east
    with pytest.raises(InvalidBoundingBoxError):
        validate_agent2_input(
            make_valid_agent1_payload(
                affected_area={"south": 12.8, "north": 13.2, "west": 78.0, "east": 77.8}
            )
        )


def test_empty_affected_network_elements_rejected() -> None:
    with pytest.raises(EmptyNetworkElementResultError):
        validate_agent2_input(make_valid_agent1_payload(affected_network_elements=[]))


def test_duplicate_network_element_ids_deduplicated_preserving_order() -> None:
    event = validate_agent2_input(
        make_valid_agent1_payload(
            affected_network_elements=["NE-102", "NE-101", "NE-102", "NE-101"]
        )
    )
    assert event.affected_network_elements == ["NE-102", "NE-101"]


def test_blank_network_element_id_rejected() -> None:
    with pytest.raises(InvalidNetworkRiskEventError):
        validate_agent2_input(
            make_valid_agent1_payload(affected_network_elements=["NE-101", "   "])
        )
