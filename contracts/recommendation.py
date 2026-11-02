"""Agent 2 recommendation, enrichment, service impact, risk, and Bedrock contracts."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from common.tracing import generate_prefixed_id

from .base import TraceableContract
from .governance_decision import GovernanceDecision


class EnrichedNetworkElement(BaseModel):
    """Synthetic network element metadata returned by NetworkInventoryPort."""

    model_config = ConfigDict(extra="forbid")

    ne_id: str
    type: str
    region: str
    criticality: str
    services: list[str] = Field(default_factory=list)
    redundancy_available: bool
    operational_status: str = "ACTIVE"
    synthetic: bool = True


class EnrichmentResult(BaseModel):
    """Result of enriching Agent 1 network element IDs against inventory."""

    model_config = ConfigDict(extra="forbid")

    resolved_elements: list[EnrichedNetworkElement] = Field(default_factory=list)
    unresolved_element_ids: list[str] = Field(default_factory=list)
    region_mismatches: list[str] = Field(default_factory=list)
    synthetic: bool = True


class AffectedAssetDetail(BaseModel):
    """Asset detail entry in Agent2Result and RiskAssessment."""

    model_config = ConfigDict(extra="forbid")

    ne_id: str
    type: str
    region: str
    criticality: str
    services: list[str] = Field(default_factory=list)
    risk: str
    redundancy_available: bool = True
    operational_status: str = "ACTIVE"


class ServiceImpact(BaseModel):
    """Deterministic service impact analysis output."""

    model_config = ConfigDict(extra="forbid")

    affected_service_count: int
    affected_services: list[str] = Field(default_factory=list)
    critical_network_element_count: int
    high_criticality_network_element_count: int
    redundancy_available_for_all: bool
    service_risk: str
    impact_summary: str
    analysis_reasons: list[str] = Field(default_factory=list)
    unresolved_network_elements: list[str] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    """Deterministic risk calculation output."""

    model_config = ConfigDict(extra="forbid")

    weather_risk: str
    network_risk: str
    service_risk: str
    overall_risk: str
    risk_score: int = Field(ge=0, le=100)
    risk_reasons: list[str] = Field(default_factory=list)
    affected_asset_details: list[AffectedAssetDetail] = Field(default_factory=list)
    unresolved_network_elements: list[str] = Field(default_factory=list)


class RiskAssessmentSummary(BaseModel):
    """Risk assessment section embedded in Agent2Result."""

    model_config = ConfigDict(extra="forbid")

    risk_score: int = Field(ge=0, le=100)
    overall_risk: str
    weather_risk: str
    network_risk: str
    service_risk: str
    risk_reasons: list[str] = Field(default_factory=list)
    unresolved_network_elements: list[str] = Field(default_factory=list)


class PreventiveRecommendation(BaseModel):
    """Deterministic preventive action recommendation produced by Agent 2."""

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(default_factory=lambda: generate_prefixed_id("REC"))
    action: str
    target_network_elements: list[str] = Field(default_factory=list)
    rationale: str
    expected_outcome: str = "Mitigate weather-induced network and service risk."
    state_changing: bool
    human_approval_required: bool
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BedrockReasoningContext(TraceableContract):
    """Sanitized, credential-free input context passed to BedrockReasoningPort."""

    weather_event: str
    weather_severity: str
    affected_region: str
    affected_network_elements: list[str] = Field(default_factory=list)
    asset_criticalities: dict[str, str] = Field(default_factory=dict)
    affected_services: list[str] = Field(default_factory=list)
    unresolved_network_elements: list[str] = Field(default_factory=list)
    risk_score: int = Field(ge=0, le=100)
    overall_risk: str
    network_risk: str
    service_risk: str
    recommended_action: str
    target_network_elements: list[str] = Field(default_factory=list)
    deterministic_rationale: str


class BedrockReasoningOutput(BaseModel):
    """Validated generative explanation returned by Bedrock or deterministic fallback."""

    model_config = ConfigDict(extra="forbid")

    situation_summary: str = Field(min_length=1)
    risk_explanation: str = Field(min_length=1)
    recommendation_rationale: str = Field(min_length=1)
    operator_summary: str = Field(min_length=1)


class BedrockReasoningTrace(TraceableContract):
    """Auditable trace of a Bedrock reasoning invocation and fallback state."""

    model_id: str = "deterministic-fallback"
    operation: str = "generate_resilience_explanation"
    start_status: str = "STARTED"
    result_status: str = "FALLBACK_USED"
    fallback_used: bool = True
    error_reason: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reasoning: BedrockReasoningOutput


class ResilienceRecommendation(TraceableContract):
    """Backward-compatible traceable recommendation contract."""

    recommendation_id: str = Field(default_factory=lambda: generate_prefixed_id("REC"))
    risk_level: str
    recommended_action: str
    target_network_elements: list[str]
    governance_decision: GovernanceDecision = GovernanceDecision.PAUSE
    human_approval_required: bool
    state_changing: bool = True
    rationale: str | None = None
    expected_outcome: str | None = None
    confidence: float = 1.0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
