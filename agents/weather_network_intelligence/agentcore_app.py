"""Thin Amazon Bedrock AgentCore Runtime entrypoint for Agent 1:
Weather & Network Intelligence Agent.

Architecture:
AgentCore Runtime -> AgentCore Entry Point -> Existing Agent 1 -> NetworkRiskEvent
"""

import argparse
import os
from typing import Any

import structlog
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response

from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent
from common.constants import AGENT1_NAME
from common.exceptions import (
    ApplicationError,
    EmptyNetworkElementResultError,
    IntegrationError,
    InvalidWeatherEventError,
    UnknownRegionError,
    UnsupportedSeverityError,
)
from common.logging import configure_logging, log_agent_operation
from common.tracing import ensure_run_id, ensure_trace_id
from integrations.agentcore import create_agentcore_app, sanitize_runtime_error_message

configure_logging()
logger = structlog.get_logger(f"{AGENT1_NAME}.agentcore")

app = create_agentcore_app()
_agent1_instance: WeatherNetworkIntelligenceAgent | None = None


def get_agent1() -> WeatherNetworkIntelligenceAgent:
    """Return the singleton Agent 1 instance used by the AgentCore runtime entrypoint."""
    global _agent1_instance
    if _agent1_instance is None:
        _agent1_instance = WeatherNetworkIntelligenceAgent()
    return _agent1_instance


def set_agent1(agent: WeatherNetworkIntelligenceAgent | None) -> None:
    """Inject a custom or mocked Agent 1 instance for testing without duplicating business logic."""
    global _agent1_instance
    _agent1_instance = agent


def _extract_correlation_ids(payload: Any) -> tuple[str, str]:
    if isinstance(payload, dict):
        raw_run = payload.get("run_id")
        raw_trace = payload.get("trace_id")
        run_id = raw_run.strip() if isinstance(raw_run, str) and raw_run.strip() else None
        trace_id = raw_trace.strip() if isinstance(raw_trace, str) and raw_trace.strip() else None
        return ensure_run_id(run_id), ensure_trace_id(trace_id)
    return ensure_run_id(None), ensure_trace_id(None)


@app.entrypoint
def invoke(payload: Any) -> dict[str, Any] | Response:
    """AgentCore Runtime entrypoint delegating to WeatherNetworkIntelligenceAgent."""
    run_id, trace_id = _extract_correlation_ids(payload)

    if not isinstance(payload, dict):
        log_agent_operation(
            logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT1_NAME,
            operation="agentcore_invoke",
            result="invalid_payload_type",
        )
        return JSONResponse(
            {
                "status": "error",
                "error_type": "InvalidPayloadError",
                "message": "Request payload must be a JSON object.",
                "run_id": run_id,
                "trace_id": trace_id,
            },
            status_code=400,
        )

    try:
        event = get_agent1().process(payload)
        response_dict: dict[str, Any] = event.model_dump(mode="json")
        response_dict.pop("occurred_at", None)
        log_agent_operation(
            logger,
            run_id=event.run_id,
            trace_id=event.trace_id,
            agent_name=AGENT1_NAME,
            operation="agentcore_invoke",
            result="success",
            affected_elements=len(event.affected_network_elements),
        )
        return response_dict
    except (
        InvalidWeatherEventError,
        UnsupportedSeverityError,
        UnknownRegionError,
        EmptyNetworkElementResultError,
        ValidationError,
    ) as exc:
        safe_message = sanitize_runtime_error_message(str(exc))
        log_agent_operation(
            logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT1_NAME,
            operation="agentcore_invoke",
            result="validation_error",
            error_type=type(exc).__name__,
            reason=safe_message,
        )
        return JSONResponse(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": safe_message,
                "run_id": run_id,
                "trace_id": trace_id,
            },
            status_code=400,
        )
    except (IntegrationError, ApplicationError, RuntimeError, OSError, ValueError) as exc:
        safe_message = sanitize_runtime_error_message(str(exc))
        log_agent_operation(
            logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT1_NAME,
            operation="agentcore_invoke",
            result="processing_error",
            error_type=type(exc).__name__,
            reason=safe_message,
        )
        return JSONResponse(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": safe_message,
                "run_id": run_id,
                "trace_id": trace_id,
            },
            status_code=500,
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the Agent 1 Amazon Bedrock AgentCore Runtime server."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", os.getenv("AGENTCORE_PORT", "8080"))),
        help="TCP port to bind the AgentCore server (default: 8080).",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", None),
        help="Optional bind host override.",
    )
    args = parser.parse_args(argv)
    app.run(port=args.port, host=args.host)


if __name__ == "__main__":
    main()
