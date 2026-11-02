#!/usr/bin/env bash
set -euo pipefail

# Remote validation script for deployed Agent 2 on Amazon Bedrock AgentCore Runtime.
# Discovers Agent 2 runtime ARN from AGENT2_RUNTIME_ARN, dist/agent2_runtime_info.json,
# or aws bedrock-agentcore-control list-agent-runtimes, invokes it with the exact Agent 1
# NetworkRiskEvent contract, and validates the returned Agent2Result.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-2}"
RUNTIME_INFO_PATH="$PROJECT_ROOT/dist/agent2_runtime_info.json"

PAYLOAD_FILE="$(mktemp "${TMPDIR:-/tmp}/agent2_remote_payload.XXXXXX.json")"
RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/agent2_remote_response.XXXXXX.json")"
LIST_JSON="$(mktemp "${TMPDIR:-/tmp}/agent2_list.XXXXXX.json")"

trap 'rm -f "$PAYLOAD_FILE" "$RESPONSE_FILE" "$LIST_JSON"' EXIT

# Discover Agent 2 Runtime ARN and Log Group
if [ -n "${AGENT2_RUNTIME_ARN:-}" ]; then
    RUNTIME_ARN="$AGENT2_RUNTIME_ARN"
    RUNTIME_ID="${RUNTIME_ARN##*/}"
elif [ -f "$RUNTIME_INFO_PATH" ]; then
    READ_INFO="$(python3 - "$RUNTIME_INFO_PATH" <<'PYINFO'
import json
from pathlib import Path
import sys
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"{data.get('agentRuntimeArn','')}|{data.get('agentRuntimeId','')}")
PYINFO
)"
    IFS='|' read -r RUNTIME_ARN RUNTIME_ID <<< "$READ_INFO"
else
    aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" > "$LIST_JSON"
    READ_INFO="$(python3 - "$LIST_JSON" <<'PYLIST'
import json
from pathlib import Path
import sys
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for rt in data.get("agentRuntimes", []):
    if isinstance(rt, dict) and rt.get("agentRuntimeName") == "cohort3_network_resilience":
        print(f"{rt.get('agentRuntimeArn','')}|{rt.get('agentRuntimeId','')}")
        break
PYLIST
)"
    IFS='|' read -r RUNTIME_ARN RUNTIME_ID <<< "$READ_INFO"
fi

if [ -z "${RUNTIME_ARN:-}" ]; then
    echo "ERROR: Could not determine Agent 2 Runtime ARN. Run ./scripts/deploy_agent2.sh first." >&2
    exit 1
fi

LOG_GROUP="/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"

print_cloudwatch_diagnostics() {
    cat >&2 <<CWEOF

============================================================
AGENT 2 CLOUDWATCH FAILURE DIAGNOSTICS
============================================================
Log Group:
  $LOG_GROUP

Inspect recent runtime log streams and events with:

  aws logs describe-log-streams \\
    --log-group-name "$LOG_GROUP" \\
    --order-by LastEventTime \\
    --descending \\
    --limit 5 \\
    --region "$AWS_REGION"

  aws logs tail "$LOG_GROUP" \\
    --since 15m \\
    --region "$AWS_REGION"
============================================================
CWEOF
}

SESSION_ID="$(python3 - <<'PYSESS'
import uuid
print(f"session-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}")
PYSESS
)"

cat > "$PAYLOAD_FILE" <<'JSONEOF'
{
  "schema_version": "0.1",
  "team_code": "TEAM_PENDING",
  "run_id": "WX-46329ae2b74b",
  "trace_id": "7a4106b675e4419f87b9b84d30a67ca4",
  "created_at": "2026-11-02T00:26:32.732111+00:00",
  "data_source": "synthetic",
  "weather_event": "SEVERE_STORM",
  "severity": "HIGH",
  "affected_region": "Demo-Region-A",
  "forecast": "Synthetic storm scenario for integration testing.",
  "affected_area": {
    "south": 12.8,
    "north": 13.2,
    "west": 77.4,
    "east": 77.8
  },
  "affected_network_elements": [
    "NE-101",
    "NE-102"
  ]
}
JSONEOF

echo "============================================================"
echo "Invoking Remote AgentCore Runtime (Agent 2)"
echo "Runtime ARN: $RUNTIME_ARN"
echo "Region:      $AWS_REGION"
echo "Session ID:  $SESSION_ID"
echo "============================================================"

if ! aws bedrock-agentcore invoke-agent-runtime \
    --agent-runtime-arn "$RUNTIME_ARN" \
    --runtime-session-id "$SESSION_ID" \
    --content-type "application/json" \
    --accept "application/json" \
    --payload "fileb://$PAYLOAD_FILE" \
    --region "$AWS_REGION" \
    "$RESPONSE_FILE"; then
    echo "ERROR: aws bedrock-agentcore invoke-agent-runtime failed for Agent 2." >&2
    print_cloudwatch_diagnostics
    exit 1
fi

echo "Raw Agent 2 remote response:"
cat "$RESPONSE_FILE"
echo ""

if ! python3 - "$RESPONSE_FILE" "$RUNTIME_ARN" <<'PYCHECK'
import json
from pathlib import Path
import sys

resp_path = Path(sys.argv[1])
runtime_arn = sys.argv[2]

raw_text = resp_path.read_text(encoding="utf-8").strip()
if not raw_text:
    raise SystemExit("ERROR: Agent 2 remote response file is empty.")

data = json.loads(raw_text)
if isinstance(data, str):
    data = json.loads(data)

if not isinstance(data, dict):
    raise SystemExit(f"ERROR: Expected JSON object response, got {type(data).__name__}")

# Validate correlation & input preservation
assert data.get("run_id") == "WX-46329ae2b74b", f"run_id mismatch: {data.get('run_id')}"
assert data.get("trace_id") == "7a4106b675e4419f87b9b84d30a67ca4", f"trace_id mismatch: {data.get('trace_id')}"
assert data.get("schema_version") == "0.1", f"schema_version mismatch: {data.get('schema_version')}"

input_ctx = data.get("input_context", {})
assert input_ctx.get("affected_region") == "Demo-Region-A", f"affected_region mismatch: {input_ctx}"
elements = input_ctx.get("affected_network_elements", [])
assert "NE-101" in elements and "NE-102" in elements, f"Missing NE-101/NE-102 in {elements}"

# Validate required sections
for section in ("risk_assessment", "recommendation", "governance", "approval", "execution", "verification", "evidence"):
    if section not in data or data[section] is None:
        raise SystemExit(f"ERROR: Missing required section '{section}' in Agent 2 response")

risk = data["risk_assessment"]
rec = data["recommendation"]
gov = data["governance"]
apr = data["approval"]
exe = data["execution"]
ver = data["verification"]

assert gov.get("decision") == "PAUSE", f"Expected PAUSE governance decision, got {gov}"
assert apr.get("status") == "PENDING", f"Expected PENDING approval status, got {apr}"
assert exe.get("status") == "BLOCKED", f"Expected BLOCKED execution status, got {exe}"
assert ver.get("status") in ("NOT_STARTED", "BLOCKED"), f"Unexpected verification status: {ver}"

print("============================================================")
print("AGENT 2 REMOTE VALIDATION PASSED")
print(f"run_id:              {data['run_id']}")
print(f"trace_id:            {data['trace_id']}")
print(f"Agent 2 runtime ARN: {runtime_arn}")
print(f"risk score:          {risk.get('risk_score')}")
print(f"overall risk:        {risk.get('overall_risk')}")
print(f"recommendation:      {rec.get('action')}")
print(f"governance decision: {gov.get('decision')}")
print(f"approval status:     {apr.get('status')}")
print(f"execution status:    {exe.get('status')}")
print("============================================================")
PYCHECK
then
    print_cloudwatch_diagnostics
    exit 1
fi
