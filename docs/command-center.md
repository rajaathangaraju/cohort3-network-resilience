# Command Center Integration

## Architecture Flow

```text
React / Web Command Center (command_center/frontend/)
  ->
Command Center Backend API (command_center/backend/service.py)
  ->
Local Orchestrator (COMMAND_CENTER_DATA_MODE=mock)
  OR
AWS AgentCore Invocation Service (COMMAND_CENTER_DATA_MODE=aws_live)
  ->
AgentCore Agent 1 (cohort3_weather_network_intelligence-aRoVQk71fj)
  ->
AgentCore Agent 2 (cohort3_network_resilience)
  ->
Governance / Approval / Controlled Mock Action / Synthetic Verification
  ->
CommandCenterView Contract -> CommandCenterDashboardModel
```

## HTTP API Endpoints

- `POST /api/runs`: Accepts weather event payload (or `{"scenario": "deny" | "allow" | "pause"}`), invokes Agent 1 then Agent 2, preserves `run_id` and `trace_id`, and returns `CommandCenterView` without auto-approving state-changing actions.
- `GET /api/runs/{run_id}`: Returns the current `CommandCenterView` for `run_id`.
- `POST /api/runs/{run_id}/approve`: Records explicit `APPROVED` decision (`approver_reference`), rejects with `409 Conflict` if governance is `DENY`, or executes the controlled mock action and synthetic verification when governance is `PAUSE`.
- `POST /api/runs/{run_id}/reject`: Records explicit `REJECTED` decision, keeps execution `BLOCKED`, and records rejection evidence.
- `GET /api/runs/{run_id}/evidence?trace_id=...`: Returns ordered `EvidenceRecord` items strictly matching `run_id` and `trace_id`.
