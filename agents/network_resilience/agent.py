"""Agent 2 orchestration: Network Resilience & Governed Action Agent.

Consumes the validated NetworkRiskEvent contract from Agent 1 without importing Agent 1
implementation modules, and delegates governance policy evaluation to the isolated
`governance` package.
"""

from typing import Any

import structlog

from common.constants import AGENT2_NAME
from common.logging import log_agent_operation
from contracts.action_result import (
    ORDERED_WORKFLOW_STAGES,
    Agent2Result,
    CommandCenterAssetsView,
    CommandCenterGeoView,
    CommandCenterHeaderView,
    CommandCenterStatusCard,
    CommandCenterView,
    CommandCenterWeatherView,
    ExecutionStatus,
    InputContextSummary,
    VerificationStatus,
    WorkflowStage,
    WorkflowStageState,
    WorkflowStageStatus,
)
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from contracts.network_risk_event import NetworkRiskEvent, validate_agent2_input
from contracts.recommendation import BedrockReasoningContext
from governance.approval import ApprovalWorkflow
from governance.audit import AuditRecorder
from governance.policy_engine import PolicyEngine
from integrations.bedrock import (
    BedrockReasoningPort,
    ResilientBedrockReasoningService,
)
from integrations.inventory_api import (
    MockNetworkInventoryAdapter,
    NetworkInventoryPort,
    enrich_network_elements,
)
from integrations.remediation_api import RemediationApiProvider

from .action_service import ActionService, SyntheticVerificationService
from .recommendation_engine import RecommendationEngine
from .risk_analyzer import RiskAnalyzer
from .service_impact import ServiceImpactAnalyzer


def _derive_workflow_status(
    governance_decision: GovernanceDecision,
    approval_status: ApprovalStatus,
    execution_status: ExecutionStatus,
) -> str:
    if governance_decision == GovernanceDecision.DENY:
        return "DENIED_AND_BLOCKED"
    if (
        governance_decision == GovernanceDecision.PAUSE
        and approval_status == ApprovalStatus.PENDING
    ):
        return "PAUSED_AWAITING_HUMAN_APPROVAL"
    if approval_status == ApprovalStatus.REJECTED:
        return "APPROVAL_REJECTED_BLOCKED"
    if approval_status == ApprovalStatus.EXPIRED:
        return "APPROVAL_EXPIRED_BLOCKED"
    if execution_status == ExecutionStatus.EXECUTED:
        return "COMPLETED_SYNTHETIC_EXECUTION"
    if execution_status == ExecutionStatus.FAILED:
        return "EXECUTION_FAILED"
    return "BLOCKED"


def _compute_workflow_stage_states(
    *,
    run_id: str,
    trace_id: str,
    unresolved_elements: list[str],
    governance_decision: GovernanceDecision,
    approval_status: ApprovalStatus,
    execution_status: ExecutionStatus,
    verification_status: VerificationStatus,
) -> list[WorkflowStageState]:
    del run_id, trace_id
    stages: dict[WorkflowStage, WorkflowStageState] = {
        stage: WorkflowStageState(stage=stage, status=WorkflowStageStatus.PENDING)
        for stage in ORDERED_WORKFLOW_STAGES
    }

    def set_stage(stage: WorkflowStage, status: WorkflowStageStatus, summary: str) -> None:
        stages[stage] = WorkflowStageState(stage=stage, status=status, summary=summary)

    set_stage(
        WorkflowStage.WEATHER,
        WorkflowStageStatus.COMPLETED,
        "Weather alert validated and ingested.",
    )
    set_stage(
        WorkflowStage.GEO_CORRELATION,
        WorkflowStageStatus.COMPLETED,
        "Geographic correlation and bounding box validated.",
    )
    set_stage(
        WorkflowStage.ASSET_IMPACT,
        WorkflowStageStatus.COMPLETED,
        (
            "Network element enrichment and service impact completed."
            if not unresolved_elements
            else f"Asset enrichment completed with unresolved IDs: {unresolved_elements}."
        ),
    )
    set_stage(
        WorkflowStage.RISK_ASSESSMENT,
        WorkflowStageStatus.COMPLETED,
        "Deterministic 0-100 risk assessment completed.",
    )
    set_stage(
        WorkflowStage.RECOMMENDATION,
        WorkflowStageStatus.COMPLETED,
        "Preventive action recommendation generated.",
    )

    if governance_decision == GovernanceDecision.DENY:
        set_stage(
            WorkflowStage.GOVERNANCE,
            WorkflowStageStatus.BLOCKED,
            "Governance policy evaluated to DENY.",
        )
        set_stage(
            WorkflowStage.APPROVAL,
            WorkflowStageStatus.BLOCKED,
            "Approval not required because governance decision is DENY.",
        )
        set_stage(
            WorkflowStage.ACTION,
            WorkflowStageStatus.BLOCKED,
            "Action execution blocked by governance DENY.",
        )
        set_stage(
            WorkflowStage.VERIFICATION,
            WorkflowStageStatus.BLOCKED,
            "Post-action verification blocked by governance DENY.",
        )
    elif governance_decision == GovernanceDecision.PAUSE:
        set_stage(
            WorkflowStage.GOVERNANCE,
            WorkflowStageStatus.PAUSED,
            "Governance policy evaluated to PAUSE (human approval required).",
        )
        if approval_status == ApprovalStatus.PENDING:
            set_stage(
                WorkflowStage.APPROVAL,
                WorkflowStageStatus.PAUSED,
                "Awaiting explicit human approval (PENDING).",
            )
            set_stage(
                WorkflowStage.ACTION,
                WorkflowStageStatus.BLOCKED,
                "Action execution blocked while human approval is PENDING.",
            )
            set_stage(
                WorkflowStage.VERIFICATION,
                WorkflowStageStatus.PENDING,
                "Verification has not started while approval is PENDING.",
            )
        elif approval_status in {ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
            set_stage(
                WorkflowStage.APPROVAL,
                WorkflowStageStatus.BLOCKED,
                f"Human approval was {approval_status.value}.",
            )
            set_stage(
                WorkflowStage.ACTION,
                WorkflowStageStatus.BLOCKED,
                f"Action execution blocked because approval was {approval_status.value}.",
            )
            set_stage(
                WorkflowStage.VERIFICATION,
                WorkflowStageStatus.BLOCKED,
                f"Verification blocked because approval was {approval_status.value}.",
            )
        else:
            set_stage(
                WorkflowStage.APPROVAL,
                WorkflowStageStatus.COMPLETED,
                "Explicit human approval granted (APPROVED).",
            )
            set_stage(
                WorkflowStage.ACTION,
                (
                    WorkflowStageStatus.COMPLETED
                    if execution_status == ExecutionStatus.EXECUTED
                    else WorkflowStageStatus.FAILED
                ),
                f"Controlled mock action status: {execution_status.value}.",
            )
            set_stage(
                WorkflowStage.VERIFICATION,
                (
                    WorkflowStageStatus.COMPLETED
                    if verification_status == VerificationStatus.PASSED
                    else WorkflowStageStatus.FAILED
                ),
                f"Synthetic verification status: {verification_status.value}.",
            )
    else:
        set_stage(
            WorkflowStage.GOVERNANCE,
            WorkflowStageStatus.COMPLETED,
            "Governance policy evaluated to ALLOW.",
        )
        set_stage(
            WorkflowStage.APPROVAL,
            WorkflowStageStatus.COMPLETED,
            "Human approval not required for ALLOW decision.",
        )
        set_stage(
            WorkflowStage.ACTION,
            (
                WorkflowStageStatus.COMPLETED
                if execution_status == ExecutionStatus.EXECUTED
                else WorkflowStageStatus.FAILED
            ),
            f"Controlled mock action status: {execution_status.value}.",
        )
        set_stage(
            WorkflowStage.VERIFICATION,
            (
                WorkflowStageStatus.COMPLETED
                if verification_status == VerificationStatus.PASSED
                else WorkflowStageStatus.FAILED
            ),
            f"Synthetic verification status: {verification_status.value}.",
        )

    set_stage(
        WorkflowStage.EVIDENCE,
        WorkflowStageStatus.COMPLETED,
        "Audit and stage evidence captured.",
    )
    return [stages[stage] for stage in ORDERED_WORKFLOW_STAGES]


class NetworkResilienceAgent:
    """Agent 2: Network Resilience & Governed Action Agent."""

    def __init__(
        self,
        *,
        inventory_port: NetworkInventoryPort | None = None,
        service_impact_analyzer: ServiceImpactAnalyzer | None = None,
        risk_analyzer: RiskAnalyzer | None = None,
        recommendation_engine: RecommendationEngine | None = None,
        policy_engine: PolicyEngine | None = None,
        approval_workflow: ApprovalWorkflow | None = None,
        action_service: ActionService | None = None,
        remediation_adapter: RemediationApiProvider | None = None,
        verification_service: SyntheticVerificationService | None = None,
        bedrock_reasoning_port: BedrockReasoningPort | None = None,
        bedrock_reasoning_service: ResilientBedrockReasoningService | None = None,
        strict_region_check: bool = True,
    ) -> None:
        self.inventory_port: NetworkInventoryPort = inventory_port or MockNetworkInventoryAdapter()
        self.service_impact_analyzer = service_impact_analyzer or ServiceImpactAnalyzer()
        self.risk_analyzer = risk_analyzer or RiskAnalyzer()
        self.recommendation_engine = recommendation_engine or RecommendationEngine()
        self.policy_engine = policy_engine or PolicyEngine()
        self.approval_workflow = approval_workflow or ApprovalWorkflow()
        self.action_service = action_service or ActionService(
            remediation_adapter=remediation_adapter,
            verification_service=verification_service,
        )
        self.bedrock_reasoning_service = (
            bedrock_reasoning_service
            or ResilientBedrockReasoningService(primary_adapter=bedrock_reasoning_port)
        )
        self.strict_region_check = strict_region_check
        self.logger = structlog.get_logger(AGENT2_NAME)

    def process(
        self,
        payload: dict[str, Any] | NetworkRiskEvent,
        *,
        simulated_approval_decision: ApprovalStatus | None = None,
        approver_reference: str = "demo-approver",
        approval_comments: str | None = None,
        force_deny: bool = False,
        force_deny_reason: str | None = None,
    ) -> Agent2Result:
        audit = AuditRecorder()

        # Stage 1 & 2: Receive and validate Agent 1 input
        event = validate_agent2_input(payload)
        run_id = event.run_id
        trace_id = event.trace_id

        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="AGENT1_INPUT_RECEIVED",
            stage="input_received",
            status="RECEIVED",
            source=AGENT2_NAME,
            summary=(
                f"Received NetworkRiskEvent for region '{event.affected_region}' "
                f"with {len(event.affected_network_elements)} network element(s)."
            ),
        )
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="INPUT_VALIDATED",
            stage="input_validation",
            status="VALIDATED",
            source=AGENT2_NAME,
            summary=(
                f"Validated schema_version={event.schema_version}, severity={event.severity}, "
                f"data_source={event.data_source}."
            ),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="validate_input",
            result="success",
        )

        # Stage 3: Network element enrichment
        enrichment = enrich_network_elements(
            self.inventory_port,
            event.affected_network_elements,
            event.affected_region,
            strict_region_check=self.strict_region_check,
        )
        enrichment_status = (
            "ENRICHED" if not enrichment.unresolved_element_ids else "PARTIALLY_ENRICHED"
        )
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="INVENTORY_ENRICHMENT_COMPLETED",
            stage="inventory_enrichment",
            status=enrichment_status,
            source="MockNetworkInventoryAdapter",
            summary=(
                f"Enriched {len(enrichment.resolved_elements)} element(s); "
                f"unresolved={enrichment.unresolved_element_ids}."
            ),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="enrich_inventory",
            result=enrichment_status.lower(),
            resolved_count=len(enrichment.resolved_elements),
            unresolved_count=len(enrichment.unresolved_element_ids),
        )

        # Stage 4: Service impact analysis
        service_impact = self.service_impact_analyzer.analyze(
            weather_severity=event.severity,
            enrichment=enrichment,
        )
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="SERVICE_IMPACT_ANALYZED",
            stage="service_impact_analysis",
            status=service_impact.service_risk,
            source=AGENT2_NAME,
            summary=service_impact.impact_summary,
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="analyze_service_impact",
            result=service_impact.service_risk,
        )

        # Stage 5: Risk assessment
        risk_assessment = self.risk_analyzer.assess(
            weather_severity=event.severity,
            total_requested_elements=len(event.affected_network_elements),
            enrichment=enrichment,
            service_impact=service_impact,
        )
        risk_summary = self.risk_analyzer.to_summary(risk_assessment)
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="RISK_ASSESSED",
            stage="risk_assessment",
            status=risk_assessment.overall_risk,
            source=AGENT2_NAME,
            summary=(
                f"Calculated overall_risk={risk_assessment.overall_risk} "
                f"(score={risk_assessment.risk_score}, "
                f"network_risk={risk_assessment.network_risk}, "
                f"service_risk={risk_assessment.service_risk})."
            ),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="assess_risk",
            result=risk_assessment.overall_risk,
            risk_score=risk_assessment.risk_score,
        )

        # Stage 6: Deterministic recommendation + Bedrock reasoning / explanation
        recommendation = self.recommendation_engine.recommend(
            requested_elements=event.affected_network_elements,
            enrichment=enrichment,
            service_impact=service_impact,
            risk_assessment=risk_assessment,
        )
        reasoning_context = BedrockReasoningContext(
            run_id=run_id,
            trace_id=trace_id,
            weather_event=event.weather_event,
            weather_severity=event.severity,
            affected_region=event.affected_region,
            affected_network_elements=list(event.affected_network_elements),
            asset_criticalities={
                elem.ne_id: elem.criticality for elem in enrichment.resolved_elements
            },
            affected_services=list(service_impact.affected_services),
            unresolved_network_elements=list(enrichment.unresolved_element_ids),
            risk_score=risk_assessment.risk_score,
            overall_risk=risk_assessment.overall_risk,
            network_risk=risk_assessment.network_risk,
            service_risk=risk_assessment.service_risk,
            recommended_action=recommendation.action,
            target_network_elements=list(recommendation.target_network_elements),
            deterministic_rationale=recommendation.rationale,
        )
        ai_reasoning_trace = self.bedrock_reasoning_service.generate_with_fallback(
            reasoning_context
        )
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="RECOMMENDATION_GENERATED",
            stage="recommendation",
            status=recommendation.action,
            source=AGENT2_NAME,
            summary=(
                f"Recommended '{recommendation.action}' on "
                f"{recommendation.target_network_elements} "
                f"(state_changing={recommendation.state_changing}, "
                f"human_approval_required={recommendation.human_approval_required}; "
                f"bedrock_model={ai_reasoning_trace.model_id}, "
                f"bedrock_status={ai_reasoning_trace.result_status}, "
                f"fallback_used={ai_reasoning_trace.fallback_used})."
            ),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="generate_recommendation",
            result=recommendation.action,
            bedrock_model_id=ai_reasoning_trace.model_id,
            bedrock_fallback_used=ai_reasoning_trace.fallback_used,
        )

        # Stage 7: Isolated governance policy evaluation
        governance_eval = self.policy_engine.evaluate_recommendation(
            run_id=run_id,
            trace_id=trace_id,
            recommendation=recommendation,
            unresolved_elements=enrichment.unresolved_element_ids,
            force_deny=force_deny,
            force_deny_reason=force_deny_reason,
        )
        governance_summary = self.policy_engine.to_summary(governance_eval)
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="GOVERNANCE_EVALUATED",
            stage="governance_decision",
            status=governance_eval.governance_decision.value,
            source="PolicyEngine",
            summary=(
                f"Governance decision={governance_eval.governance_decision.value}: "
                f"{governance_eval.reason} (rules={governance_eval.matched_policy_rules})."
            ),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="evaluate_governance",
            result=governance_eval.governance_decision.value,
            decision=governance_eval.governance_decision.value,
        )

        # Stage 8: Human approval workflow
        approval_record = self.approval_workflow.create_for_governance(
            run_id=run_id,
            trace_id=trace_id,
            recommendation_id=recommendation.recommendation_id,
            governance_decision=governance_eval.governance_decision,
        )
        if (
            governance_eval.governance_decision == GovernanceDecision.PAUSE
            and simulated_approval_decision is not None
        ):
            approval_record = self.approval_workflow.apply_decision(
                approval_record,
                decision=simulated_approval_decision,
                approver_reference=approver_reference,
                comments=approval_comments,
            )

        approval_summary = self.approval_workflow.to_summary(approval_record)
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="HUMAN_APPROVAL_EVALUATED",
            stage="human_approval",
            status=approval_record.decision.value,
            source="ApprovalWorkflow",
            summary=(
                f"Approval status={approval_record.decision.value} "
                f"(approver={approval_record.approver_reference or 'none'})."
            ),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="evaluate_approval",
            result=approval_record.decision.value,
            approval_status=approval_record.decision.value,
        )

        # Stage 9: Controlled mock action execution
        action_result = self.action_service.execute(
            run_id=run_id,
            trace_id=trace_id,
            recommendation=recommendation,
            governance_decision=governance_eval.governance_decision,
            approval_status=approval_record.decision,
        )
        execution_summary = self.action_service.to_execution_summary(action_result)
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="ACTION_EXECUTION_EVALUATED",
            stage="action_execution",
            status=action_result.execution_status.value,
            source="MockRemediationAdapter",
            summary=action_result.result_message,
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="execute_action",
            result=action_result.execution_status.value,
            execution_status=action_result.execution_status.value,
        )

        # Stage 10: Synthetic post-action verification
        verification_result = self.action_service.verify(
            run_id=run_id,
            trace_id=trace_id,
            action_result=action_result,
            governance_decision=governance_eval.governance_decision,
            approval_status=approval_record.decision,
        )
        verification_summary = self.action_service.verification_service.to_summary(
            verification_result
        )
        audit.record_stage(
            run_id=run_id,
            trace_id=trace_id,
            event_type="POST_ACTION_VERIFIED",
            stage="verification",
            status=verification_result.verification_status.value,
            source="SyntheticVerificationService",
            summary="; ".join(verification_result.verification_reasons),
        )
        log_agent_operation(
            self.logger,
            run_id=run_id,
            trace_id=trace_id,
            agent_name=AGENT2_NAME,
            operation="verify_action",
            result=verification_result.verification_status.value,
            verification_status=verification_result.verification_status.value,
        )

        evidence_records = audit.get_records()
        assert event.affected_area is not None

        input_context = InputContextSummary(
            weather_event=event.weather_event,
            severity=event.severity,
            affected_region=event.affected_region,
            affected_area=event.affected_area,
            forecast=event.forecast,
            affected_network_elements=list(event.affected_network_elements),
        )

        workflow_status = _derive_workflow_status(
            governance_eval.governance_decision,
            approval_record.decision,
            action_result.execution_status,
        )
        asset_mapping_status = (
            "COMPLETE" if not enrichment.unresolved_element_ids else "PARTIAL_UNRESOLVED"
        )
        workflow_stages = _compute_workflow_stage_states(
            run_id=run_id,
            trace_id=trace_id,
            unresolved_elements=enrichment.unresolved_element_ids,
            governance_decision=governance_eval.governance_decision,
            approval_status=approval_record.decision,
            execution_status=action_result.execution_status,
            verification_status=verification_result.verification_status,
        )

        command_center_view = CommandCenterView(
            header=CommandCenterHeaderView(
                run_id=run_id,
                trace_id=trace_id,
                schema_version=event.schema_version,
                team_code=event.team_code,
                overall_risk=risk_assessment.overall_risk,
                workflow_status=workflow_status,
            ),
            weather=CommandCenterWeatherView(
                event=event.weather_event,
                severity=event.severity,
                forecast=event.forecast,
                affected_region=event.affected_region,
            ),
            geo=CommandCenterGeoView(
                region=event.affected_region,
                affected_area=event.affected_area,
                geo_match_status="MATCHED",
                asset_mapping_status=asset_mapping_status,
            ),
            assets=CommandCenterAssetsView(
                affected_ne_count=len(risk_assessment.affected_asset_details),
                critical_ne_count=service_impact.critical_network_element_count,
                affected_service_count=service_impact.affected_service_count,
                unresolved_ne_ids=list(enrichment.unresolved_element_ids),
                asset_details=list(risk_assessment.affected_asset_details),
            ),
            risk=risk_summary,
            recommendation=recommendation,
            governance=governance_summary,
            approval=approval_summary,
            execution=execution_summary,
            verification=verification_summary,
            status_card=CommandCenterStatusCard(
                policy_decision=governance_eval.governance_decision,
                human_approval=approval_record.decision,
                execution=action_result.execution_status,
                verification=verification_result.verification_status,
            ),
            ai_reasoning=ai_reasoning_trace,
            workflow_stages=workflow_stages,
            evidence=list(evidence_records),
            audit_trace=list(evidence_records),
        )

        return Agent2Result(
            schema_version=event.schema_version,
            team_code=event.team_code,
            run_id=run_id,
            trace_id=trace_id,
            created_at=event.created_at,
            data_source=event.data_source,
            input_context=input_context,
            affected_assets=list(risk_assessment.affected_asset_details),
            unresolved_network_elements=list(enrichment.unresolved_element_ids),
            service_impact=service_impact,
            risk_assessment=risk_summary,
            recommendation=recommendation,
            ai_reasoning=ai_reasoning_trace,
            governance=governance_summary,
            approval=approval_summary,
            execution=execution_summary,
            verification=verification_summary,
            workflow_stages=workflow_stages,
            evidence=list(evidence_records),
            command_center=command_center_view,
        )
