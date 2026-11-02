"""Policy evaluation boundary, isolated from Agent 2 business logic."""

from contracts.governance_decision import (
    GovernanceDecision,
    GovernanceEvaluation,
    GovernanceOutcomeSummary,
)
from contracts.recommendation import PreventiveRecommendation, ResilienceRecommendation

from .deny_rules import evaluate_deny_rules


class PolicyEngine:
    """Evaluates recommendations against governance rules to produce ALLOW, DENY, or PAUSE."""

    def evaluate_recommendation(
        self,
        *,
        run_id: str,
        trace_id: str,
        recommendation: PreventiveRecommendation,
        unresolved_elements: list[str] | None = None,
        force_deny: bool = False,
        force_deny_reason: str | None = None,
    ) -> GovernanceEvaluation:
        deny_matches = evaluate_deny_rules(
            recommendation,
            unresolved_elements=unresolved_elements,
            force_deny=force_deny,
            force_deny_reason=force_deny_reason,
        )
        if deny_matches:
            rule_ids = [m.rule_id for m in deny_matches]
            reasons = "; ".join(m.reason for m in deny_matches)
            return GovernanceEvaluation(
                run_id=run_id,
                trace_id=trace_id,
                governance_decision=GovernanceDecision.DENY,
                human_approval_required=False,
                policy_id=rule_ids[0],
                reason=reasons,
                matched_policy_rules=rule_ids,
            )

        # Critical governance rule: state-changing or approval-requiring actions MUST return PAUSE,
        # never ALLOW directly.
        if recommendation.state_changing or recommendation.human_approval_required:
            matched_rules: list[str] = []
            if recommendation.state_changing:
                matched_rules.append("POLICY-PAUSE-STATE-CHANGING-101")
            if recommendation.human_approval_required:
                matched_rules.append("POLICY-PAUSE-HUMAN-APPROVAL-102")
            return GovernanceEvaluation(
                run_id=run_id,
                trace_id=trace_id,
                governance_decision=GovernanceDecision.PAUSE,
                human_approval_required=True,
                policy_id=matched_rules[0],
                reason="Human approval is required before a state-changing or governed action.",
                matched_policy_rules=matched_rules,
            )

        return GovernanceEvaluation(
            run_id=run_id,
            trace_id=trace_id,
            governance_decision=GovernanceDecision.ALLOW,
            human_approval_required=False,
            policy_id="POLICY-ALLOW-NON-STATE-CHANGING-201",
            reason="Non-state-changing monitoring action is allowed by governance policy.",
            matched_policy_rules=["POLICY-ALLOW-NON-STATE-CHANGING-201"],
        )

    def evaluate(self, recommendation: ResilienceRecommendation) -> GovernanceEvaluation:
        """Backward-compatible entry point for ResilienceRecommendation contracts."""
        preventive = PreventiveRecommendation(
            recommendation_id=recommendation.recommendation_id,
            action=recommendation.recommended_action,
            target_network_elements=recommendation.target_network_elements,
            rationale=recommendation.rationale or "Evaluated via ResilienceRecommendation",
            state_changing=recommendation.state_changing,
            human_approval_required=recommendation.human_approval_required,
        )
        if recommendation.governance_decision == GovernanceDecision.DENY:
            return self.evaluate_recommendation(
                run_id=recommendation.run_id,
                trace_id=recommendation.trace_id,
                recommendation=preventive,
                force_deny=True,
                force_deny_reason=recommendation.rationale
                or "Explicit DENY on ResilienceRecommendation.",
            )
        return self.evaluate_recommendation(
            run_id=recommendation.run_id,
            trace_id=recommendation.trace_id,
            recommendation=preventive,
        )

    @staticmethod
    def to_summary(evaluation: GovernanceEvaluation) -> GovernanceOutcomeSummary:
        return GovernanceOutcomeSummary(
            decision=evaluation.governance_decision,
            reason=evaluation.reason,
            matched_policy_rules=list(evaluation.matched_policy_rules),
        )
