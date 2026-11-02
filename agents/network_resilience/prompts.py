"""Prompt templates for Agent 2 generative explanation via Amazon Bedrock.

Bedrock is used strictly for operator-facing explanations and rationale summarization.
It never computes or overrides numerical risk scores, governance decisions (ALLOW/DENY/PAUSE),
human approval requirements, or action execution authorization.
"""

import json

from contracts.recommendation import BedrockReasoningContext

AGENT2_SYSTEM_PROMPT = (
    "You are an AI network resilience analyst assisting a telecommunications Network Operations "
    "Center (NOC). You receive deterministically computed risk scores and preventive action "
    "recommendations for synthetic weather events. Your role is ONLY to explain the situation, "
    "the risk factors, and the rationale for the recommended preventive action clearly for NOC "
    "operators. Do NOT invent customer counts, SLA penalties, or financial figures. Do NOT claim "
    "authority to approve or execute network changes. Respond ONLY with a valid JSON object "
    "containing the exact keys: situation_summary, risk_explanation, recommendation_rationale, "
    "and operator_summary."
)


def build_reasoning_user_prompt(context: BedrockReasoningContext) -> str:
    """Build a sanitized, credential-free JSON prompt payload for Bedrock."""
    sanitized_payload = {
        "run_id": context.run_id,
        "trace_id": context.trace_id,
        "weather_event": context.weather_event,
        "weather_severity": context.weather_severity,
        "affected_region": context.affected_region,
        "affected_network_elements": context.affected_network_elements,
        "asset_criticalities": context.asset_criticalities,
        "affected_services": context.affected_services,
        "unresolved_network_elements": context.unresolved_network_elements,
        "risk_score": context.risk_score,
        "overall_risk": context.overall_risk,
        "network_risk": context.network_risk,
        "service_risk": context.service_risk,
        "recommended_action": context.recommended_action,
        "target_network_elements": context.target_network_elements,
        "deterministic_rationale": context.deterministic_rationale,
    }
    return (
        "Generate a concise JSON explanation for the following deterministic network resilience "
        "assessment. Return ONLY raw JSON with keys: situation_summary, risk_explanation, "
        "recommendation_rationale, operator_summary.\n\n"
        f"{json.dumps(sanitized_payload, indent=2)}"
    )
