"""Governance outcomes, approval statuses, and auditable policy evaluation."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .base import TraceableContract


class GovernanceDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    PAUSE = "PAUSE"


class ApprovalStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class GovernanceEvaluation(TraceableContract):
    governance_decision: GovernanceDecision
    human_approval_required: bool
    policy_id: str
    reason: str
    matched_policy_rules: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GovernanceOutcomeSummary(BaseModel):
    """Governance section embedded in Agent2Result."""

    model_config = ConfigDict(extra="forbid")

    decision: GovernanceDecision
    reason: str
    matched_policy_rules: list[str] = Field(default_factory=list)


class ApprovalRecord(TraceableContract):
    """Full auditable human approval record."""

    approval_id: str
    recommendation_id: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    decision: ApprovalStatus = ApprovalStatus.PENDING
    approver_reference: str | None = None
    comments: str | None = None


class ApprovalOutcomeSummary(BaseModel):
    """Approval section embedded in Agent2Result."""

    model_config = ConfigDict(extra="forbid")

    status: ApprovalStatus
    approval_id: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    approver_reference: str | None = None
    comments: str | None = None
