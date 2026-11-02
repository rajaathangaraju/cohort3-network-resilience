# Primary track: Track 3 / Cohort 3 - Network Resilience Command Center

Weather events can expose network equipment and the services that depend on it.
This project connects two agents to identify exposed elements, assess service risk, and recommend preventive action.
The workflow records governance, explicit approval, simulated execution, and evidence under shared run and trace IDs.

## Architecture and implementation status

```text
Command Center (Streamlit or web frontend)
  -> Backend HTTP API / workflow SSE
  -> Agent 1: weather input validation + region-to-inventory mapping
  -> NetworkRiskEvent
  -> Agent 2: inventory enrichment + service impact + risk + recommendation
  -> Policy decision: ALLOW / DENY / PAUSE
  -> Explicit approval where required
  -> Controlled mock action + synthetic verification
  -> CommandCenterView + application / CloudWatch evidence
```

The team reports both agents deployed on Amazon Bedrock AgentCore in `us-east-2`.
Runtime identifiers recorded in the deployment configuration are:

| Component | Runtime ID |
| --- | --- |
| Agent 1 | `cohort3_weather_network_intelligence-aRoVQk71fj` |
| Agent 2 | `cohort3_network_resilience-ph5xzN4X1u` |

These identifiers describe the configured deployment; current availability and runtime versions must be checked in AWS. See [deployment details](docs/deployment.md).

| Component | What the current source implements |
| --- | --- |
| Agent 1 | Validates supplied weather alerts, looks up network IDs by named region using a synthetic inventory adapter, and builds the shared `NetworkRiskEvent`. |
| Agent 2 | Enriches equipment, calculates deterministic service impact and a 0-100 risk score, produces recommendations, and processes governance, approval, mock actions and synthetic verification. |
| Bedrock explanation | Optional operator-facing explanation using the configured model; deterministic fallback is explicitly recorded when disabled or unsuccessful. It does not determine authoritative risk scores or approval decisions. |
| Backend | Local or AWS-runtime orchestration, JSON APIs, workflow SSE, approval/rejection commands, and evidence aggregation. |
| Dashboard | Streamlit operator interface with workflow progress, decisions, approval controls and evidence; a web frontend is also present under `command_center/frontend/`. |
| Separate weather prototype | `forecast_prototype.py` fetches per-location Open-Meteo forecasts or runs an offline scenario. Its draft output is separate from the deployed Agent 1 entry point and shared contract. |

**AWS-live execution does not mean live telecom data or real remediation.** The primary workflow uses synthetic inventory and simulated actions. Its Agent 1 performs region lookup, not polygon-based device discovery. A supplied/default bounding box does not prove geometric matching. The separate forecast prototype uses equipment coordinates and demo weather thresholds; those thresholds are not production-approved risk limits.

## Repository layout

| Location | Purpose |
| --- | --- |
| `agents/weather_network_intelligence/` | Agent 1 logic, CLI, AgentCore entry point and separate forecast prototype |
| `agents/network_resilience/` | Agent 2 analysis, recommendations, action orchestration and runtime entry point |
| `contracts/` | Shared typed input/output, governance, result and SSE contracts |
| `governance/` | Policy decisions, approval and audit logic |
| `integrations/` | AgentCore, Bedrock, inventory, weather, remediation and CloudWatch adapters |
| `orchestration/` | Local end-to-end scenarios and run state |
| `command_center/` | Backend service, SSE handling and web frontend |
| `streamlit_ui/` | Streamlit Command Center |
| `tests/`, `samples/`, `scripts/`, `docs/` | Tests, fixtures, packaging/deployment tools and detailed documentation |

## Local setup

Use Python **3.13 or newer**, from the repository root. These instructions use PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,dashboard]" starlette uvicorn
```

Activate the environment if your organisation permits it, or use the explicit `.venv` Python path in place of `python` below. On Linux/EC2, use `python3.13 -m venv .venv`, then `source .venv/bin/activate` and the same installation command with `python`.

`starlette` and `uvicorn` are included explicitly because the backend imports them but they are not directly declared in the project's dependency list. Consult `.env.example` and `common/config.py`; do not overwrite an existing `.env` or commit credentials. AWS clients use the standard boto3 credential chain.

For an offline application demonstration, explicitly disable model calls:

```powershell
$env:BEDROCK_ENABLED = "false"
$env:COMMAND_CENTER_DATA_MODE = "mock"
```

On Bash, use `export BEDROCK_ENABLED=false` and `export COMMAND_CENTER_DATA_MODE=mock`.

## Run each stage

Run these commands from the repository root with the environment above:

```powershell
# Agent 1: supplied synthetic weather alert -> shared contract
python -m agents.weather_network_intelligence.main

# Agent 2: sample shared event -> analysis, governance and result
python -m agents.network_resilience.main --input samples/network_risk_event.json

# Both agents locally: normal PAUSE leaves approval pending
python -m orchestration.workflow --scenario pause

# Additional synthetic scenarios (approval flags simulate an explicit decision)
python -m orchestration.workflow --scenario allow
python -m orchestration.workflow --scenario deny
python -m orchestration.workflow --scenario pause --approve
python -m orchestration.workflow --scenario pause --reject

# Backend; keep this terminal open
python -m command_center.backend.service --host 127.0.0.1 --port 8000 --data-mode mock
```

In another terminal using the same Python environment:

```powershell
python -m streamlit run streamlit_ui/dashboard.py
```

The backend exposes `http://127.0.0.1:8000/health` and `/docs`; Streamlit prints its own URL, normally `http://localhost:8501`. The frontend reads authoritative backend results rather than calculating risk or fabricating fallback outcomes. See [UI instructions](streamlit_ui/README.md).

### Optional standalone weather prototype

```powershell
python agents/weather_network_intelligence/forecast_prototype.py --mode demo
python agents/weather_network_intelligence/forecast_prototype.py --mode live --team-code YOUR_TEAM_CODE
```

Live mode calls Open-Meteo and requires permitted internet access; its free endpoint terms and attribution requirements apply. Inventory remains synthetic unless explicitly supplied. Outputs go to `runs/<run_id>/`, with raw weather evidence, local audit and a draft handoff. It does not invoke Agent 2. See [prototype documentation](agents/weather_network_intelligence/README_PROTOTYPE.md).

## AWS runtime mode and deployment

From an approved AWS-authenticated Linux/EC2 environment:

```bash
export AWS_REGION=us-east-2
export COMMAND_CENTER_DATA_MODE=aws_live
python -m command_center.backend.service --host 127.0.0.1 --port 8000 --data-mode aws_live
```

`AGENT1_RUNTIME_ARN` and `AGENT2_RUNTIME_ARN` can override configured runtime targets. Agent 2 discovery is available when its ARN is omitted. `BEDROCK_ENABLED` defaults to false locally; enabling it on the backend does not update a remote agent's environment. The Agent 2 deployment script configures remote Bedrock settings. Inspect each result's `ai_reasoning` model ID, status and fallback flag to establish whether a model was actually invoked.

The configured explanation model is `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Model availability and permissions are account-dependent.

For an intentional deployment update, the repository provides:

```bash
bash scripts/package_agent1.sh
bash scripts/deploy_agent1.sh
bash scripts/package_agent2.sh
bash scripts/deploy_agent2.sh
```

Packaging targets Python 3.13 / Linux ARM64. Deployment scripts upload artifacts and change AWS runtime configuration; review their account, bucket, role and runtime settings first. They are not read-only validation commands. Keep public exposure and operator access behind the team's approved access controls.

## API, streaming and approval semantics

| Endpoint | Purpose |
| --- | --- |
| `POST /api/runs` | Start an Agent 1 -> Agent 2 workflow |
| `POST /api/runs/stream` | Start a workflow with HTTP SSE progress |
| `GET /api/runs/{run_id}` | Fetch the stored run view |
| `GET /api/runs/{run_id}/stream` | Replay workflow events, supporting `Last-Event-ID` |
| `POST /api/runs/{run_id}/approve` | Explicitly approve a pending PAUSE workflow |
| `POST /api/runs/{run_id}/reject` | Explicitly reject a pending PAUSE workflow |
| `GET /api/runs/{run_id}/evidence` | Fetch correlated evidence |

SSE communicates workflow progress, not model tokens or action authorisation. Agents return complete typed JSON. Replaying stored SSE events does not rerun the agents. A pending approval closes the initial stream with `AWAITING_APPROVAL`; a later approve/reject request advances the workflow. DENY cannot be overridden through the approval endpoint.

Run state and SSE storage are in memory: replay and pending approvals are not durable across process restarts. The approval interface records a decision; it does not by itself prove authenticated approver identity. All remediation and post-action verification remain synthetic.

## Validation and evidence

Local checks (results must be recorded from the revision actually submitted):

```powershell
python -m pytest -v
python -m ruff check .
python -m mypy .
python -m unittest discover -s agents/weather_network_intelligence/tests -p "test_forecast_prototype.py" -v
```

The standalone prototype is excluded from root Ruff/mypy checks and has its own offline unittest suite. No test-count or live-validation result is asserted by this README.

AWS checks invoke deployed runtimes and may incur usage charges:

```bash
bash scripts/test_agent1_remote.sh
bash scripts/test_agent2_remote.sh
bash scripts/test_agent1_to_agent2_aws.sh
bash scripts/test_workflow_sse_aws.sh
```

The SSE script starts a backend and tests five scenario paths, including approval and rejection. It saves captured streams under `evidence/<run_id>/workflow-stream.sse`.

After a run, collect correlated CloudWatch evidence using the actual IDs:

```bash
bash scripts/collect_run_evidence.sh --run-id YOUR_RUN_ID --trace-id YOUR_TRACE_ID
```

The collector uses locally cached runtime responses as well as CloudWatch; keep the run's response cache. The evidence bundle can contain:

```text
evidence/<run_id>/
  agent1-response.json
  agent2-response.json
  cloudwatch-events.json
  workflow-summary.json
  evidence-index.json
  runtime-metadata.json
  workflow-stream.sse       # when the SSE validation script was used
```

Check actual log counts, timestamps, correlation IDs and Bedrock fallback status. A generated evidence filename or a completed UI stage is not proof of a successful external API or model call. Weather API evidence from the separate prototype is under its `runs/` directory, not automatically included in the primary workflow's CloudWatch bundle.

## Submission and known limits

### Declared Judging Submission Workflow (`scripts/run_declared_scenario.sh`)

To automate the complete pre-declared judging evidence pipeline against S3 bucket `s3://cohort3-agentcore-835156611439` in `us-east-2` (AWS Account `835156611439`), use `scripts/run_declared_scenario.sh`.

The script enforces the strict fail-closed sequence:

```text
DECLARE -> VERIFY IN S3 -> RUN -> CAPTURE -> MIRROR -> VERIFY
```

1. **DECLARE**: Generates a fresh `RUN_ID` (`WX-<SCENARIO>-<8HEX>`) and `TRACE_ID` (`TRACE-<SCENARIO>-<8HEX>`) and writes `declared/<RUN_ID>.json` (input parameters only; no expected outcomes).
2. **VERIFY IN S3**: Uploads to `s3://cohort3-agentcore-835156611439/declared/<RUN_ID>.json` and verifies object existence via `aws s3api head-object` **before** Agent 1 is invoked.
3. **RUN**: Executes the real AWS Agent 1 -> Agent 2 handoff (`scripts/test_agent1_to_agent2_aws.sh --run-id "$RUN_ID" --trace-id "$TRACE_ID"`) and validates exact correlation and scenario governance outcomes.
4. **CAPTURE**: Collects the correlated evidence bundle (`scripts/collect_run_evidence.sh`), verifies local JSON files and correlated CloudWatch events (`> 0` total, `>= 1` Agent 1, `>= 1` Agent 2), copies `declared.json`, and writes `submission-summary.json`.
5. **MIRROR**: Uploads `evidence/<RUN_ID>/` to `s3://cohort3-agentcore-835156611439/evidence/<RUN_ID>/` using `aws s3 sync`.
6. **VERIFY**: Confirms every required evidence file exists in S3 via `aws s3api head-object`.

Single scenario commands:

```bash
./scripts/run_declared_scenario.sh --scenario deny
./scripts/run_declared_scenario.sh --scenario allow
./scripts/run_declared_scenario.sh --scenario pause
./scripts/run_declared_scenario.sh --scenario pause --approve
./scripts/run_declared_scenario.sh --scenario pause --reject
```

Final five-run submission command (executes `deny`, `allow`, `pause`, `pause --approve`, and `pause --reject` sequentially with unique IDs and uploads `s3://cohort3-agentcore-835156611439/evidence/submission-runs.json`):

```bash
./scripts/run_declared_scenario.sh --all-scenarios
```

For judging, preserve the exact SVN revision, run/trace IDs, runtime metadata and recorded demo. The submission owner should complete `judging.json`, list up to 20 scored runs, specify approved evidence locations, and mirror required evidence to the submission bucket. `judging.json` is not present in this checkout at the time of this README update.

Declare targets and frozen-test hashes in `evidence/declared/<run_id>.json` in the bucket **before** scored runs. Do not backdate declarations. The existing collector retrieves evidence; it is not an offline score-rebuild tool. A submission-ready **offline rebuild command and read-only judging command are still to be supplied and verified** against saved records; this README does not invent commands that are absent from the repository.

**Before Nov 1:** the team must document any pre-existing code, models, templates or external assets and distinguish them from event work. That provenance has not been established by this documentation review.

Current limitations to carry into the submission:

- Synthetic network inventory, service metadata, remediation and verification; demo risk rules are not validated telecom operating limits.
- The primary Agent 1 path consumes supplied alerts and performs named-region lookup. Standalone live forecast code is not evidence of live-weather ingestion in that deployed path.
- Bedrock may be disabled or fall back; report the recorded outcome for each run.
- Python governance logic is present. Cedar enforcement, Bedrock Guardrails on all required calls, and the model-withdrawal PAUSE demonstration are not established by the reviewed code. Approval-related PAUSE is a different demonstration.
- In-memory workflow state and operator access controls require further work for production use.
- [DATA.md](DATA.md) and [GAPS.md](GAPS.md) need reconciliation before submission: their blanket synthetic-data statement does not describe the standalone live-forecast mode, and the frontend-deferred statement does not describe the existing Streamlit dashboard. They have not been changed as part of this README-only update.

See [architecture](docs/architecture.md), [contracts](docs/agent-contract.md), [governance](docs/governance.md), [observability](docs/observability.md), and [Command Center integration](docs/command-center.md) for component details. Source entry points and recorded evidence determine actual behaviour when older documents differ.
