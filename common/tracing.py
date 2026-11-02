"""Helpers for preserving run and trace correlation context."""

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class TraceContext:
    run_id: str
    trace_id: str
    schema_version: str = "0.1"
    team_code: str = "TEAM_PENDING"
    data_source: str = "synthetic"


def ensure_run_id(run_id: str | None = None) -> str:
    return run_id or f"WX-{uuid4().hex[:8].upper()}"


def ensure_trace_id(trace_id: str | None = None) -> str:
    return trace_id or f"TRACE-{uuid4().hex[:8].upper()}"


def generate_prefixed_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10].upper()}"
