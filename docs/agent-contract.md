# Agent Contracts

All inter-agent and dashboard contracts are defined in `contracts/` using Pydantic v2 with `extra="forbid"`.

## Correlation & Metadata Propagation

Every workflow preserves:
- `schema_version` (e.g., `"0.1"`)
- `team_code` (e.g., `"TEAM_PENDING"`)
- `run_id` (e.g., `"WX-46329ae2b74b"`)
- `trace_id` (e.g., `"7a4106b675e4419f87b9b84d30a67ca4"`)
- `data_source` (`"synthetic"`, `"mock"`, or `"external"`)

## Agent 1 -> Agent 2 Contract (`NetworkRiskEvent`)

Validated by `validate_agent2_input` in `contracts/network_risk_event.py`:
- `created_at`: timezone-aware ISO-8601 datetime
- `weather_event`: non-empty string
- `severity`: `LOW` | `MEDIUM` | `HIGH` | `CRITICAL`
- `affected_region`: non-empty string
- `affected_area`: `AffectedArea(south, north, west, east)` where `south < north` and `west < east`
- `affected_network_elements`: non-empty list of non-blank element IDs (deduplicated preserving order)

## Agent 2 Output Contract (`Agent2Result`)

Defined in `contracts/action_result.py`:
- `input_context`: preserved Agent 1 weather, region, bounds, and element IDs
- `affected_assets`: enriched `AffectedAssetDetail` list
- `unresolved_network_elements`: any element IDs not found in `NetworkInventoryPort`
- `service_impact`: `ServiceImpact`
- `risk_assessment`: `RiskAssessmentSummary` (`risk_score`, `overall_risk`, `weather_risk`, `network_risk`, `service_risk`, `risk_reasons`)
- `recommendation`: `PreventiveRecommendation`
- `governance`: `GovernanceOutcomeSummary` (`ALLOW` | `DENY` | `PAUSE`, `reason`, `matched_policy_rules`)
- `approval`: `ApprovalOutcomeSummary` (`NOT_REQUIRED` | `PENDING` | `APPROVED` | `REJECTED` | `EXPIRED`)
- `execution`: `ExecutionOutcomeSummary` (`PENDING` | `EXECUTED` | `BLOCKED` | `FAILED`, `synthetic=true`)
- `verification`: `VerificationOutcomeSummary` (`NOT_STARTED` | `PENDING` | `PASSED` | `FAILED` | `BLOCKED`, `synthetic=true`)
- `evidence`: 10-stage ordered `EvidenceRecord` list
- `command_center`: pre-computed `CommandCenterView`
