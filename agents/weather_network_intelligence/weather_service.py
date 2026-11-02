"""Validation and normalization for synthetic weather alerts."""

from common.constants import SUPPORTED_SEVERITIES, SUPPORTED_WEATHER_EVENTS
from common.exceptions import InvalidWeatherEventError, UnknownRegionError, UnsupportedSeverityError
from contracts.network_risk_event import WeatherAlertInput


class WeatherService:
    def normalize(self, payload: dict[str, object] | WeatherAlertInput) -> WeatherAlertInput:
        try:
            if isinstance(payload, WeatherAlertInput):
                alert = payload
            else:
                normalized = dict(payload)
                if not normalized.get("run_id"):
                    normalized.pop("run_id", None)
                if not normalized.get("trace_id"):
                    normalized.pop("trace_id", None)
                alert = WeatherAlertInput.model_validate(normalized)
        except Exception as exc:
            raise UnknownRegionError("Weather input is missing required fields") from exc
        if alert.weather_event not in SUPPORTED_WEATHER_EVENTS:
            raise InvalidWeatherEventError(f"Unsupported weather event: {alert.weather_event}")
        if alert.severity not in SUPPORTED_SEVERITIES:
            raise UnsupportedSeverityError(f"Unsupported severity: {alert.severity}")
        if not alert.affected_region.strip():
            raise UnknownRegionError("Affected region must not be empty")
        return alert
