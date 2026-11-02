"""Typed workflow stage state model and transition tracker for Phase 3 orchestration."""

from datetime import UTC, datetime

from contracts.action_result import (
    ORDERED_WORKFLOW_STAGES,
    WorkflowStage,
    WorkflowStageState,
    WorkflowStageStatus,
)


class WorkflowStateTracker:
    """Tracks ordered workflow stage transitions for a single run_id / trace_id."""

    def __init__(self, *, run_id: str, trace_id: str) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self._stages: dict[WorkflowStage, WorkflowStageState] = {
            stage: WorkflowStageState(
                stage=stage,
                status=WorkflowStageStatus.PENDING,
                summary=f"{stage.value} is pending.",
            )
            for stage in ORDERED_WORKFLOW_STAGES
        }

    def transition(
        self,
        stage: WorkflowStage,
        status: WorkflowStageStatus,
        summary: str,
    ) -> WorkflowStageState:
        state = WorkflowStageState(
            stage=stage,
            status=status,
            summary=summary,
            updated_at=datetime.now(UTC),
        )
        self._stages[stage] = state
        return state

    def get_stage(self, stage: WorkflowStage) -> WorkflowStageState:
        return self._stages[stage]

    def get_ordered_states(self) -> list[WorkflowStageState]:
        return [self._stages[stage] for stage in ORDERED_WORKFLOW_STAGES]


__all__ = [
    "ORDERED_WORKFLOW_STAGES",
    "WorkflowStage",
    "WorkflowStageState",
    "WorkflowStageStatus",
    "WorkflowStateTracker",
]
