"""Unit and ASGI HTTP tests for the Agent 1 Amazon Bedrock AgentCore Runtime entrypoint wrapper.

Verifies:
1. Accepts a valid Agent 1 payload
2. Invokes the existing WeatherNetworkIntelligenceAgent without duplicating logic
3. Returns the existing NetworkRiskEvent contract shape
4. Preserves run_id
5. Preserves trace_id
6. Rejects malformed input cleanly without leaking stack traces or secrets
7. Exposes working GET /ping and POST /invocations endpoints on the BedrockAgentCoreApp
"""

import ast
import json
from pathlib import Path
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Scope

from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent
from agents.weather_network_intelligence.agentcore_app import (
    app,
    invoke,
    set_agent1,
)
from common.exceptions import InventoryLookupError
from contracts.network_risk_event import NetworkRiskEvent, validate_agent2_input


class SpyAgent1(WeatherNetworkIntelligenceAgent):
    """Spy subclass of the existing Agent 1 to prove the wrapper delegates directly to Agent 1."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self.last_payload: dict[str, object] | None = None

    def process(self, payload: dict[str, object]) -> NetworkRiskEvent:
        self.call_count += 1
        self.last_payload = payload
        return super().process(payload)


class FailingInventoryAgent1(WeatherNetworkIntelligenceAgent):
    """Simulates an internal inventory/Open-Meteo failure containing a sensitive token string."""

    def process(self, payload: dict[str, object]) -> NetworkRiskEvent:
        del payload
        raise InventoryLookupError("Upstream failure with aws_session_token=SUPER_SECRET_TOKEN_999")


def test_agentcore_wrapper_invokes_existing_agent1_and_returns_network_risk_event() -> None:
    spy = SpyAgent1()
    set_agent1(spy)
    try:
        payload: dict[str, Any] = {
            "schema_version": "0.1",
            "team_code": "TEAM_PENDING",
            "run_id": "WX-AGENTCORE-01",
            "trace_id": "TRACE-AGENTCORE-01",
            "data_source": "external",
            "weather_event": "SEVERE_STORM",
            "severity": "HIGH",
            "affected_region": "Demo-Region-A",
            "forecast": "Severe storm over Demo-Region-A.",
            "affected_area": {
                "south": 12.8,
                "north": 13.2,
                "west": 77.4,
                "east": 77.8,
            },
        }
        response = invoke(payload)
        assert isinstance(response, dict)
        assert spy.call_count == 1
        assert spy.last_payload == payload

        # Verify preserved correlation and metadata fields
        assert response["schema_version"] == "0.1"
        assert response["team_code"] == "TEAM_PENDING"
        assert response["run_id"] == "WX-AGENTCORE-01"
        assert response["trace_id"] == "TRACE-AGENTCORE-01"
        assert response["data_source"] == "external"
        assert response["weather_event"] == "SEVERE_STORM"
        assert response["severity"] == "HIGH"
        assert response["affected_region"] == "Demo-Region-A"
        assert response["affected_network_elements"] == ["NE-101", "NE-102"]

        # Confirm the returned dict validates against the NetworkRiskEvent contract
        validated = validate_agent2_input(response)
        assert isinstance(validated, NetworkRiskEvent)
        assert validated.run_id == "WX-AGENTCORE-01"
        assert validated.trace_id == "TRACE-AGENTCORE-01"
    finally:
        set_agent1(None)


def test_agentcore_wrapper_generates_correlation_ids_when_omitted() -> None:
    set_agent1(None)
    response = invoke(
        {
            "weather_event": "SEVERE_STORM",
            "severity": "HIGH",
            "affected_region": "Region-A",
        }
    )
    assert isinstance(response, dict)
    assert str(response["run_id"]).startswith("WX-")
    assert str(response["trace_id"]).startswith("TRACE-")
    assert response["affected_network_elements"] == ["NE-101", "NE-102", "NE-103"]


def test_agentcore_wrapper_rejects_malformed_input_cleanly() -> None:
    set_agent1(None)

    # Non-dict payload
    non_dict_resp = invoke(["not", "an", "object"])
    assert isinstance(non_dict_resp, JSONResponse)
    assert non_dict_resp.status_code == 400
    body_non_dict = json.loads(bytes(non_dict_resp.body).decode("utf-8"))
    assert body_non_dict["status"] == "error"
    assert body_non_dict["error_type"] == "InvalidPayloadError"

    # Invalid weather event
    bad_event_resp = invoke(
        {
            "run_id": "WX-ERR-1",
            "trace_id": "TRACE-ERR-1",
            "weather_event": "METEOR_SHOWER",
            "severity": "HIGH",
            "affected_region": "Region-A",
        }
    )
    assert isinstance(bad_event_resp, JSONResponse)
    assert bad_event_resp.status_code == 400
    body_bad_event = json.loads(bytes(bad_event_resp.body).decode("utf-8"))
    assert body_bad_event["run_id"] == "WX-ERR-1"
    assert body_bad_event["trace_id"] == "TRACE-ERR-1"
    assert body_bad_event["error_type"] == "InvalidWeatherEventError"

    # Unknown region
    bad_region_resp = invoke(
        {
            "run_id": "WX-ERR-2",
            "trace_id": "TRACE-ERR-2",
            "weather_event": "SEVERE_STORM",
            "severity": "HIGH",
            "affected_region": "Unknown-Region-99",
        }
    )
    assert isinstance(bad_region_resp, JSONResponse)
    assert bad_region_resp.status_code == 400
    body_bad_region = json.loads(bytes(bad_region_resp.body).decode("utf-8"))
    assert body_bad_region["error_type"] == "UnknownRegionError"


def test_agentcore_wrapper_redacts_secrets_on_processing_errors() -> None:
    set_agent1(FailingInventoryAgent1())
    try:
        err_resp = invoke(
            {
                "run_id": "WX-FAIL-500",
                "trace_id": "TRACE-FAIL-500",
                "weather_event": "SEVERE_STORM",
                "severity": "HIGH",
                "affected_region": "Region-A",
            }
        )
        assert isinstance(err_resp, JSONResponse)
        assert err_resp.status_code == 500
        body = json.loads(bytes(err_resp.body).decode("utf-8"))
        assert body["run_id"] == "WX-FAIL-500"
        assert body["trace_id"] == "TRACE-FAIL-500"
        assert body["error_type"] == "InventoryLookupError"
        assert "SUPER_SECRET_TOKEN_999" not in body["message"]
        assert "[REDACTED]" in body["message"]
    finally:
        set_agent1(None)


def test_agentcore_http_ping_and_invocations_routes() -> None:
    """Verify the BedrockAgentCoreApp routes (/ping and POST /invocations) directly via ASGI."""
    set_agent1(None)

    async def _run_asgi_checks() -> None:
        # 1. GET /ping
        ping_scope: Scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/ping",
            "raw_path": b"/ping",
            "query_string": b"",
            "headers": [],
        }
        ping_req = Request(ping_scope)
        ping_resp: Any = app._handle_ping(ping_req)  # type: ignore[no-untyped-call]
        assert ping_resp.status_code == 200
        ping_json = json.loads(bytes(ping_resp.body).decode("utf-8"))
        assert ping_json["status"] == "Healthy"

        # 2. POST /invocations
        payload_bytes = json.dumps(
            {
                "run_id": "WX-ASGI-01",
                "trace_id": "TRACE-ASGI-01",
                "weather_event": "SEVERE_STORM",
                "severity": "HIGH",
                "affected_region": "Demo-Region-A",
            }
        ).encode("utf-8")

        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        inv_scope: Scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/invocations",
            "raw_path": b"/invocations",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        }
        inv_req = Request(inv_scope, receive)
        inv_resp: Any = await app._handle_invocation(inv_req)  # type: ignore[no-untyped-call]
        assert inv_resp.status_code == 200
        inv_json = json.loads(bytes(inv_resp.body).decode("utf-8"))
        assert inv_json["run_id"] == "WX-ASGI-01"
        assert inv_json["trace_id"] == "TRACE-ASGI-01"
        assert inv_json["affected_network_elements"] == ["NE-101", "NE-102"]

    anyio.run(_run_asgi_checks)


def test_agentcore_entrypoint_uses_only_absolute_imports() -> None:
    """Ensure agentcore_app.py has zero relative imports so direct /var/task execution works."""
    entrypoint_path = (
        Path(__file__).resolve().parents[2]
        / "agents"
        / "weather_network_intelligence"
        / "agentcore_app.py"
    )
    source = entrypoint_path.read_text(encoding="utf-8")
    assert (
        "from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent"
        in source
    )
    assert "from .agent import WeatherNetworkIntelligenceAgent" not in source

    tree = ast.parse(source, filename=str(entrypoint_path))
    relative_imports = [
        node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert relative_imports == [], f"Relative imports found in agentcore_app.py: {relative_imports}"
