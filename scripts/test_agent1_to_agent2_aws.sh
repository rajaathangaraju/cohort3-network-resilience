#!/usr/bin/env bash
set -euo pipefail

# Real AWS-to-AWS Agent 1 -> Agent 2 handoff, Bedrock diagnostic inspector,
# governance scenario runner, and automatic evidence bundle collector.
#
# Supports:
#   ./scripts/test_agent1_to_agent2_aws.sh
#   ./scripts/test_agent1_to_agent2_aws.sh --scenario pause
#   ./scripts/test_agent1_to_agent2_aws.sh --scenario pause --approve
#   ./scripts/test_agent1_to_agent2_aws.sh --scenario pause --reject
#   ./scripts/test_agent1_to_agent2_aws.sh --scenario allow
#   ./scripts/test_agent1_to_agent2_aws.sh --scenario deny
#   ./scripts/test_agent1_to_agent2_aws.sh --all-scenarios

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-2}"
AGENT1_RUNTIME_ARN="${AGENT1_RUNTIME_ARN:-arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_weather_network_intelligence-aRoVQk71fj}"
RUNTIME_INFO_PATH="$PROJECT_ROOT/dist/agent2_runtime_info.json"

SCENARIO="pause"
APPROVAL_MODE="NONE"
RUN_ALL="0"
DECLARED_RUN_ID=""
DECLARED_TRACE_ID=""

while [ $# -gt 0 ]; do
    case "$1" in
        --scenario)
            SCENARIO="$(echo "$2" | tr '[:upper:]' '[:lower:]')"
            shift 2
            ;;
        --approve)
            APPROVAL_MODE="APPROVED"
            shift
            ;;
        --reject)
            APPROVAL_MODE="REJECTED"
            shift
            ;;
        --all-scenarios)
            RUN_ALL="1"
            shift
            ;;
        --run-id)
            DECLARED_RUN_ID="$2"
            shift 2
            ;;
        --trace-id)
            DECLARED_TRACE_ID="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

LIST_JSON="$(mktemp "${TMPDIR:-/tmp}/agent2_discover.XXXXXX.json")"
trap 'rm -f "$LIST_JSON"' EXIT

# Discover Agent 2 Runtime ARN
if [ -n "${AGENT2_RUNTIME_ARN:-}" ]; then
    RESOLVED_AGENT2_ARN="$AGENT2_RUNTIME_ARN"
elif [ -f "$RUNTIME_INFO_PATH" ]; then
    RESOLVED_AGENT2_ARN="$(python3 - "$RUNTIME_INFO_PATH" <<'PYINFO'
import json
from pathlib import Path
import sys
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("agentRuntimeArn", ""))
PYINFO
)"
else
    aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" > "$LIST_JSON"
    RESOLVED_AGENT2_ARN="$(python3 - "$LIST_JSON" <<'PYLIST'
import json
from pathlib import Path
import sys
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for rt in data.get("agentRuntimes", []):
    if isinstance(rt, dict) and rt.get("agentRuntimeName") == "cohort3_network_resilience":
        print(rt.get("agentRuntimeArn", ""))
        break
PYLIST
)"
fi

if [ -z "${RESOLVED_AGENT2_ARN:-}" ]; then
    echo "ERROR: Agent 2 Runtime ARN could not be discovered. Run ./scripts/deploy_agent2.sh first." >&2
    exit 1
fi

LAST_RUN_ID=""
LAST_TRACE_ID=""

run_single_handoff() {
    local scen="$1"
    local appr_mode="$2"
    local scen_key="${scen}_${appr_mode}"

    local a1_in a1_out a2_in a2_out
    a1_in="$(mktemp "${TMPDIR:-/tmp}/a1_in.XXXXXX.json")"
    a1_out="$(mktemp "${TMPDIR:-/tmp}/a1_out.XXXXXX.json")"
    a2_in="$(mktemp "${TMPDIR:-/tmp}/a2_in.XXXXXX.json")"
    a2_out="$(mktemp "${TMPDIR:-/tmp}/a2_out.XXXXXX.json")"

    python3 - "$scen" "$a1_in" "$DECLARED_RUN_ID" "$DECLARED_TRACE_ID" <<'PYBUILD'
import json
from pathlib import Path
import sys
import uuid

scen = sys.argv[1]
out_path = Path(sys.argv[2])
suffix = uuid.uuid4().hex[:8].upper()
declared_run_id = sys.argv[3]
declared_trace_id = sys.argv[4]

if scen == "allow":
    payload = {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": declared_run_id or f"WX-ALLOW-{suffix}",
        "trace_id": declared_trace_id or f"TRACE-ALLOW-{suffix}",
        "data_source": "synthetic",
        "weather_event": "HIGH_WIND",
        "severity": "LOW",
        "affected_region": "Region-B",
    }
elif scen == "deny":
    payload = {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": declared_run_id or f"WX-DENY-{suffix}",
        "trace_id": declared_trace_id or f"TRACE-DENY-{suffix}",
        "data_source": "synthetic",
        "weather_event": "SEVERE_STORM",
        "severity": "HIGH",
        "affected_region": "Region-C",
    }
else:
    payload = {
        "schema_version": "0.1",
        "team_code": "TEAM_PENDING",
        "run_id": declared_run_id or f"WX-PAUSE-{suffix}",
        "trace_id": declared_trace_id or f"TRACE-PAUSE-{suffix}",
        "data_source": "synthetic",
        "weather_event": "SEVERE_STORM",
        "severity": "HIGH",
        "affected_region": "Demo-Region-A",
    }

out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PYBUILD

    local sess_a1 sess_a2
    sess_a1="$(python3 -c 'import uuid; print(f"session-a1-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}")')"
    sess_a2="$(python3 -c 'import uuid; print(f"session-a2-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}")')"

    echo "============================================================"
    echo "Step 1: Invoking AWS AgentCore Agent 1 (scenario=$scen, approval=$appr_mode)"
    echo "Agent 1 ARN:     $AGENT1_RUNTIME_ARN"
    echo "Agent 1 Session: $sess_a1"
    echo "============================================================"

    aws bedrock-agentcore invoke-agent-runtime \
        --agent-runtime-arn "$AGENT1_RUNTIME_ARN" \
        --runtime-session-id "$sess_a1" \
        --content-type "application/json" \
        --accept "application/json" \
        --payload "fileb://$a1_in" \
        --region "$AWS_REGION" \
        "$a1_out"

    python3 - "$a1_out" "$a2_in" "$scen" "$appr_mode" <<'PYPREP'
import json
from pathlib import Path
import sys

a1_raw = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
a1_data = json.loads(a1_raw)
if isinstance(a1_data, str):
    a1_data = json.loads(a1_data)

scen = sys.argv[3]
appr_mode = sys.argv[4]

a2_payload = dict(a1_data)
if scen == "deny":
    a2_payload["force_deny"] = True
    a2_payload["force_deny_reason"] = "Explicit governance DENY scenario policy rule."
if appr_mode in ("APPROVED", "REJECTED"):
    a2_payload["simulated_approval_decision"] = appr_mode
    a2_payload["approver_reference"] = "demo-approver"

Path(sys.argv[2]).write_text(json.dumps(a2_payload, indent=2) + "\n", encoding="utf-8")
PYPREP

    echo "============================================================"
    echo "Step 2: Passing Actual Agent 1 Response to AWS AgentCore Agent 2"
    echo "Agent 2 ARN:     $RESOLVED_AGENT2_ARN"
    echo "Agent 2 Session: $sess_a2"
    echo "============================================================"

    aws bedrock-agentcore invoke-agent-runtime \
        --agent-runtime-arn "$RESOLVED_AGENT2_ARN" \
        --runtime-session-id "$sess_a2" \
        --content-type "application/json" \
        --accept "application/json" \
        --payload "fileb://$a2_in" \
        --region "$AWS_REGION" \
        "$a2_out"

    READ_IDS="$(python3 - "$a1_out" "$a2_out" "$scen" "$appr_mode" "$PROJECT_ROOT" "$AGENT1_RUNTIME_ARN" "$RESOLVED_AGENT2_ARN" "$sess_a1" "$sess_a2" "$AWS_REGION" "$scen_key" <<'PYVERIFY'
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

a1_data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8").strip())
if isinstance(a1_data, str):
    a1_data = json.loads(a1_data)

a2_data = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8").strip())
if isinstance(a2_data, str):
    a2_data = json.loads(a2_data)

scen = sys.argv[3]
appr_mode = sys.argv[4]
project_root = Path(sys.argv[5])
a1_arn = sys.argv[6]
a2_arn = sys.argv[7]
sess_a1 = sys.argv[8]
sess_a2 = sys.argv[9]
aws_region = sys.argv[10]
scen_key = sys.argv[11]

for key in ("run_id", "trace_id", "schema_version", "team_code", "data_source"):
    if a1_data.get(key) != a2_data.get(key):
        raise SystemExit(
            f"ERROR: Correlation mismatch on '{key}': Agent1={a1_data.get(key)!r} vs Agent2={a2_data.get(key)!r}"
        )

in_ctx = a2_data.get("input_context", {})
for key in ("weather_event", "severity", "affected_region", "affected_network_elements"):
    if a1_data.get(key) != in_ctx.get(key):
        raise SystemExit(
            f"ERROR: Input context mismatch on '{key}': Agent1={a1_data.get(key)!r} vs Agent2={in_ctx.get(key)!r}"
        )

gov = a2_data["governance"]["decision"]
apr = a2_data["approval"]["status"]
exe = a2_data["execution"]["status"]
ver = a2_data["verification"]["status"]
ai_reasoning = a2_data.get("ai_reasoning") or {}
fallback_used = ai_reasoning.get("fallback_used")
bedrock_model = ai_reasoning.get("model_id")
bedrock_status = ai_reasoning.get("result_status")
bedrock_err = ai_reasoning.get("error_reason")

if scen == "deny":
    assert gov == "DENY", f"Expected DENY, got {gov}"
    assert apr == "NOT_REQUIRED", f"Expected NOT_REQUIRED, got {apr}"
    assert exe == "BLOCKED", f"Expected BLOCKED, got {exe}"
    assert ver == "BLOCKED", f"Expected BLOCKED, got {ver}"
elif scen == "allow":
    assert gov == "ALLOW", f"Expected ALLOW, got {gov}"
    assert apr == "NOT_REQUIRED", f"Expected NOT_REQUIRED, got {apr}"
    assert exe == "EXECUTED", f"Expected EXECUTED, got {exe}"
    assert ver == "PASSED", f"Expected PASSED, got {ver}"
elif scen == "pause" and appr_mode == "NONE":
    assert gov == "PAUSE", f"Expected PAUSE, got {gov}"
    assert apr == "PENDING", f"Expected PENDING, got {apr}"
    assert exe == "BLOCKED", f"Expected BLOCKED, got {exe}"
    assert ver in ("NOT_STARTED", "BLOCKED"), f"Expected NOT_STARTED/BLOCKED, got {ver}"
elif scen == "pause" and appr_mode == "APPROVED":
    assert gov == "PAUSE", f"Expected PAUSE, got {gov}"
    assert apr == "APPROVED", f"Expected APPROVED, got {apr}"
    assert exe == "EXECUTED", f"Expected EXECUTED, got {exe}"
    assert ver == "PASSED", f"Expected PASSED, got {ver}"
elif scen == "pause" and appr_mode == "REJECTED":
    assert gov == "PAUSE", f"Expected PAUSE, got {gov}"
    assert apr == "REJECTED", f"Expected REJECTED, got {apr}"
    assert exe == "BLOCKED", f"Expected BLOCKED, got {exe}"
    assert ver == "BLOCKED", f"Expected BLOCKED, got {ver}"

run_id = a2_data["run_id"]
trace_id = a2_data["trace_id"]

metadata = {
    "run_id": run_id,
    "trace_id": trace_id,
    "scenario": scen,
    "approval_mode": appr_mode,
    "aws_region": aws_region,
    "agent1_runtime_arn": a1_arn,
    "agent2_runtime_arn": a2_arn,
    "agent1_session_id": sess_a1,
    "agent2_session_id": sess_a2,
    "bedrock_model_id": bedrock_model,
    "bedrock_result_status": bedrock_status,
    "bedrock_fallback_used": fallback_used,
    "bedrock_error_reason": bedrock_err,
    "recorded_at": datetime.now(UTC).isoformat(),
}

dist_dir = project_root / "dist"
run_cache_dir = dist_dir / "runs" / run_id
scen_cache_dir = dist_dir / "scenarios" / scen_key
run_cache_dir.mkdir(parents=True, exist_ok=True)
scen_cache_dir.mkdir(parents=True, exist_ok=True)

for target_dir in (dist_dir, run_cache_dir, scen_cache_dir):
    (target_dir / "last_agent1_response.json").write_text(
        json.dumps(a1_data, indent=2) + "\n", encoding="utf-8"
    )
    (target_dir / "last_agent2_response.json").write_text(
        json.dumps(a2_data, indent=2) + "\n", encoding="utf-8"
    )
    (target_dir / "last_runtime_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

print(f"{run_id}|{trace_id}|{bedrock_model}|{bedrock_status}|{fallback_used}|{bedrock_err or 'NONE'}|{gov}|{apr}|{exe}|{ver}")
PYVERIFY
)"
    IFS='|' read -r LAST_RUN_ID LAST_TRACE_ID B_MODEL B_STATUS B_FALLBACK B_ERR GOV_OUT APR_OUT EXE_OUT VER_OUT <<< "$READ_IDS"

    echo "============================================================"
    echo "AWS AGENT 1 TO AGENT 2 HANDOFF PASSED"
    echo "Scenario:              $scen (approval=$appr_mode)"
    echo "run_id:                $LAST_RUN_ID"
    echo "trace_id:              $LAST_TRACE_ID"
    echo "bedrock_model_id:      $B_MODEL"
    echo "bedrock_result_status: $B_STATUS"
    echo "bedrock_fallback_used: $B_FALLBACK"
    if [ "$B_ERR" != "NONE" ]; then
        echo "bedrock_error_reason:  $B_ERR"
    fi
    echo "governance decision:   $GOV_OUT"
    echo "approval status:       $APR_OUT"
    echo "execution status:      $EXE_OUT"
    echo "verification status:   $VER_OUT"
    echo "============================================================"

    rm -f "$a1_in" "$a1_out" "$a2_in" "$a2_out"
}

if [ "$RUN_ALL" = "1" ]; then
    run_single_handoff "deny" "NONE"
    DENY_RUN_ID="$LAST_RUN_ID"
    DENY_TRACE_ID="$LAST_TRACE_ID"

    run_single_handoff "allow" "NONE"
    ALLOW_RUN_ID="$LAST_RUN_ID"
    ALLOW_TRACE_ID="$LAST_TRACE_ID"

    run_single_handoff "pause" "NONE"
    PAUSE_RUN_ID="$LAST_RUN_ID"
    PAUSE_TRACE_ID="$LAST_TRACE_ID"

    run_single_handoff "pause" "APPROVED"
    APPROVE_RUN_ID="$LAST_RUN_ID"
    APPROVE_TRACE_ID="$LAST_TRACE_ID"

    run_single_handoff "pause" "REJECTED"
    REJECT_RUN_ID="$LAST_RUN_ID"
    REJECT_TRACE_ID="$LAST_TRACE_ID"

    echo "============================================================"
    echo "Collecting Evidence Bundles for DENY, PAUSE+APPROVE, and PAUSE+REJECT"
    echo "============================================================"
    "$SCRIPT_DIR/collect_run_evidence.sh" --run-id "$DENY_RUN_ID" --trace-id "$DENY_TRACE_ID" --wait-seconds 4
    "$SCRIPT_DIR/collect_run_evidence.sh" --run-id "$APPROVE_RUN_ID" --trace-id "$APPROVE_TRACE_ID" --wait-seconds 2
    "$SCRIPT_DIR/collect_run_evidence.sh" --run-id "$REJECT_RUN_ID" --trace-id "$REJECT_TRACE_ID" --wait-seconds 2

    echo "============================================================"
    echo "ALL 5 AWS GOVERNANCE SCENARIOS & 3 DEMO EVIDENCE BUNDLES COMPLETED"
    echo "  DENY Bundle:          evidence/$DENY_RUN_ID/"
    echo "  ALLOW Run:            $ALLOW_RUN_ID ($ALLOW_TRACE_ID)"
    echo "  PAUSE Run:            $PAUSE_RUN_ID ($PAUSE_TRACE_ID)"
    echo "  PAUSE+APPROVE Bundle: evidence/$APPROVE_RUN_ID/"
    echo "  PAUSE+REJECT Bundle:  evidence/$REJECT_RUN_ID/"
    echo "============================================================"
else
    run_single_handoff "$SCENARIO" "$APPROVAL_MODE"
fi
