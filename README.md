# Cohort 3 Network Resilience

Multi-agent network resilience solution deployed on Amazon Bedrock AgentCore Runtime (`us-east-2`), featuring two independent AI agents connected by AWS-live / local orchestration, workflow-level HTTP Server-Sent Events (SSE) progress streaming, Amazon Bedrock generative explanation (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) with deterministic fallback, and CloudWatch evidence collection:

1. **Agent 1 (`agents/weather_network_intelligence/`)**: Deployed on AgentCore Runtime (`cohort3_weather_network_intelligence-aRoVQk71fj`, `PYTHON_3_13`, ARM64). Ingests weather alerts, correlates affected geography, queries network inventory, and returns a complete `NetworkRiskEvent` JSON contract.
2. **Agent 2 (`agents/network_resilience/`)**: Deployed on AgentCore Runtime (`cohort3_network_resilience-ph5xzN4X1u`, `PYTHON_3_13`, ARM64). Consumes `NetworkRiskEvent`, enriches affected network elements, computes deterministic service impact and a 0-100 risk score, invokes Amazon Bedrock for operator explanations, evaluates isolated governance policies (`ALLOW` / `DENY` / `PAUSE`), manages explicit human approval, executes controlled mock actions, runs synthetic post-action verification, and returns a complete `Agent2Result` JSON contract.
3. **Command Center Backend & Workflow SSE Orchestrator (`command_center/backend/`)**: Supports both `COMMAND_CENTER_DATA_MODE=mock` and `COMMAND_CENTER_DATA_MODE=aws_live`, exposing synchronous JSON endpoints (`POST /api/runs`, `GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/approve`, `POST /api/runs/{run_id}/reject`, `GET /api/runs/{run_id}/evidence`) and workflow-level HTTP SSE streaming (`POST /api/runs/stream` and `GET /api/runs/{run_id}/stream` with `Last-Event-ID` replay).

## Key SSE & Governance Design Principles

- **Workflow-Progress Streaming (Not Token Streaming)**: Agent 1 and Agent 2 continue returning complete typed JSON contracts (`NetworkRiskEvent` and `Agent2Result`). The backend translates workflow progress into typed `WorkflowSSEEvent` frames over `text/event-stream` (HTTP SSE, not WebSocket).
- **SSE Does Not Authorize Actions or Replace Evidence**: Authoritative risk scores, `ALLOW`/`DENY`/`PAUSE` governance decisions, approval states, and audit evidence remain strictly deterministic in Agent 2 and `governance/`.
- **Production-Safe PAUSE Behavior**: In normal mode, a `PAUSE` stream emits `approval_pending`, `action_blocked`, and closes cleanly with `run_completed` (`workflow_status="AWAITING_APPROVAL"`). Approval or rejection uses explicit commands (`POST /api/runs/{run_id}/approve` or `POST /api/runs/{run_id}/reject`).
- **Deterministic Reconnection (`Last-Event-ID`)**: Reconnecting with `Last-Event-ID` replays stored events from `WorkflowEventStore` and never reruns Agent 1 or Agent 2.

## Live AWS SSE Validation (EC2)

```bash
cd ~/cohort3-network-resilience
source .venv/bin/activate

python -m ruff check .
python -m mypy .
python -m pytest -v

./scripts/test_workflow_sse_aws.sh
```
