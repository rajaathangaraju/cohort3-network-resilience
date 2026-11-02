"""Hard-stop governance deny rules evaluated by PolicyEngine."""

from dataclasses import dataclass

from common.constants import SUPPORTED_RECOMMENDATIONS
from contracts.recommendation import PreventiveRecommendation


@dataclass(frozen=True)
class DenyRuleMatch:
    rule_id: str
    reason: str


def evaluate_deny_rules(
    recommendation: PreventiveRecommendation,
    *,
    unresolved_elements: list[str] | None = None,
    force_deny: bool = False,
    force_deny_reason: str | None = None,
) -> list[DenyRuleMatch]:
    """Evaluate hard-stop deny rules against a recommendation and inventory context."""
    matches: list[DenyRuleMatch] = []
    unresolved = unresolved_elements or []

    if force_deny:
        matches.append(
            DenyRuleMatch(
                rule_id="POLICY-DENY-FORCED-000",
                reason=force_deny_reason or "Action denied by explicit governance policy override.",
            )
        )

    if recommendation.action not in SUPPORTED_RECOMMENDATIONS:
        matches.append(
            DenyRuleMatch(
                rule_id="POLICY-DENY-UNSUPPORTED-ACTION-001",
                reason=f"Action '{recommendation.action}' is not in the governed action catalog.",
            )
        )

    if not recommendation.target_network_elements:
        matches.append(
            DenyRuleMatch(
                rule_id="POLICY-DENY-EMPTY-TARGETS-002",
                reason="Cannot authorize recommendation with empty target_network_elements.",
            )
        )

    if recommendation.state_changing and unresolved:
        matches.append(
            DenyRuleMatch(
                rule_id="POLICY-DENY-UNRESOLVED-STATE-CHANGE-003",
                reason=(
                    f"State-changing action '{recommendation.action}' is denied when inventory "
                    f"contains unresolved network elements: {unresolved}."
                ),
            )
        )

    if recommendation.action == "NO_AUTOMATED_ACTION" and unresolved:
        matches.append(
            DenyRuleMatch(
                rule_id="POLICY-DENY-INCOMPLETE-INVENTORY-004",
                reason=(
                    f"Automated action is denied because inventory data is incomplete for: "
                    f"{unresolved}."
                ),
            )
        )

    return matches
