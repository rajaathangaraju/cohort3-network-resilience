#!/usr/bin/env bash
set -euo pipefail

# Phase 16: Collect correlated CloudWatch evidence across Agent 1 and Agent 2 log groups
# and generate the final evidence bundle under:
#   evidence/<run_id>/
#     - agent1-response.json
#     - agent2-response.json
#     - cloudwatch-events.json
#     - workflow-summary.json
#     - evidence-index.json
#     - runtime-metadata.json
#
# Supports both named flags and positional arguments:
#   ./scripts/collect_run_evidence.sh --run-id <RUN_ID> --trace-id <TRACE_ID>
#   ./scripts/collect_run_evidence.sh <RUN_ID> <TRACE_ID>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-2}"

AGENT1_LOG_GROUP="/aws/bedrock-agentcore/runtimes/cohort3_weather_network_intelligence-aRoVQk71fj-DEFAULT"
RUNTIME_INFO_PATH="$PROJECT_ROOT/dist/agent2_runtime_info.json"

RUN_ID=""
TRACE_ID=""
WAIT_SECONDS="${CLOUDWATCH_INGEST_WAIT_SECONDS:-5}"

while [ $# -gt 0 ]; do
    case "$1" in
        --run-id)
            RUN_ID="$2"
            shift 2
            ;;
        --trace-id)
            TRACE_ID="$2"
            shift 2
            ;;
        --wait-seconds)
            WAIT_SECONDS="$2"
            shift 2
            ;;
        *)
            if [ -z "$RUN_ID" ]; then
                RUN_ID="$1"
            elif [ -z "$TRACE_ID" ]; then
                TRACE_ID="$1"
            fi
            shift
            ;;
    esac
done

echo "============================================================"
echo "Phase 16: CloudWatch Log Discovery & Evidence Bundle Builder"
echo "============================================================"

# Allow a brief ingestion window if collecting immediately after a live invocation
if [ "$WAIT_SECONDS" -gt 0 ] 2>/dev/null; then
    echo "Waiting ${WAIT_SECONDS}s for CloudWatch log stream ingestion ..."
    sleep "$WAIT_SECONDS"
fi

python3 - "$PROJECT_ROOT" "$RUN_ID" "$TRACE_ID" "$AWS_REGION" "$AGENT1_LOG_GROUP" "$RUNTIME_INFO_PATH" <<'PYCOLLECT'
import json
from pathlib import Path
import sys
import time

project_root = Path(sys.argv[1])
sys.path.insert(0, str(project_root))

from integrations.cloudwatch import (
    discover_agent2_log_group,
    fetch_correlated_cloudwatch_events,
    write_run_evidence_bundle,
)

run_id_arg = sys.argv[2].strip()
trace_id_arg = sys.argv[3].strip()
region_name = sys.argv[4]
agent1_log_group = sys.argv[5]
runtime_info_path = Path(sys.argv[6])

dist_dir = project_root / "dist"
run_cache_dir = dist_dir / "runs" / run_id_arg if run_id_arg else None

if run_cache_dir is not None and (run_cache_dir / "last_agent2_response.json").exists():
    source_dir = run_cache_dir
elif (dist_dir / "last_agent2_response.json").exists():
    source_dir = dist_dir
else:
    source_dir = None

runtime_meta = None
if source_dir is not None:
    a1_resp = json.loads((source_dir / "last_agent1_response.json").read_text(encoding="utf-8"))
    a2_resp = json.loads((source_dir / "last_agent2_response.json").read_text(encoding="utf-8"))
    meta_file = source_dir / "last_runtime_metadata.json"
    if meta_file.exists():
        runtime_meta = json.loads(meta_file.read_text(encoding="utf-8"))
else:
    from orchestration.workflow import DemoScenario, EndToEndOrchestrator

    orch = EndToEndOrchestrator()
    res = orch.run_scenario(DemoScenario.PAUSE)
    a2_resp = res.model_dump(mode="json")
    a1_resp = {
        "schema_version": res.schema_version,
        "team_code": res.team_code,
        "run_id": res.run_id,
        "trace_id": res.trace_id,
        "created_at": res.created_at.isoformat(),
        "data_source": res.data_source,
        "weather_event": res.input_context.weather_event,
        "severity": res.input_context.severity,
        "affected_region": res.input_context.affected_region,
        "forecast": res.input_context.forecast,
        "affected_area": res.input_context.affected_area.model_dump(mode="json"),
        "affected_network_elements": res.input_context.affected_network_elements,
    }

run_id = run_id_arg or str(a2_resp.get("run_id", ""))
trace_id = trace_id_arg or str(a2_resp.get("trace_id", ""))

if not run_id or not trace_id:
    raise SystemExit("ERROR: Both --run-id and --trace-id are required.")

a1_resp["run_id"] = run_id
a1_resp["trace_id"] = trace_id
a2_resp["run_id"] = run_id
a2_resp["trace_id"] = trace_id

# Dynamically discover Agent 2 log group and runtime ARN
agent2_log_group, agent2_arn = discover_agent2_log_group(
    region_name=region_name,
    runtime_info_path=runtime_info_path,
)

log_groups = [agent1_log_group]
if agent2_log_group and agent2_log_group not in log_groups:
    log_groups.append(agent2_log_group)

session_ids = []
if isinstance(runtime_meta, dict):
    for key in ("agent1_session_id", "agent2_session_id"):
        val = runtime_meta.get(key)
        if isinstance(val, str) and val.strip():
            session_ids.append(val.strip())
    if not runtime_meta.get("agent2_runtime_arn") and agent2_arn:
        runtime_meta["agent2_runtime_arn"] = agent2_arn

# Retry up to 3 times if CloudWatch log ingestion is still catching up
cw_events = []
for attempt in range(1, 4):
    cw_events = fetch_correlated_cloudwatch_events(
        log_group_names=log_groups,
        run_id=run_id,
        trace_id=trace_id,
        session_ids=session_ids,
        region_name=region_name,
    )
    if cw_events or attempt == 3:
        break
    print(f"[Attempt {attempt}/3] 0 correlated CloudWatch events yet; waiting 4s for ingestion ...")
    time.sleep(4)

evidence_dir = write_run_evidence_bundle(
    output_root=project_root / "evidence",
    run_id=run_id,
    trace_id=trace_id,
    agent1_response=a1_resp,
    agent2_response=a2_resp,
    cloudwatch_events=cw_events,
    runtime_metadata=runtime_meta,
)

a1_events = sum(
    1
    for e in cw_events
    if (agent1_log_group and agent1_log_group in str(e.get("logGroupName", "")))
    or "weather_network_intelligence" in str(e.get("message", ""))
)
a2_events = sum(
    1
    for e in cw_events
    if (agent2_log_group and agent2_log_group in str(e.get("logGroupName", "")))
    or '"agent_name": "network_resilience"' in str(e.get("message", ""))
    or '"agent_name":"network_resilience"' in str(e.get("message", ""))
)

ai_reasoning = a2_resp.get("ai_reasoning") or {}
gov_status = (a2_resp.get("governance") or {}).get("decision")
apr_status = (a2_resp.get("approval") or {}).get("status")
exe_status = (a2_resp.get("execution") or {}).get("status")
ver_status = (a2_resp.get("verification") or {}).get("status")

print("============================================================")
print("EVIDENCE BUNDLE GENERATED")
print(f"run_id:                  {run_id}")
print(f"trace_id:                {trace_id}")
print(f"Log groups queried:      {log_groups}")
print(f"CloudWatch events total: {len(cw_events)}")
print(f"Agent 1 CW events count: {a1_events}")
print(f"Agent 2 CW events count: {a2_events}")
print(f"Bedrock model_id:        {ai_reasoning.get('model_id')}")
print(f"Bedrock result_status:   {ai_reasoning.get('result_status')}")
print(f"Bedrock fallback_used:   {ai_reasoning.get('fallback_used')}")
if ai_reasoning.get("error_reason"):
    print(f"Bedrock error_reason:    {ai_reasoning.get('error_reason')}")
print(f"governance status:       {gov_status}")
print(f"approval status:         {apr_status}")
print(f"execution status:        {exe_status}")
print(f"verification status:     {ver_status}")
print(f"Evidence directory:      {evidence_dir}")
for fname in sorted(p.name for p in evidence_dir.iterdir()):
    print(f"  - {evidence_dir / fname}")
print("============================================================")
PYCOLLECT
