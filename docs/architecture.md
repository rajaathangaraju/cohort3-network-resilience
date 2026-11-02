# Architecture

## Multi-Agent & Workflow SSE Pipeline

```text
SSE / API Client
  -> Command Center Backend (POST /api/runs or POST /api/runs/stream)
  -> AwsWorkflowOrchestrator + WorkflowEventStore
  -> AWS AgentCore Agent 1 (cohort3_weather_network_intelligence-aRoVQk71fj) -> NetworkRiskEvent
  -> AWS AgentCore Agent 2 (cohort3_network_resilience-ph5xzN4X1u) -> Agent2Result
      -> Deterministic Service Impact & 0-100 Risk Engine
      -> Bedrock Reasoning / Explanation (with Deterministic Fallback)
      -> Deterministic Preventive Recommendation
      -> Isolated Governance PolicyEngine (ALLOW / DENY / PAUSE)
      -> Explicit Human Approval Workflow
      -> Controlled Mock Action
      -> Synthetic Post-Action Verification
  -> Evidence Bundle & Ordered WorkflowSSEEvent Stream
```

## Workflow SSE Streaming (`command_center/backend/sse.py`)

- **Transport**: HTTP Server-Sent Events (`text/event-stream` with `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`), not WebSockets.
- **Contract**: `WorkflowSSEEvent` (`schema_version`, `event_id`, `event_type`, `sequence`, `run_id`, `trace_id`, `timestamp`, `stage`, `status`, `source`, `message`, `data`, `terminal`, `synthetic`).
- **PAUSE Lifecycle**: Closes with `run_completed` (`workflow_status="AWAITING_APPROVAL"`) rather than holding an HTTP connection open indefinitely. Explicit approval/rejection is submitted via `POST /api/runs/{run_id}/approve` or `POST /api/runs/{run_id}/reject`, and subsequent events can be replayed via `GET /api/runs/{run_id}/stream` with `Last-Event-ID`.
