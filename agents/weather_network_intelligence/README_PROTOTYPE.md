# Agent 1 forecast prototype

This addition preserves the repository's existing `agent.py`, `main.py`, adapters,
and shared `contracts/network_risk_event.py`. It consolidates the independently
developed CloudShell forecast workflow. It does not change the existing agent entry point.

## Run from the repository root

The repository specifies Python 3.13+. This prototype needs only the standard library.

```powershell
python agents/weather_network_intelligence/forecast_prototype.py --mode demo
python agents/weather_network_intelligence/forecast_prototype.py --mode live --team-code YOUR_TEAM_CODE
python -m unittest discover -s agents/weather_network_intelligence/tests -p "test_forecast_prototype.py" -v
```

Demo mode is offline and uses synthetic Bengaluru-area inventory and synthetic
weather peaks: NE-101 HIGH, NE-102 MEDIUM, NE-103 LOW. Live mode requests a
24-hour Open-Meteo forecast at each inventory location. No API key is required by
the free endpoint; its non-commercial terms and attribution requirements apply:
https://open-meteo.com/en/terms and https://open-meteo.com/en/docs.
An organisation-approved network connection is required. Errors are not bypassed.

External inventory is a non-empty JSON array. Each object requires unique `ne_id`,
numeric `latitude` and `longitude`; optional fields are `element_type`,
`criticality`, and `associated_service_ids` (a list of strings).

```powershell
python agents/weather_network_intelligence/forecast_prototype.py --mode live --inventory inventory.json --inventory-source synthetic --team-code YOUR_TEAM_CODE
```

Use only public/synthetic data for this event. The provenance label must accurately
describe the supplied inventory. Latitude/longitude are sent to Open-Meteo; other
inventory attributes are not included in the API request.

## Meaning of the results

The `risk` field means **weather exposure**, not network/service risk. Nikunj owns
network risk, service impact, recommendation, action and verification. The demo
thresholds are HIGH for hourly precipitation >=10 mm OR gusts >=60 km/h; MEDIUM
for >=5 mm OR >=40 km/h; otherwise LOW. They are not validated engineering limits.
LOW is not a safety guarantee. Missing or invalid forecasts produce UNKNOWN and
an INCOMPLETE handoff, with process exit code 2. Bad CLI/input exits before a run.

Geo correlation here means requesting weather at each equipment coordinate. A
provider grid coordinate can differ from the requested coordinate. This does not
identify an official storm polygon or implement official alert ingestion.

## Draft contract and evidence

Each run is saved under `runs/<run_id>/` (override with `--output-dir`). Files include
inventory, assessment, JSONL audit and `agent2_event.json`; live runs save raw
forecast responses and request URLs, demo runs save synthetic inputs. Run and trace
IDs propagate through these artifacts. File creation timestamps are not weather
observation timestamps. Historical CloudShell runs are not part of this package.

`contracts/forecast_handoff.schema.json` documents **draft** `agent1-handoff-0.1`.
It is deliberately separate from the repository's authoritative `NetworkRiskEvent`.
The shared Pydantic model forbids extra fields and cannot consume this draft as-is.
Raja and Nikunj need to agree on a mapping or contract revision before integration;
UNKNOWN or LOW must not be converted to SEVERE_STORM/HIGH to fit the old contract.
Local evidence filenames resolve relative to the run directory, not a remote URL.

All events have `action_requested: false`, `official_warning: false`, and
`delivery_status: NOT_SENT`. No remediation or delivery is implemented.
Model calls, governance checkpoints, AgentCore deployment and CloudWatch are pending.

## SVN

Add the four supplied source/documentation/schema/test files explicitly. Do not
add generated runs, Python caches or credentials. `.gitignore` does not configure
SVN ignores. No SVN properties or repository commits are made by this package.

Tests run offline, use temporary directories, and mock all live HTTP responses.
They exercise normal/elevated/unknown outcomes, provenance, trace linkage,
coordinate validation, forecast validation, and CLI restrictions. They do not
prove deployment readiness or full conformance to the draft JSON Schema.
