"""Network Resilience Command Center Frontend Display & API Client Models (Phase 14).

Connects to the Command Center Backend via a configurable API base URL
(`COMMAND_CENTER_API_BASE_URL` / `VITE_API_BASE_URL`).

The frontend NEVER calls Agent 1 or Agent 2 directly and NEVER calculates risk scores
or governance decisions. All statuses come strictly from the backend's CommandCenterView.
"""

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts.action_result import CommandCenterView, EvidenceRecord

DASHBOARD_TITLE = "NETWORK RESILIENCE COMMAND CENTER"
WORKFLOW_PIPELINE_LABELS: tuple[str, ...] = (
    "WEATHER",
    "GEO",
    "ASSET",
    "RISK",
    "ACTION",
    "VERIFY",
    "EVIDENCE",
)


class GovernanceDisplayBanner(BaseModel):
    """Exact operator-facing governance banner rendered by the Command Center UI."""

    model_config = ConfigDict(extra="forbid")

    policy_decision_line: str
    approval_line: str
    execution_line: str
    verification_line: str


class EvidenceDisplayRow(BaseModel):
    """Sanitized row model for the VIEW EVIDENCE panel."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    run_id: str
    trace_id: str
    stage: str
    event_type: str
    status: str
    recorded_time: str
    source: str
    summary: str
    synthetic_indicator: str


class CommandCenterDashboardModel(BaseModel):
    """Complete read-only UI view model mapped from the backend CommandCenterView contract."""

    model_config = ConfigDict(extra="forbid")

    title: str = DASHBOARD_TITLE
    api_base_url: str
    run_id: str
    trace_id: str
    overall_risk: str
    workflow_status: str
    pipeline_steps: list[str] = Field(default_factory=lambda: list(WORKFLOW_PIPELINE_LABELS))
    governance_banner: GovernanceDisplayBanner
    panels: dict[str, Any]
    evidence_rows: list[EvidenceDisplayRow] = Field(default_factory=list)


def get_configured_api_base_url(override_url: str | None = None) -> str:
    """Return the configurable Command Center Backend API base URL."""
    url = (
        override_url
        or os.getenv("COMMAND_CENTER_API_BASE_URL")
        or os.getenv("VITE_API_BASE_URL")
        or "http://127.0.0.1:8000"
    )
    return url.rstrip("/")


def map_view_to_dashboard_model(
    view: CommandCenterView,
    *,
    api_base_url: str | None = None,
) -> CommandCenterDashboardModel:
    """Map a backend CommandCenterView into the read-only frontend display model.

    Performs zero risk or governance calculation—reads directly from the backend contract.
    """
    resolved_url = get_configured_api_base_url(api_base_url)

    policy_val = view.governance.decision.value
    approval_val = view.approval.status.value
    execution_val = view.execution.status.value
    verification_val = view.verification.status.value

    banner = GovernanceDisplayBanner(
        policy_decision_line=f"Policy Decision: {policy_val}",
        approval_line=f"Approval: {approval_val}",
        execution_line=f"Execution: {execution_val}",
        verification_line=f"Verification: {verification_val}",
    )

    evidence_rows = [
        format_evidence_row(ev)
        for ev in view.evidence
        if ev.run_id == view.header.run_id and ev.trace_id == view.header.trace_id
    ]

    panels: dict[str, Any] = {
        "Weather Alert": view.weather.model_dump(mode="json"),
        "Geo Correlation": view.geo.model_dump(mode="json"),
        "Asset Impact": {
            "affected_ne_count": view.assets.affected_ne_count,
            "critical_ne_count": view.assets.critical_ne_count,
            "affected_service_count": view.assets.affected_service_count,
            "unresolved_ne_ids": view.assets.unresolved_ne_ids,
        },
        "Risk Assessment": view.risk.model_dump(mode="json"),
        "Affected Assets": [a.model_dump(mode="json") for a in view.assets.asset_details],
        "Preventive Recommendation": view.recommendation.model_dump(mode="json"),
        "Governance": view.governance.model_dump(mode="json"),
        "Approval": view.approval.model_dump(mode="json"),
        "Execution": view.execution.model_dump(mode="json"),
        "Verification": view.verification.model_dump(mode="json"),
        "Evidence": [r.model_dump(mode="json") for r in evidence_rows],
        "Audit Trace": [e.model_dump(mode="json") for e in view.audit_trace],
    }

    return CommandCenterDashboardModel(
        title=DASHBOARD_TITLE,
        api_base_url=resolved_url,
        run_id=view.header.run_id,
        trace_id=view.header.trace_id,
        overall_risk=view.header.overall_risk,
        workflow_status=view.header.workflow_status,
        pipeline_steps=list(WORKFLOW_PIPELINE_LABELS),
        governance_banner=banner,
        panels=panels,
        evidence_rows=evidence_rows,
    )


def format_evidence_row(record: EvidenceRecord) -> EvidenceDisplayRow:
    """Format a single EvidenceRecord for the VIEW EVIDENCE modal without exposing secrets."""
    return EvidenceDisplayRow(
        evidence_id=record.evidence_id,
        run_id=record.run_id,
        trace_id=record.trace_id,
        stage=record.stage,
        event_type=record.event_type,
        status=record.status,
        recorded_time=record.recorded_at.isoformat(),
        source=record.source,
        summary=record.summary,
        synthetic_indicator="SYNTHETIC" if record.synthetic else "EXTERNAL",
    )


__all__ = [
    "DASHBOARD_TITLE",
    "WORKFLOW_PIPELINE_LABELS",
    "CommandCenterDashboardModel",
    "EvidenceDisplayRow",
    "GovernanceDisplayBanner",
    "format_evidence_row",
    "get_configured_api_base_url",
    "map_view_to_dashboard_model",
]
