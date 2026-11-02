# Deployment & AgentCore Operations Guide

## 1. Deployed AgentCore Runtimes (`us-east-2`)

- **AWS Account**: `835156611439`
- **Region**: `us-east-2`
- **Runtime Role ARN**: `arn:aws:iam::835156611439:role/bb26/cohort3-agent-runtime`
- **Agent 1 Runtime ARN**: `arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_weather_network_intelligence-aRoVQk71fj` (`READY`)
- **Agent 2 Runtime ARN**: `arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_network_resilience-ph5xzN4X1u` (`READY`)

## 2. Backend HTTP & SSE Endpoints

- `POST /api/runs`: Synchronous JSON orchestration run
- `POST /api/runs/stream`: Workflow-level HTTP SSE stream (`text/event-stream`)
- `GET /api/runs/{run_id}`: Enriched JSON run state
- `GET /api/runs/{run_id}/stream`: SSE event replay supporting `Last-Event-ID` without rerunning Agent 1 or Agent 2
- `POST /api/runs/{run_id}/approve`: Explicit human approval (only when `governance == PAUSE` and `approval == PENDING`)
- `POST /api/runs/{run_id}/reject`: Explicit human rejection (only when `governance == PAUSE` and `approval == PENDING`)
- `GET /api/runs/{run_id}/evidence`: Full sanitized evidence bundle

## 3. Live AWS SSE Validation Script

```bash
./scripts/test_workflow_sse_aws.sh
```

Starts the backend in `COMMAND_CENTER_DATA_MODE=aws_live`, streams all 5 governance scenarios via `curl -N`, verifies event ordering and `run_id`/`trace_id` preservation, and saves each captured stream to `evidence/<run_id>/workflow-stream.sse`.
