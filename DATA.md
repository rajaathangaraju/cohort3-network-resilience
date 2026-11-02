# Data Sources and Provenance

This document describes data handling in the reviewed repository, not an independent verification of deployed runtime versions or individual run results. The primary two-agent workflow uses synthetic weather scenarios and network catalogs. The separate forecast prototype can retrieve real public weather forecasts. AWS execution records and model-generated explanations are distinct from those synthetic inputs.

Only public or synthetic data is intended for the hackathon. Do not add customer records, operator logs, credentials, session tokens or private production inventories. A `synthetic` flag describes the payload/scenario; it does not mean an AWS invocation or CloudWatch timestamp is simulated.

## Source register

| Data | Origin / generator | Usage and limitations |
| --- | --- | --- |
| Workflow weather scenarios | [orchestration/workflow.py](orchestration/workflow.py), [samples/](samples/), and Agent 1 CLI defaults | Team-defined synthetic weather events, severities and regions; not official alerts or observed weather. |
| Agent 1 inventory | `MockInventoryAdapter.DEFAULT_DATA` in [integrations/inventory_api.py](integrations/inventory_api.py) | Synthetic region-to-ID lookup. It does not query physical devices or a live inventory API. |
| Agent 2 enrichment | `MockNetworkInventoryAdapter.DEFAULT_CATALOG` in [integrations/inventory_api.py](integrations/inventory_api.py) | Synthetic equipment types, criticality, service categories, redundancy and operational state. |
| Prototype inventory and adverse weather | `INVENTORY` and `DEMO_PEAKS` in [forecast_prototype.py](agents/weather_network_intelligence/forecast_prototype.py) | Separate synthetic Bengaluru-area fixtures, reproducible without internet access. |
| Prototype live weather | [Open-Meteo Forecast API](https://open-meteo.com/en/docs), `https://api.open-meteo.com/v1/forecast` | Public model forecasts requested at equipment coordinates; not official storm warnings, observed equipment health or a verified affected-area polygon. |
| Risk and service impact | [risk_analyzer.py](agents/network_resilience/risk_analyzer.py), [service_impact.py](agents/network_resilience/service_impact.py) | Deterministic results derived from scenario inputs and synthetic inventory; not measured outage, customer or financial impact. |
| Recommendations | [recommendation_engine.py](agents/network_resilience/recommendation_engine.py) | Rule-generated preventive actions and configured confidence values. Confidence is not an empirically calibrated probability of success. |
| Operator explanation | [prompts.py](agents/network_resilience/prompts.py), [integrations/bedrock.py](integrations/bedrock.py) | Bedrock-generated text when enabled/successful; otherwise explicitly recorded deterministic fallback. |
| Remediation and verification | [integrations/remediation_api.py](integrations/remediation_api.py), [action_service.py](agents/network_resilience/action_service.py) | Simulated actions and synthetic verification; no proof of physical network recovery. |
| Runtime evidence | [integrations/cloudwatch.py](integrations/cloudwatch.py), backend and runtime logs | Execution metadata and correlated records; their presence and counts must be checked per run. |

## Primary workflow inventory

Agent 1's synthetic region membership is:

| Region | Element IDs |
| --- | --- |
| `Region-A` | NE-101, NE-102, NE-103 |
| `Demo-Region-A` | NE-101, NE-102 |
| `Region-B` | NE-201, NE-202 |
| `Region-C` | NE-301, NE-302, NE-303, NE-304 |

Agent 2's default enrichment catalog is:

| ID | Type | Catalog region | Criticality | Services | Redundancy |
| --- | --- | --- | --- | --- | --- |
| NE-101 | CELL_SITE | Demo-Region-A | CRITICAL | MOBILE_DATA, VOICE | Yes |
| NE-102 | ROUTER | Demo-Region-A | HIGH | IP_TRANSIT, ENTERPRISE_CONNECTIVITY | Yes |
| NE-103 | FIBER_AGGREGATION | Demo-Region-A | MEDIUM | BACKHAUL | Yes |
| NE-201 | EDGE_SWITCH | Region-B | LOW | INTERNAL_TELEMETRY | Yes |
| NE-202 | MICROWAVE_LINK | Region-B | MEDIUM | REGIONAL_VOICE | Yes |
| NE-301 | CORE_ROUTER | Region-C | HIGH | ENTERPRISE_VPN, SIP_TRUNKING | No |

These entries have synthetic ACTIVE status. NE-302 through NE-304 have no default enrichment record. Region labels also differ for Region-A versus Demo-Region-A; strict enrichment can reject mismatches. Unresolved IDs and mismatches must be retained in evidence, not interpreted as healthy equipment.

The prototype uses different equipment metadata: NE-102 is BACKHAUL_LINK and NE-103 is CELL_SITE. Its `SVC-DEMO-*` service IDs are not the primary workflow's service categories. Do not join these catalogs solely by NE ID without reconciling their definitions.

## Public forecast data and demo rules

The standalone prototype requests 24 hourly values of `precipitation` (mm) and `wind_gusts_10m` (km/h), with UTC timestamps. It sends latitude/longitude to Open-Meteo, not equipment IDs or service metadata. The returned grid coordinates may differ from the requested location.

Open-Meteo data attribution is recorded as **Weather data by Open-Meteo (CC BY 4.0)**. The free endpoint is subject to the provider's non-commercial service terms and usage limits; data licensing and API access terms are separate. Review [provider terms](https://open-meteo.com/en/terms) for the intended use. No separate licence is asserted here for team-created fixtures; their source code and submission revision provide generation provenance.

Rule `demo-weather-risk-v1` classifies weather exposure, not telecom service risk:

| Class | Rule over the returned forecast hours |
| --- | --- |
| HIGH | Precipitation >=10 mm in any hour OR gust >=60 km/h |
| MEDIUM | No HIGH condition, but precipitation >=5 mm OR gust >=40 km/h |
| LOW | Neither elevated threshold is crossed |
| UNKNOWN | Request failure or invalid/incomplete forecast |

The maxima may occur in different hours. These thresholds are team-defined demo assumptions, with `validated=false`. LOW is not a safety guarantee. The synthetic adverse fixture uses (precipitation mm, gust km/h): NE-101 (12,65), NE-102 (6,45), NE-103 (1,20).

The deployed Agent 1 source path consumes supplied weather severity; it does not currently import this forecast classifier. Its default bounding box in [network_mapper.py](agents/weather_network_intelligence/network_mapper.py) is synthetic metadata, not an API-derived storm footprint.

## Model and prompt provenance

The configured model identifier defaults to `us.anthropic.claude-haiku-4-5-20251001-v1:0` and can be overridden. [common/config.py](common/config.py) defaults local Bedrock enablement to false. A deployed runtime may have different environment settings.

`AGENT2_SYSTEM_PROMPT` and `build_reasoning_user_prompt()` define the prompt. The input includes correlation IDs, weather context, synthetic assets/services, deterministic risk scores and recommendation rationale. The expected output has `situation_summary`, `risk_explanation`, `recommendation_rationale`, and `operator_summary`. The model does not authorise actions or override deterministic decisions.

For each reported model-assisted result, preserve the actual requested model ID, prompt text/context, output, output SHA-256, timestamps, status and fallback reason under the same run ID. The current reasoning trace stores model/status/fallback information and parsed explanation, but the reviewed adapter does not persist the complete rendered prompt, raw response and output hash as a dedicated provenance bundle. Those are submission gaps, not records already produced by this document. A fallback result must not be presented as successful Bedrock generation; depending on the failure path the model ID can describe the attempted model while `fallback_used=true` identifies the actual outcome.

## Saved evidence and reproducibility

- **Declared S3 submission workflow (`scripts/run_declared_scenario.sh`):** Writes pre-run input declarations locally to `declared/<RUN_ID>.json`, uploads and verifies them at `s3://cohort3-agentcore-835156611439/declared/<RUN_ID>.json` prior to Agent 1 invocation, and mirrors the resulting run evidence directory (`agent1-response.json`, `agent2-response.json`, `cloudwatch-events.json`, `evidence-index.json`, `runtime-metadata.json`, `workflow-summary.json`, `declared.json`, `submission-summary.json`) to `s3://cohort3-agentcore-835156611439/evidence/<RUN_ID>/`. When `--all-scenarios` is executed, the 5-run manifest is written to `evidence/submission-runs.json` and mirrored to `s3://cohort3-agentcore-835156611439/evidence/submission-runs.json`.
- **Primary workflow:** `evidence/<run_id>/` contains collected agent responses, workflow summary, CloudWatch events, evidence index and runtime metadata. SSE validation additionally saves `workflow-stream.sse`. See [README](README.md) for collection commands.
- **Standalone prototype:** `runs/<run_id>/` contains inventory, assessment, draft handoff and audit JSONL; live mode adds raw forecast responses and request/retrieval details, while demo mode saves synthetic peaks. These files are not automatically merged into primary workflow evidence.
- **Submission:** capture SVN revision, generator/config versions, dataset hashes, exact run IDs and an independent checker. Freeze test inputs and declare targets before scored runs. Never backdate declarations or claim current live forecasts reproduce an older run.
- Generated runs should be preserved in the approved evidence location, not discarded merely because they are excluded from source control. Do not publish credentials or private data with evidence.

This documentation update generated no new measurements, model outputs, dataset hashes or historical provenance. The team must supply missing run-specific values and the pre-event asset history before submission. See [GAPS.md](GAPS.md).
