#!/usr/bin/env bash
set -euo pipefail

# Remote validation script for deployed Agent 1 on Amazon Bedrock AgentCore Runtime.
# Invokes cohort3_weather_network_intelligence-aRoVQk71fj and validates the returned
# NetworkRiskEvent contract.

AWS_REGION="${AWS_REGION:-us-east-2}"
RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_weather_network_intelligence-aRoVQk71fj"
LOG_GROUP="/aws/bedrock-agentcore/runtimes/cohort3_weather_network_intelligence-aRoVQk71fj-DEFAULT"

PAYLOAD_FILE="$(mktemp "${TMPDIR:-/tmp}/agent1_remote_payload.XXXXXX.json")"
RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/agent1_remote_response.XXXXXX.json")"

trap 'rm -f "$PAYLOAD_FILE" "$RESPONSE_FILE"' EXIT

print_cloudwatch_diagnostics() {
    cat >&2 <<CWEOF

============================================================
CLOUDWATCH FAILURE DIAGNOSTICS
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
  "weather_event": "SEVERE_STORM",
  "severity": "HIGH",
  "affected_region": "Demo-Region-A"
}
JSONEOF

echo "============================================================"
echo "Invoking Remote AgentCore Runtime (Agent 1)"
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
    echo "ERROR: aws bedrock-agentcore invoke-agent-runtime failed." >&2
    print_cloudwatch_diagnostics
    exit 1
fi

echo "Raw remote response:"
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
    raise SystemExit("ERROR: Remote response file is empty.")

data = json.loads(raw_text)
if isinstance(data, str):
    data = json.loads(data)

if not isinstance(data, dict):
    raise SystemExit(f"ERROR: Expected JSON object response, got {type(data).__name__}")

run_id = data.get("run_id")
trace_id = data.get("trace_id")
if not isinstance(run_id, str) or not run_id.strip():
    raise SystemExit(f"ERROR: Missing or empty run_id: {run_id!r}")
if not isinstance(trace_id, str) or not trace_id.strip():
    raise SystemExit(f"ERROR: Missing or empty trace_id: {trace_id!r}")

checks = [
    ("schema_version", data.get("schema_version"), "0.1"),
    ("weather_event", data.get("weather_event"), "SEVERE_STORM"),
    ("severity", data.get("severity"), "HIGH"),
    ("affected_region", data.get("affected_region"), "Demo-Region-A"),
]
for field_name, actual, expected in checks:
    if actual != expected:
        raise SystemExit(f"ERROR: {field_name} mismatch: expected {expected!r}, got {actual!r}")

elements = data.get("affected_network_elements")
if not isinstance(elements, list):
    raise SystemExit(f"ERROR: affected_network_elements is not a list: {elements!r}")

for required_ne in ("NE-101", "NE-102"):
    if required_ne not in elements:
        raise SystemExit(
            f"ERROR: affected_network_elements missing {required_ne!r}; got {elements!r}"
        )

print("============================================================")
print("AGENT 1 REMOTE VALIDATION PASSED")
print(f"run_id:      {run_id}")
print(f"trace_id:    {trace_id}")
print(f"runtime ARN: {runtime_arn}")
print("============================================================")
PYCHECK
then
    print_cloudwatch_diagnostics
    exit 1
fi
