"""Base model enforcing correlation identifiers on every message."""

from pydantic import BaseModel, ConfigDict, Field

from common.tracing import ensure_run_id, ensure_trace_id


class TraceableContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(default_factory=ensure_run_id, min_length=1)
    trace_id: str = Field(default_factory=ensure_trace_id, min_length=1)
