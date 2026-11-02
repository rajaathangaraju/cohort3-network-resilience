# Observability, Traceability, SSE Logging, and Audit Evidence

## Correlation Propagation

Every log event, contract, SSE frame (`WorkflowSSEEvent`), and evidence record carries:
- `run_id`
- `trace_id`
- `schema_version`
- `team_code`

## Workflow SSE Structured Logging

Every business `WorkflowSSEEvent` emitted by `WorkflowSSEBuilder` writes a structured JSON log containing:
- `run_id`
- `trace_id`
- `event_id`
- `event_type`
- `sequence`
- `stage`
- `status`

Transport-only heartbeats (`: heartbeat`) do not increment sequence numbers and do not create application evidence records.

## Secret Redaction

`common/logging.py`, `integrations/agentcore.py`, `integrations/bedrock.py`, and `command_center/backend/sse.py` scrub any credential, session token, authorization header, or key pattern before emitting logs, SSE `run_failed` events, or audit summaries.
