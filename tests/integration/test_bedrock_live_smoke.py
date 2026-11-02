"""Opt-in live Amazon Bedrock smoke test.

This test is skipped by default during ordinary `pytest` runs so unit/integration tests
never require live AWS credentials.

To run this live smoke test against Amazon Bedrock (`us-east-2`):
    $env:RUN_LIVE_BEDROCK_SMOKE="1"
    $env:BEDROCK_ENABLED="true"
    $env:BEDROCK_MODEL_ID="us.anthropic.claude-3-5-haiku-20241022-v1:0"
    python -m pytest tests/integration/test_bedrock_live_smoke.py -v
"""

import os

import pytest

from agents.network_resilience.agent import NetworkResilienceAgent
from common.config import Settings
from contracts.action_result import ExecutionStatus
from contracts.governance_decision import ApprovalStatus, GovernanceDecision
from integrations.bedrock import BedrockRuntimeAdapter, ResilientBedrockReasoningService
from tests.integration.test_agent1_agent2 import EXACT_AGENT1_INTEGRATION_PAYLOAD

RUN_LIVE = os.getenv("RUN_LIVE_BEDROCK_SMOKE") == "1"


@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live Bedrock smoke test is opt-in; set RUN_LIVE_BEDROCK_SMOKE=1 to execute.",
)
def test_live_bedrock_reasoning_smoke() -> None:
    settings = Settings(
        bedrock_enabled=True,
        bedrock_model_id=os.getenv(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
        ),
    )
    adapter = BedrockRuntimeAdapter(settings=settings)
    service = ResilientBedrockReasoningService(
        settings=settings,
        primary_adapter=adapter,
    )
    agent2 = NetworkResilienceAgent(bedrock_reasoning_service=service)
    result = agent2.process(EXACT_AGENT1_INTEGRATION_PAYLOAD)

    assert result.ai_reasoning is not None
    if result.ai_reasoning.fallback_used:
        pytest.fail(
            "Live Bedrock call fell back to deterministic reasoning. "
            f"Verify AWS credentials / model access in {settings.aws_region}. "
            f"Details: {result.ai_reasoning.error_reason}"
        )

    assert result.ai_reasoning.result_status == "SUCCEEDED"
    assert len(result.ai_reasoning.reasoning.situation_summary) > 0
    # Governance and execution remain strictly deterministic and blocked pending human approval
    assert result.governance.decision == GovernanceDecision.PAUSE
    assert result.approval.status == ApprovalStatus.PENDING
    assert result.execution.status == ExecutionStatus.BLOCKED
