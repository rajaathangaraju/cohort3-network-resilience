"""Weather provider protocol and safe mock boundary."""

from typing import Protocol


class WeatherApiProvider(Protocol):
    def get_alert(self, region: str) -> dict[str, object]: ...


class MockWeatherApiClient:
    def get_alert(self, region: str) -> dict[str, object]:
        # TODO: return test fixture data, never live operational data.
        return {}


class MockWeatherAdapter:
    """Synthetic weather adapter; no external weather API is called."""

    def get_event(self) -> dict[str, str]:
        return {
            "weather_event": "SEVERE_STORM",
            "severity": "HIGH",
            "affected_region": "Region-A",
        }
