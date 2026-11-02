# Known Gaps and Limitations

This register reflects the reviewed local source. The team reports an end-to-end AgentCore deployment; that does not establish that every data source, model call, policy control or verification step is live. Current AWS state and submitted run results were not rechecked during this documentation-only update.

No failures, scores, timestamps or request IDs below have been invented. Record actual run evidence against each relevant gap before submission.

## Data, Agent 1 and contracts

| Gap | What it limits | Current handling / closure evidence |
| --- | --- | --- |
| Synthetic inventory | Claims about real equipment, customers or service impact | Both primary adapters use fixture catalogs; `RealInventoryAPIAdapter` is unimplemented. Keep synthetic labels and document any future public/synthetic replacement source. |
| Separate forecast path | Claim that the deployed Agent 1 fetches live weather | Agent 1's runtime invokes `WeatherNetworkIntelligenceAgent`, which validates supplied alerts and uses region lookup. Open-Meteo retrieval exists in the standalone prototype. Integration needs a compatible contract and end-to-end request/response evidence. |
| Limited geographic correlation | Claim of accurate storm-to-device geometry | Primary mapping uses region names; default bounds are synthetic. The prototype requests a forecast at each coordinate but does not infer a storm polygon. Validate a future geometry implementation against frozen boundary cases. |
| Different inventory definitions | Consistent cross-agent device interpretation | Prototype and primary catalogs differ for NE-102/NE-103. Primary Region-A membership also differs from Agent 2's catalog region, and NE-302/303/304 lack enrichment records. Resolve or explicitly test those cases before combining data. |
| Draft forecast handoff | Direct prototype-to-Agent-2 delivery | `agent1-handoff-0.1` is separate from shared `NetworkRiskEvent`. Do not bypass validation or manufacture severe alerts to fit the contract. |
| Demo weather thresholds | Claims of operationally validated exposure | Prototype HIGH/MEDIUM thresholds are unvalidated. Failed/invalid forecasts become UNKNOWN, but explicit forecast-expiry checks, first-threshold-crossing timing and duplicate-input handling are not established by this prototype. |
| No official warning feed | Official alert provenance | Open-Meteo values are model forecasts, not official severe-weather bulletins. Supplied scenario severity is not independent meteorological evidence. |

## Analysis, models and governed action

| Gap | What it limits | Current handling / closure evidence |
| --- | --- | --- |
| Rule-based impact and risk | Claims of measured outage reduction or calibrated probabilities | The additive 0-100 risk score and service analysis operate on synthetic metadata. They do not estimate measured downtime, customers or SLA/financial impact. Recommendation confidence constants are not calibration results. |
| Optional Bedrock with fallback | Claim that every result used AI generation | The adapter records disabled/error/fallback outcomes. Report per-run model ID and `fallback_used`; successful deterministic output does not prove a model call succeeded. |
| Incomplete model provenance | Reproducing and independently grading model outputs | Prompt templates are versioned, but complete rendered prompts, raw model outputs and SHA-256 output records are not automatically archived by the reviewed adapter. Add and verify those artifacts before reporting model-evaluation results. |
| Python policy implementation | Claim of complete event-required governance | ALLOW/DENY/PAUSE rules are implemented in Python. Cedar-backed enforcement and Bedrock Guardrails covering all required model/tool/agent calls are not established by the reviewed implementation. Supply actual policy configuration and execution records if enforced outside this checkout. |
| Approval PAUSE versus withdrawal PAUSE | Model-withdrawal demonstration | Current PAUSE represents waiting for human approval. Separately demonstrate that withdrawing a model causes calls to be refused, with policy/audit evidence. |
| Approver authentication | Production authorisation assurance | Explicit approve/reject state transitions exist, but a caller-supplied approver reference is not verified identity. Authentication, role-based access and the deployment access layer need independent verification. |
| Synthetic actions and verification | Claim of real network remediation | `MockRemediationAdapter` and `SyntheticVerificationService` do not change or inspect real devices. A synthetic PASSED result is workflow evidence, not measured network recovery. |

## Operations, UI and evidence

- **UI is implemented:** Streamlit and a web frontend exist. The earlier claim that the frontend is entirely deferred is obsolete. UI status must still be backed by agent/API evidence; no fresh UI or deployment acceptance run was performed for this update.
- **In-memory state:** backend run dictionaries and SSE event storage are not a durable database. Pending approvals and replay history can be lost on restart. SSE reconnect replay is implemented, but it is not a general durable retry/idempotency guarantee for new workflow requests.
- **External access:** backend routes and permissive CORS do not constitute an authentication boundary. Confirm the actual ingress/access configuration before claiming production readiness.
- **Evidence collection:** AWS log delivery can be delayed or empty. Inspect counts, time windows, agent IDs and run/trace correlation; a bundle file or inferred stage record alone does not prove the underlying call occurred. The collector also relies on cached agent responses.
- **Prototype evidence remains separate:** weather request URLs, raw forecasts and local prototype audit records are not automatically included in the deployed two-agent evidence bundle. An AWS log containing a weather event is not by itself proof of a weather-provider API call.
- **Dependency reproducibility:** Python dependencies use lower bounds rather than a complete lockfile. The README explicitly installs Starlette/Uvicorn for the backend; these imports are not directly listed in `pyproject.toml`. Preserve the submitted environment and artifact versions.
- **Validation scope:** root lint/type-check settings exclude the standalone prototype; its offline unittest suite runs separately. No updated full-suite count, coverage percentage, live latency or cloud-validation result is claimed here.

## Submission evidence still to complete

`scripts/run_declared_scenario.sh` automates pre-run declaration generation (`declared/<RUN_ID>.json`), S3 upload and `head-object` verification (`s3://cohort3-agentcore-835156611439/declared/<RUN_ID>.json`), real AWS Agent 1 -> Agent 2 invocation with explicit `run_id`/`trace_id`, CloudWatch correlation checks, and S3 evidence mirroring (`s3://cohort3-agentcore-835156611439/evidence/<RUN_ID>/`).

At documentation review, this checkout does not contain a completed `judging.json` or a verified offline reported-score rebuild command. Existing run/evidence scripts are useful but do not alone meet the entire submission requirement.

The submission owner should:

1. Record the primary track, actual team code, final SVN revision, evidence bucket/prefix and up to 20 scored run IDs. Replace `TEAM_PENDING` in submitted configuration/results as appropriate; do not rewrite historical records misleadingly.
2. Freeze evaluation data and hashes, choose an independent checker, and place declarations in the bucket before new scored runs. Existing development tests cannot retrospectively become predeclared evaluation.
3. Archive actual inputs/outputs, prompt/model provenance where used, timestamps, CloudWatch records and per-item pass/fail results.
4. Provide an offline rebuild command that reads frozen evidence, checks integrity and reproduces reported results without a fresh weather/model call. Supply the separate read-only judge inspection command.
5. Complete pre-November-1 provenance and record the demo with the code revision and run ID visible. Confirm judge access through the approved repository/evidence route.

## Per-run gap / blocker record

For a concrete failure or platform restriction, append a record with:

| Field | Required content |
| --- | --- |
| Gap and affected unit | Which capability/result is limited |
| Run ID / trace ID | Actual identifiers, or explicitly not available if execution never began |
| Observed versus expected | Measured result, count or failed assertion; no invented numbers |
| Time / service / region | Actual UTC timestamp and platform context |
| Error / request ID | Exact sanitised error and provider request ID when available |
| Evidence location | Repository path or approved bucket object |
| Escalation / disposition | Who was notified, when, and unresolved/fixed/retested status |

The previously reported `iam:GetPolicy` AccessDenied was an inability to inspect the boundary policy, not proof that the boundary was absent or deployment was impossible. Do not list it as a current deployment blocker without a new relevant failure. Preserve its original error details if it is included in historical evidence.

See [DATA.md](DATA.md) for provenance and [README.md](README.md) for supported commands. This file is a limitation register, not a claim that missing capabilities were implemented or that fresh checks passed.
