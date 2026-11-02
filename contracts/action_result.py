"""Controlled remediation execution, verification, evidence, and Agent2Result contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.constants import (
    DEFAULT_DATA_SOURCE,
    DEFAULT_SCHEMA_VERSION,
    DEFAULT_TEAM_CODE,
)
from common.tracing import generate_prefixed_id

from .base import TraceableContract
from .governance_decision import (
    ApprovalOutcomeSummary,
    ApprovalStatus,
    GovernanceDecision,
    GovernanceOutcomeSummary,
)
from .network_risk_event import AffectedArea
from .recommendation import (
    AffectedAssetDetail,
    BedrockReasoningContext,
    BedrockReasoningOutput,
    BedrockReasoningTrace,
    PreventiveRecommendation,
    RiskAssessmentSummary,
    ServiceImpact,
)


class WorkflowStage(StrEnum):
    WEATHER = "WEATHER"
    GEO_CORRELATION = "GEO_CORRELATION"
    ASSET_IMPACT = "ASSET_IMPACT"
    RISK_ASSESSMENT = "RISK_ASSESSMENT"
    RECOMMENDATION = "RECOMMENDATION"
    GOVERNANCE = "GOVERNANCE"
    APPROVAL = "APPROVAL"
    ACTION = "ACTION"
    VERIFICATION = "VERIFICATION"
    EVIDENCE = "EVIDENCE"


ORDERED_WORKFLOW_STAGES: tuple[WorkflowStage, ...] = (
    WorkflowStage.WEATHER,
    WorkflowStage.GEO_CORRELATION,
    WorkflowStage.ASSET_IMPACT,
    WorkflowStage.RISK_ASSESSMENT,
    WorkflowStage.RECOMMENDATION,
    WorkflowStage.GOVERNANCE,
    WorkflowStage.APPROVAL,
    WorkflowStage.ACTION,
    WorkflowStage.VERIFICATION,
    WorkflowStage.EVIDENCE,
)


class WorkflowStageStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"


class WorkflowStageState(BaseModel):
    """Typed state for a single workflow stage exposed to Command Center."""

    model_config = ConfigDict(extra="forbid")

    stage: WorkflowStage
    status: WorkflowStageStatus = WorkflowStageStatus.PENDING
    summary: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class VerificationStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ActionResult(TraceableContract):
    """Typed result of controlled mock remediation execution."""

    action_id: str = Field(default_factory=lambda: generate_prefixed_id("ACT"))
    recommendation_id: str = Field(default_factory=lambda: generate_prefixed_id("REC"))
    action: str = ""
    recommended_action: str | None = None
    target_network_elements: list[str] = Field(default_factory=list)
    governance_decision: GovernanceDecision | None = None
    execution_status: ExecutionStatus = ExecutionStatus.BLOCKED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_message: str = ""
    changed_state: bool = False
    synthetic: bool = True
    approved_by: str | None = None
    details: str | None = None

    @model_validator(mode="after")
    def sync_legacy_action_fields(self) -> "ActionResult":
        if not self.action and self.recommended_action:
            self.action = self.recommended_action
        elif self.action and not self.recommended_action:
            self.recommended_action = self.action
        if not self.result_message and self.details:
            self.result_message = self.details
        return self


class ExecutionOutcomeSummary(BaseModel):
    """Execution section embedded in Agent2Result."""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    action_id: str | None = None
    action: str | None = None
    target_network_elements: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_message: str | None = None
    changed_state: bool = False
    synthetic: bool = True


class VerificationResult(TraceableContract):
    """Synthetic post-action verification result."""

    verification_id: str = Field(default_factory=lambda: generate_prefixed_id("VER"))
    action_id: str
    action_result_verified: bool
    asset_state: str
    post_action_check: str
    outcome_verified: bool
    verification_status: VerificationStatus
    verification_reasons: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    synthetic: bool = True


class VerificationOutcomeSummary(BaseModel):
    """Verification section embedded in Agent2Result."""

    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus
    verification_id: str | None = None
    action_id: str | None = None
    action_result_verified: bool = False
    asset_state: str | None = None
    post_action_check: str | None = None
    outcome_verified: bool = False
    verification_reasons: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    synthetic: bool = True


class EvidenceRecord(TraceableContract):
    """Immutable audit/evidence entry for a workflow stage."""

    evidence_id: str = Field(default_factory=lambda: generate_prefixed_id("EVD"))
    event_type: str
    stage: str
    status: str
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    summary: str
    synthetic: bool = True


class InputContextSummary(BaseModel):
    """Preserved Agent 1 input context inside Agent2Result."""

    model_config = ConfigDict(extra="forbid")

    weather_event: str
    severity: str
    affected_region: str
    affected_area: AffectedArea
    forecast: str | None = None
    affected_network_elements: list[str] = Field(default_factory=list)


class CommandCenterHeaderView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    trace_id: str
    schema_version: str = DEFAULT_SCHEMA_VERSION
    team_code: str = DEFAULT_TEAM_CODE
    overall_risk: str
    workflow_status: str


class CommandCenterWeatherView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str
    severity: str
    forecast: str | None = None
    affected_region: str


class CommandCenterGeoView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str
    affected_area: AffectedArea
    geo_match_status: str
    asset_mapping_status: str


class CommandCenterAssetsView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    affected_ne_count: int
    critical_ne_count: int
    affected_service_count: int
    unresolved_ne_ids: list[str] = Field(default_factory=list)
    asset_details: list[AffectedAssetDetail] = Field(default_factory=list)


class CommandCenterStatusCard(BaseModel):
    """Explicit governance/approval/execution status card for Command Center display."""

    model_config = ConfigDict(extra="forbid")

    policy_decision: GovernanceDecision
    human_approval: ApprovalStatus
    execution: ExecutionStatus
    verification: VerificationStatus


class CommandCenterView(BaseModel):
    """Pre-computed Command Center dashboard view for zero-calculation frontend rendering."""

    model_config = ConfigDict(extra="forbid")

    header: CommandCenterHeaderView
    weather: CommandCenterWeatherView
    geo: CommandCenterGeoView
    assets: CommandCenterAssetsView
    risk: RiskAssessmentSummary
    recommendation: PreventiveRecommendation
    governance: GovernanceOutcomeSummary
    approval: ApprovalOutcomeSummary
    execution: ExecutionOutcomeSummary
    verification: VerificationOutcomeSummary
    status_card: CommandCenterStatusCard | None = None
    ai_reasoning: BedrockReasoningTrace | None = None
    workflow_stages: list[WorkflowStageState] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    audit_trace: list[EvidenceRecord] = Field(default_factory=list)


class Agent2Result(TraceableContract):
    """Complete typed output produced by Agent 2 (Network Resilience & Governed Action Agent)."""

    schema_version: str = Field(default=DEFAULT_SCHEMA_VERSION, min_length=1)
    team_code: str = Field(default=DEFAULT_TEAM_CODE, min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data_source: str = Field(default=DEFAULT_DATA_SOURCE, min_length=1)

    input_context: InputContextSummary
    affected_assets: list[AffectedAssetDetail] = Field(default_factory=list)
    unresolved_network_elements: list[str] = Field(default_factory=list)
    service_impact: ServiceImpact
    risk_assessment: RiskAssessmentSummary
    recommendation: PreventiveRecommendation
    ai_reasoning: BedrockReasoningTrace | None = None
    governance: GovernanceOutcomeSummary
    approval: ApprovalOutcomeSummary
    execution: ExecutionOutcomeSummary
    verification: VerificationOutcomeSummary
    workflow_stages: list[WorkflowStageState] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    command_center: CommandCenterView | None = None

    def to_command_center_response(self) -> CommandCenterView:
        """Return the Command Center aggregation view for backend/dashboard consumption."""
        if self.command_center is not None:
            return self.command_center
        raise ValueError("command_center view was not populated")


class WorkflowSSEEvent(TraceableContract):
    """Typed Server-Sent Event (SSE) contract for workflow-level progress streaming."""

    schema_version: str = Field(default=DEFAULT_SCHEMA_VERSION, min_length=1)
    event_id: str = Field(default_factory=lambda: generate_prefixed_id("EVT"))
    event_type: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stage: str = Field(min_length=1)
    status: str = Field(min_length=1)
    source: str = Field(min_length=1)
    message: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    terminal: bool = False
    synthetic: bool = True


__all__ = [
    "ORDERED_WORKFLOW_STAGES",
    "ActionResult",
    "Agent2Result",
    "BedrockReasoningContext",
    "BedrockReasoningOutput",
    "BedrockReasoningTrace",
    "CommandCenterAssetsView",
    "CommandCenterGeoView",
    "CommandCenterHeaderView",
    "CommandCenterStatusCard",
    "CommandCenterView",
    "CommandCenterWeatherView",
    "EvidenceRecord",
    "ExecutionOutcomeSummary",
    "ExecutionStatus",
    "InputContextSummary",
    "VerificationOutcomeSummary",
    "VerificationResult",
    "VerificationStatus",
    "WorkflowSSEEvent",
    "WorkflowStage",
    "WorkflowStageState",
    "WorkflowStageStatus",
]
