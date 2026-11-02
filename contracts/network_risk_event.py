"""Agent 1 input and output contracts and Agent 2 input validation."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.constants import (
    DEFAULT_DATA_SOURCE,
    DEFAULT_SCHEMA_VERSION,
    DEFAULT_TEAM_CODE,
    SUPPORTED_DATA_SOURCES,
    SUPPORTED_SEVERITIES,
)
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

from .base import TraceableContract


class AffectedArea(BaseModel):
    """Geographic bounding box for an affected weather region."""

    model_config = ConfigDict(extra="forbid")

    south: float
    north: float
    west: float
    east: float

    @model_validator(mode="after")
    def validate_bounds(self) -> "AffectedArea":
        if self.south >= self.north:
            raise InvalidBoundingBoxError(
                f"Invalid bounding box: south ({self.south}) must be less than north ({self.north})"
            )
        if self.west >= self.east:
            raise InvalidBoundingBoxError(
                f"Invalid bounding box: west ({self.west}) must be less than east ({self.east})"
            )
        return self


class WeatherAlertInput(TraceableContract):
    """Initial weather alert input contract for Agent 1."""

    schema_version: str = Field(default=DEFAULT_SCHEMA_VERSION, min_length=1)
    team_code: str = Field(default=DEFAULT_TEAM_CODE, min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data_source: str = Field(default=DEFAULT_DATA_SOURCE, min_length=1)
    weather_event: str
    severity: str
    affected_region: str
    forecast: str | None = None
    affected_area: AffectedArea | None = None


class NetworkRiskEvent(WeatherAlertInput):
    """Consolidated risk event emitted by Agent 1 and consumed by Agent 2."""

    affected_network_elements: list[str] = Field(default_factory=list)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("affected_network_elements")
    @classmethod
    def deduplicate_and_validate_elements(cls, elements: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for item in elements:
            if not isinstance(item, str) or not item.strip():
                raise InvalidNetworkRiskEventError(
                    "Blank network-element IDs are not permitted in affected_network_elements"
                )
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped


def validate_agent2_input(payload: dict[str, Any] | NetworkRiskEvent) -> NetworkRiskEvent:
    """Strictly validate an incoming Agent 1 NetworkRiskEvent payload for Agent 2 processing.

    Enforces all Agent 2 input validation rules without altering or guessing Agent 1 values,
    except deduplicating network-element IDs while preserving original order.
    """
    if isinstance(payload, NetworkRiskEvent):
        raw: dict[str, Any] = payload.model_dump()
    elif isinstance(payload, dict):
        raw = dict(payload)
    else:
        raise InvalidNetworkRiskEventError("Input payload must be a dict or NetworkRiskEvent")

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise InvalidNetworkRiskEventError("schema_version must not be empty")

    team_code = raw.get("team_code")
    if not isinstance(team_code, str) or not team_code.strip():
        raise InvalidNetworkRiskEventError("team_code must not be empty")

    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidNetworkRiskEventError("run_id must not be empty")

    trace_id = raw.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise InvalidNetworkRiskEventError("trace_id must not be empty")

    created_at_raw = raw.get("created_at")
    if created_at_raw is None:
        raise InvalidTimestampError("created_at is required and must be a timezone-aware datetime")
    if isinstance(created_at_raw, str):
        try:
            parsed_dt = datetime.fromisoformat(created_at_raw)
        except ValueError as exc:
            raise InvalidTimestampError(f"Invalid created_at timestamp: {created_at_raw}") from exc
    elif isinstance(created_at_raw, datetime):
        parsed_dt = created_at_raw
    else:
        raise InvalidTimestampError("created_at must be a valid ISO-8601 string or datetime")

    if parsed_dt.tzinfo is None or parsed_dt.tzinfo.utcoffset(parsed_dt) is None:
        raise InvalidTimestampError("created_at must be a timezone-aware datetime")
    raw["created_at"] = parsed_dt

    data_source = raw.get("data_source")
    if not isinstance(data_source, str) or data_source not in SUPPORTED_DATA_SOURCES:
        raise InvalidDataSourceError(
            f"Unsupported data_source '{data_source}'. Supported: {sorted(SUPPORTED_DATA_SOURCES)}"
        )

    weather_event = raw.get("weather_event")
    if not isinstance(weather_event, str) or not weather_event.strip():
        raise InvalidWeatherEventError("weather_event must not be empty")

    severity = raw.get("severity")
    if not isinstance(severity, str) or severity not in SUPPORTED_SEVERITIES:
        raise UnsupportedSeverityError(
            f"Unsupported severity '{severity}'. Supported: {sorted(SUPPORTED_SEVERITIES)}"
        )

    affected_region = raw.get("affected_region")
    if not isinstance(affected_region, str) or not affected_region.strip():
        raise UnknownRegionError("affected_region must not be empty")

    affected_area_raw = raw.get("affected_area")
    if not isinstance(affected_area_raw, (dict, AffectedArea)):
        raise InvalidBoundingBoxError(
            "affected_area is required and must contain south, north, west, and east"
        )
    if isinstance(affected_area_raw, dict):
        required_keys = {"south", "north", "west", "east"}
        missing_keys = required_keys - set(affected_area_raw.keys())
        if missing_keys:
            raise InvalidBoundingBoxError(
                f"affected_area is missing required bounds: {sorted(missing_keys)}"
            )
        try:
            raw["affected_area"] = AffectedArea.model_validate(affected_area_raw)
        except InvalidBoundingBoxError:
            raise
        except Exception as exc:
            raise InvalidBoundingBoxError(f"Invalid affected_area bounds: {exc}") from exc

    elements_raw = raw.get("affected_network_elements")
    if not isinstance(elements_raw, list) or len(elements_raw) == 0:
        raise EmptyNetworkElementResultError("affected_network_elements must not be empty")

    try:
        return NetworkRiskEvent.model_validate(raw)
    except (
        InvalidBoundingBoxError,
        InvalidDataSourceError,
        InvalidTimestampError,
        InvalidWeatherEventError,
        UnsupportedSeverityError,
        UnknownRegionError,
        EmptyNetworkElementResultError,
        InvalidNetworkRiskEventError,
    ):
        raise
    except Exception as exc:
        raise InvalidNetworkRiskEventError(f"Failed to validate NetworkRiskEvent: {exc}") from exc
