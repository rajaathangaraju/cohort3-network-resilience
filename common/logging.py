"""CloudWatch-ready JSON logging helpers.

Emits structured JSON records containing run_id, trace_id, agent_name, operation, and result,
and flushes immediately so Amazon Bedrock AgentCore Runtime captures every event in CloudWatch.
"""

import contextlib
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

import structlog

_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "access_key",
    "session_token",
    "authorization",
    "cookie",
)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def log_agent_operation(
    logger: Any,
    *,
    run_id: str,
    trace_id: str,
    agent_name: str,
    operation: str,
    result: str,
    **extra: object,
) -> None:
    """Emit a structured JSON log record with explicit operation and correlation fields.

    Writes through structlog (stderr) and flushes standard streams so both local CLI runs
    (where stdout carries pure JSON output) and AWS AgentCore container log collectors capture
    every event immediately without buffering lag.
    """
    safe_extra = {
        key: value
        for key, value in extra.items()
        if not any(secret in key.lower() for secret in _SENSITIVE_KEY_PARTS)
    }
    logger.info(
        operation,
        run_id=run_id,
        trace_id=trace_id,
        agent_name=agent_name,
        operation=operation,
        result=result,
        **safe_extra,
    )
    with contextlib.suppress(OSError):
        sys.stderr.flush()

    # Also emit to root logger if handlers are attached (e.g. AgentCore / uvicorn log handler)
    root_logger = logging.getLogger("cohort3.agentcore")
    if root_logger.handlers:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "trace_id": trace_id,
            "agent_name": agent_name,
            "operation": operation,
            "result": result,
            **safe_extra,
        }
        root_logger.info(json.dumps(record))
