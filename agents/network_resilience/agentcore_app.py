"""Thin Amazon Bedrock AgentCore Runtime entrypoint for Agent 2:
Network Resilience & Governed Action Agent.

Uses strictly ABSOLUTE imports so direct /var/task execution in AgentCore succeeds.
Delegates all domain, risk, Bedrock explanation, governance, approval, action,
verification, and evidence logic to the existing NetworkResilienceAgent.
"""

import argparse
import os
from typing import Any

import structlog
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response

from agents.network_resilience.agent import NetworkResilienceAgent
from common.config import Settings
from common.constants import AGENT2_NAME
from common.exceptions import (
    ApplicationError,
    EmptyNetworkElementResultError,
    GovernanceError,
    IntegrationError,
    InvalidBoundingBoxError,
    InvalidDataSourceError,
    InvalidNetworkRiskEventError,
    InvalidTimestampError,
    InvalidWeatherEventError,
    RegionMismatchError,
    UnknownRegionError,
    UnsupportedSeverityError,
)
from common.logging import configure_logging, log_agent_operation
from common.tracing import ensure_run_id, ensure_trace_id
from contracts.governance_decision import ApprovalStatus
from integrations.agentcore import create_agentcore_app, sanitize_runtime_error_message
from integrations.bedrock import ResilientBedrockReasoningService
from integrations.inventory_api import MockNetworkInventoryAdapter

configure_logging()
logger = structlog.get_logger(f"{AGENT2_NAME}.agentcore")

app = create_agentcore_app()
_agent2_instance: NetworkResilienceAgent | None = None


def get_agent2() -> NetworkResilienceAgent:
    """Return the singleton Agent 2 instance used by the AgentCore runtime entrypoint.

    In AgentCore Runtime, BEDROCK_ENABLED defaults to true (unless explicitly set to false)
    using model us.anthropic.claude-haiku-4-5-20251001-v1:0, while preserving safe
    deterministic fallback if Bedrock is unreachable.
    """
    global _agent2_instance
    if _agent2_instance is None:
        bedrock_env = os.getenv("BEDROCK_ENABLED", "true").strip().lower()
        bedrock_enabled = bedrock_env not in {"0", "false", "no"}
        model_id = os.getenv(
            "BEDROCK_MODEL_ID",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
        runtime_settings = Settings(
            aws_region=os.getenv("AWS_REGION", "us-east-2"),
            bedrock_enabled=bedrock_enabled,
            bedrock_model_id=model_id,
        )
        _agent2_instance = NetworkResilienceAgent(
            inventory_port=MockNetworkInventoryAdapter(),
            bedrock_reasoning_service=ResilientBedrockReasoningService(settings=runtime_settings),
            strict_region_check=False,
        )
    return _agent2_instance


def set_agent2(agent: NetworkResilienceAgent | None) -> None:
    """Inject a custom or mocked Agent 2 instance for testing without duplicating business logic."""
    global _agent2_instance
    _agent2_instance = agent


def _extract_correlation_ids(payload: Any) -> tuple[str, str]:
    if isinstance(payload, dict):
        raw_run = payload.get("run_id")
        raw_trace = payload.get("trace_id")
        run_id = raw_run.strip() if isinstance(raw_run, str) and raw_run.strip() else None
        trace_id = raw_trace.strip() if isinstance(raw_trace, str) and raw_trace.strip() else None
        return ensure_run_id(run_id), ensure_trace_id(trace_id)
    return ensure_run_id(None), ensure_trace_id(None)


def _extract_execution_options(
    payload: dict[str, Any],
) -> tuple[
    dict[str, Any],
    ApprovalStatus | None,
    str,
    str | None,
    bool,
    str | None,
]:
    """Extract optional explicit approval or governance control fields if wrapped or supplied."""
    cleaned = dict(payload)
    # Support either direct NetworkRiskEvent dict or {"network_risk_event": {...}, ...}
    if "network_risk_event" in cleaned and isinstance(cleaned["network_risk_event"], dict):
        event_dict = dict(cleaned["network_risk_event"])
    else:
        event_dict = cleaned

    raw_decision = cleaned.pop("simulated_approval_decision", None)
    event_dict.pop("simulated_approval_decision", None)
    approver_ref = str(
        cleaned.pop("approver_reference", event_dict.pop("approver_reference", "demo-approver"))
    )
    comments_raw = cleaned.pop("approval_comments", event_dict.pop("approval_comments", None))
    comments = str(comments_raw) if isinstance(comments_raw, str) else None
    force_deny = bool(cleaned.pop("force_deny", event_dict.pop("force_deny", False)))
    force_deny_reason_raw = cleaned.pop(
        "force_deny_reason", event_dict.pop("force_deny_reason", None)
    )
    force_deny_reason = (
        str(force_deny_reason_raw) if isinstance(force_deny_reason_raw, str) else None
    )

    approval_decision: ApprovalStatus | None = None
    if isinstance(raw_decision, str) and raw_decision.strip():
        approval_decision = ApprovalStatus(raw_decision.strip().upper())

    return (
        event_dict,
        approval_decision,
        approver_ref,
        comments,
        force_deny,
        force_deny_reason,
    )


@app.entrypoint
def invoke(payload: Any) -> dict[str, Any] | Response:
    """AgentCore Runtime entrypoint delegating to NetworkResilienceAgent."""
    run_id, trace_id = _extract_correlation_ids(payload)

    if not isinstance(payload, dict):
        log_agent_operation(
            logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="agentcore_invoke",
            result="invalid_payload_type",
        )
        return JSONResponse(
            {
                "status": "error",
                "error_type": "InvalidPayloadError",
                "message": "Request payload must be a JSON object representing NetworkRiskEvent.",
                "run_id": run_id,
                "trace_id": trace_id,
            },
            status_code=400,
        )

    try:
        (
            event_payload,
            approval_decision,
            approver_ref,
            approval_comments,
            force_deny,
            force_deny_reason,
        ) = _extract_execution_options(payload)

        result = get_agent2().process(
            event_payload,
            simulated_approval_decision=approval_decision,
            approver_reference=approver_ref,
            approval_comments=approval_comments,
            force_deny=force_deny,
            force_deny_reason=force_deny_reason,
        )
        response_dict: dict[str, Any] = result.model_dump(mode="json")
        log_agent_operation(
            logger,
            run_id=result.run_id,
            trace_id=result.trace_id,
            agent_name=AGENT2_NAME,
            operation="agentcore_invoke",
            result="success",
            governance_decision=result.governance.decision.value,
            approval_status=result.approval.status.value,
            execution_status=result.execution.status.value,
        )
        return response_dict
    except (
        InvalidBoundingBoxError,
        InvalidDataSourceError,
        InvalidTimestampError,
        InvalidWeatherEventError,
        UnsupportedSeverityError,
        UnknownRegionError,
        EmptyNetworkElementResultError,
        InvalidNetworkRiskEventError,
        RegionMismatchError,
        GovernanceError,
        ValidationError,
        ValueError,
    ) as exc:
        safe_message = sanitize_runtime_error_message(str(exc))
        log_agent_operation(
            logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
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
    except (IntegrationError, ApplicationError, RuntimeError, OSError) as exc:
        safe_message = sanitize_runtime_error_message(str(exc))
        log_agent_operation(
            logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
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
        description="Run the Agent 2 Amazon Bedrock AgentCore Runtime server."
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
