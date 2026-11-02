"""End-to-end local orchestrator connecting Agent 1 -> Agent 2 -> Command Center.

The orchestrator owns workflow sequencing only. Agent 1 and Agent 2 remain independently executable.
All data and actions are strictly synthetic.
"""

import argparse
import json
import sys
from enum import StrEnum
from typing import Any

import structlog

from agents.network_resilience.agent import NetworkResilienceAgent
from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent
from common.exceptions import ApplicationError
from common.logging import configure_logging, log_agent_operation
from contracts.action_result import (
    Agent2Result,
    CommandCenterView,
    EvidenceRecord,
    WorkflowStageState,
)
from contracts.governance_decision import ApprovalStatus
from contracts.network_risk_event import NetworkRiskEvent
from integrations.inventory_api import MockInventoryAdapter, MockNetworkInventoryAdapter

ORCHESTRATOR_NAME = "end_to_end_orchestrator"


class DemoScenario(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PAUSE = "pause"


SCENARIO_INPUTS: dict[DemoScenario, dict[str, Any]] = {
    # Scenario A - DENY: Region-Deny maps to an element missing from Agent 2 enrichment catalog,
    # triggering NO_AUTOMATED_ACTION -> Governance DENY -> Execution BLOCKED
    DemoScenario.DENY: {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": "WX-SCENARIO-DENY",
        "trace_id": "TRACE-SCENARIO-DENY-0001",
        "created_at": "2026-11-02T00:26:32.732111+00:00",
        "data_source": "synthetic",
        "weather_event": "SEVERE_STORM",
        "severity": "HIGH",
        "affected_region": "Demo-Region-Deny",
        "forecast": "Synthetic storm scenario in region with incomplete inventory for DENY demo.",
        "affected_area": {
            "south": 12.8,
            "north": 13.2,
            "west": 77.4,
            "east": 77.8,
        },
    },
    # Scenario B - ALLOW: Region-B with LOW weather severity produces MEDIUM risk ->
    # INCREASE_MONITORING (non-state-changing, human_approval_required=False) ->
    # Governance ALLOW -> Approval NOT_REQUIRED -> Execution EXECUTED -> Verification PASSED
    DemoScenario.ALLOW: {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": "WX-SCENARIO-ALLOW",
        "trace_id": "TRACE-SCENARIO-ALLOW-0002",
        "created_at": "2026-11-02T00:26:32.732111+00:00",
        "data_source": "synthetic",
        "weather_event": "HIGH_WIND",
        "severity": "LOW",
        "affected_region": "Region-B",
        "forecast": "Synthetic low-severity wind advisory for ALLOW monitoring demo.",
        "affected_area": {
            "south": 12.8,
            "north": 13.2,
            "west": 77.4,
            "east": 77.8,
        },
    },
    # Scenario C - PAUSE: Demo-Region-A with HIGH SEVERE_STORM produces CRITICAL risk ->
    # REROUTE_TRAFFIC (state_changing=True, human_approval_required=True) ->
    # Governance PAUSE -> Approval PENDING -> Execution BLOCKED (until explicit human approval)
    DemoScenario.PAUSE: {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": "WX-46329ae2b74b",
        "trace_id": "7a4106b675e4419f87b9b84d30a67ca4",
        "created_at": "2026-11-02T00:26:32.732111+00:00",
        "data_source": "synthetic",
        "weather_event": "SEVERE_STORM",
        "severity": "HIGH",
        "affected_region": "Demo-Region-A",
        "forecast": "Synthetic storm scenario for integration testing.",
        "affected_area": {
            "south": 12.8,
            "north": 13.2,
            "west": 77.4,
            "east": 77.8,
        },
    },
}


def build_default_agent1_inventory() -> MockInventoryAdapter:
    """Provide synthetic region mapping for Agent 1 across all deterministic scenarios."""
    data = dict(MockInventoryAdapter.DEFAULT_DATA)
    data["Demo-Region-Deny"] = ["NE-101", "NE-UNRESOLVED-999"]
    return MockInventoryAdapter(data)


class EndToEndOrchestrator:
    """Sequences Agent 1 -> NetworkRiskEvent -> Agent 2 -> CommandCenterView."""

    def __init__(
        self,
        *,
        agent1: WeatherNetworkIntelligenceAgent | None = None,
        agent2: NetworkResilienceAgent | None = None,
    ) -> None:
        self.agent1 = agent1 or WeatherNetworkIntelligenceAgent(
            inventory_adapter=build_default_agent1_inventory()
        )
        self.agent2 = agent2 or NetworkResilienceAgent(
            inventory_port=MockNetworkInventoryAdapter(),
            strict_region_check=False,
        )
        self.logger = structlog.get_logger(ORCHESTRATOR_NAME)
        self._pending_events: dict[str, NetworkRiskEvent] = {}
        self._results: dict[str, Agent2Result] = {}

    def run_weather_event(
        self,
        weather_payload: dict[str, Any],
        *,
        force_deny: bool = False,
        force_deny_reason: str | None = None,
    ) -> Agent2Result:
        """Execute Agent 1 followed by Agent 2 without automatic approval."""
        risk_event: NetworkRiskEvent = self.agent1.process(weather_payload)

        log_agent_operation(
            self.logger,
            run_id=risk_event.run_id,
            trace_id=risk_event.trace_id,
            agent_name=ORCHESTRATOR_NAME,
            operation="handoff_agent1_to_agent2",
            result="success",
            affected_elements=len(risk_event.affected_network_elements),
        )

        result = self.agent2.process(
            risk_event,
            simulated_approval_decision=None,
            force_deny=force_deny,
            force_deny_reason=force_deny_reason,
        )
        self._pending_events[result.run_id] = risk_event
        self._results[result.run_id] = result
        return result

    def submit_human_approval(
        self,
        *,
        run_id: str,
        decision: ApprovalStatus,
        approver_reference: str = "demo-approver",
        comments: str | None = None,
    ) -> Agent2Result:
        """Resume a PAUSED workflow after an explicit human approval decision."""
        if run_id not in self._pending_events:
            raise ApplicationError(f"Unknown run_id '{run_id}' for approval continuation.")

        risk_event = self._pending_events[run_id]
        result = self.agent2.process(
            risk_event,
            simulated_approval_decision=decision,
            approver_reference=approver_reference,
            approval_comments=comments,
        )
        self._results[run_id] = result
        return result

    def run_scenario(
        self,
        scenario: DemoScenario | str,
        *,
        approve_after_pause: bool = False,
        approval_decision: ApprovalStatus = ApprovalStatus.APPROVED,
        approver_reference: str = "demo-approver",
    ) -> Agent2Result:
        """Run one of the deterministic demo scenarios: allow, deny, or pause."""
        resolved_scenario = (
            scenario if isinstance(scenario, DemoScenario) else DemoScenario(scenario.lower())
        )
        weather_input = dict(SCENARIO_INPUTS[resolved_scenario])
        result = self.run_weather_event(weather_input)

        if resolved_scenario == DemoScenario.PAUSE and approve_after_pause:
            result = self.submit_human_approval(
                run_id=result.run_id,
                decision=approval_decision,
                approver_reference=approver_reference,
            )
        return result

    def get_result(self, run_id: str) -> Agent2Result | None:
        return self._results.get(run_id)

    def get_command_center_view(self, run_id: str) -> CommandCenterView:
        result = self._results.get(run_id)
        if result is None:
            raise ApplicationError(f"No workflow result found for run_id '{run_id}'.")
        return result.to_command_center_response()

    def get_evidence(self, *, run_id: str, trace_id: str | None = None) -> list[EvidenceRecord]:
        result = self._results.get(run_id)
        if result is None:
            raise ApplicationError(f"No workflow result found for run_id '{run_id}'.")
        if trace_id is not None and result.trace_id != trace_id:
            return []
        return list(result.evidence)

    def get_workflow_stages(self, run_id: str) -> list[WorkflowStageState]:
        result = self._results.get(run_id)
        if result is None:
            raise ApplicationError(f"No workflow result found for run_id '{run_id}'.")
        return list(result.workflow_stages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Phase 3 end-to-end local orchestration demo.")
    parser.add_argument(
        "--scenario",
        choices=["allow", "deny", "pause"],
        default="pause",
        help="Deterministic demo scenario to execute (allow, deny, pause).",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Simulate explicit human approval after PAUSE is reached.",
    )
    parser.add_argument(
        "--reject",
        action="store_true",
        help="Simulate explicit human rejection after PAUSE is reached.",
    )
    parser.add_argument(
        "--approver",
        default="demo-approver",
        help="Synthetic approver reference when --approve or --reject is used.",
    )
    args = parser.parse_args(argv)
    configure_logging()

    try:
        orchestrator = EndToEndOrchestrator()
        scenario = DemoScenario(args.scenario)
        result = orchestrator.run_scenario(scenario)

        if scenario == DemoScenario.PAUSE and (args.approve or args.reject):
            decision = ApprovalStatus.APPROVED if args.approve else ApprovalStatus.REJECTED
            result = orchestrator.submit_human_approval(
                run_id=result.run_id,
                decision=decision,
                approver_reference=args.approver,
            )

        cc_view = result.to_command_center_response()
        print(cc_view.model_dump_json(indent=2))
        return 0
    except (ApplicationError, ValueError, KeyError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
