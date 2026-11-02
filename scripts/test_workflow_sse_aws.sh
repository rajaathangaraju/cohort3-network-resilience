#!/usr/bin/env bash
set -euo pipefail

# Live AWS Server-Sent Events (SSE) workflow streaming validation script.
# 1. Starts the Command Center Backend in COMMAND_CENTER_DATA_MODE=aws_live
# 2. Calls POST /api/runs/stream with curl -N (unbuffered) across DENY, ALLOW, PAUSE,
#    PAUSE+APPROVE, and PAUSE+REJECT
# 3. Validates event ordering, strictly increasing sequence numbers, run_id & trace_id
#    preservation, and final run_completed event
# 4. Saves each captured stream under evidence/<run_id>/workflow-stream.sse

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-2}"
PORT="${COMMAND_CENTER_PORT:-8092}"
BACKEND_URL="http://127.0.0.1:${PORT}"

AGENT1_RUNTIME_ARN="${AGENT1_RUNTIME_ARN:-arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_weather_network_intelligence-aRoVQk71fj}"
AGENT2_RUNTIME_ARN="${AGENT2_RUNTIME_ARN:-arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_network_resilience-ph5xzN4X1u}"

SERVER_LOG="$(mktemp "${TMPDIR:-/tmp}/sse_backend.XXXXXX.log")"
PAYLOAD_FILE="$(mktemp "${TMPDIR:-/tmp}/sse_payload.XXXXXX.json")"
STREAM_TMP="$(mktemp "${TMPDIR:-/tmp}/sse_stream.XXXXXX.sse")"
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -f "$SERVER_LOG" "$PAYLOAD_FILE" "$STREAM_TMP"
}
trap cleanup EXIT

echo "============================================================"
echo "Starting Command Center Backend in AWS_LIVE mode on $BACKEND_URL"
echo "Agent 1 ARN: $AGENT1_RUNTIME_ARN"
echo "Agent 2 ARN: $AGENT2_RUNTIME_ARN"
echo "============================================================"

COMMAND_CENTER_DATA_MODE=aws_live \
AWS_REGION="$AWS_REGION" \
AGENT1_RUNTIME_ARN="$AGENT1_RUNTIME_ARN" \
AGENT2_RUNTIME_ARN="$AGENT2_RUNTIME_ARN" \
python3 -m command_center.backend.service --host 127.0.0.1 --port "$PORT" --data-mode aws_live >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

# Wait for backend server to accept connections
for _ in $(seq 1 30); do
    if curl -s -o /dev/null "${BACKEND_URL}/api/runs/NONEXISTENT" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

run_sse_scenario() {
    local scen="$1"
    local appr_inst="$2"

    python3 - "$scen" "$appr_inst" "$PAYLOAD_FILE" <<'PYPAYLOAD'
import json
from pathlib import Path
import sys
import uuid

scen = sys.argv[1]
appr_inst = sys.argv[2]
out_path = Path(sys.argv[3])
suffix = uuid.uuid4().hex[:8].upper()

payload = {
    "scenario": scen,
    "run_id": f"WX-SSE-{scen.upper()}-{suffix}",
    "trace_id": f"TRACE-SSE-{scen.upper()}-{suffix}",
}
if appr_inst != "NONE":
    payload["approval_instruction"] = appr_inst
    payload["approver_reference"] = "demo-approver"

out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PYPAYLOAD

    echo "------------------------------------------------------------"
    echo "Streaming scenario=$scen approval_instruction=$appr_inst via POST /api/runs/stream"
    echo "------------------------------------------------------------"

    curl -N -sS \
        -X POST \
        -H "Content-Type: application/json" \
        -H "Accept: text/event-stream" \
        --data "@$PAYLOAD_FILE" \
        "${BACKEND_URL}/api/runs/stream" > "$STREAM_TMP"

    python3 - "$STREAM_TMP" "$scen" "$appr_inst" "$PROJECT_ROOT" <<'PYVALIDATESSE'
import json
from pathlib import Path
import sys

stream_path = Path(sys.argv[1])
scen = sys.argv[2]
appr_inst = sys.argv[3]
project_root = Path(sys.argv[4])

raw_text = stream_path.read_text(encoding="utf-8")
blocks = [b for b in raw_text.split("\n\n") if b.strip()]

events = []
for block in blocks:
    lines = block.splitlines()
    if all(line.startswith(":") for line in lines):
        continue
    data_lines = [line[6:] for line in lines if line.startswith("data: ")]
    if data_lines:
        events.append(json.loads("\n".join(data_lines)))

if not events:
    raise SystemExit("ERROR: No SSE events received in stream!")

sequences = [e["sequence"] for e in events]
expected_sequences = list(range(1, len(events) + 1))
if sequences != expected_sequences:
    raise SystemExit(f"ERROR: Non-monotonic sequence numbers: {sequences}")

run_ids = {e["run_id"] for e in events}
trace_ids = {e["trace_id"] for e in events}
if len(run_ids) != 1 or len(trace_ids) != 1:
    raise SystemExit(f"ERROR: Inconsistent correlation IDs across stream: run_ids={run_ids}, trace_ids={trace_ids}")

run_id = events[-1]["run_id"]
trace_id = events[-1]["trace_id"]
event_types = [e["event_type"] for e in events]

if events[-1]["event_type"] != "run_completed" or not events[-1]["terminal"]:
    raise SystemExit(f"ERROR: Stream did not end with terminal run_completed: last={events[-1]}")

if scen == "deny":
    required_subseq = ["governance_decided", "action_blocked", "verification_blocked", "run_completed"]
elif scen == "allow":
    required_subseq = ["governance_decided", "action_started", "action_executed", "verification_passed", "run_completed"]
elif scen == "pause" and appr_inst == "NONE":
    required_subseq = ["governance_decided", "approval_pending", "action_blocked", "run_completed"]
elif scen == "pause" and appr_inst == "APPROVED":
    required_subseq = ["governance_decided", "approval_approved", "action_started", "action_executed", "verification_passed", "run_completed"]
else:
    required_subseq = ["governance_decided", "approval_rejected", "action_blocked", "verification_blocked", "run_completed"]

idx = 0
for req_ev in required_subseq:
    while idx < len(event_types) and event_types[idx] != req_ev:
        idx += 1
    if idx >= len(event_types):
        raise SystemExit(f"ERROR: Missing required event '{req_ev}' in stream order {event_types}")
    idx += 1

out_dir = project_root / "evidence" / run_id
out_dir.mkdir(parents=True, exist_ok=True)
saved_sse = out_dir / "workflow-stream.sse"
saved_sse.write_text(raw_text, encoding="utf-8")

final_data = events[-1]["data"]
print(f"SSE_SCENARIO_PASSED: scenario={scen} approval_instruction={appr_inst}")
print(f"  run_id:              {run_id}")
print(f"  trace_id:            {trace_id}")
print(f"  event_count:         {len(events)}")
print(f"  event_order:         {event_types}")
print(f"  terminal_event:      {events[-1]['event_type']} (terminal={events[-1]['terminal']})")
print(f"  governance_decision: {final_data.get('governance_decision')}")
print(f"  approval_status:     {final_data.get('approval_status')}")
print(f"  execution_status:    {final_data.get('execution_status')}")
print(f"  verification_status: {final_data.get('verification_status')}")
print(f"  saved_stream_path:   {saved_sse}")
PYVALIDATESSE
}

run_sse_scenario "deny" "NONE"
run_sse_scenario "allow" "NONE"
run_sse_scenario "pause" "NONE"
run_sse_scenario "pause" "APPROVED"
run_sse_scenario "pause" "REJECTED"

echo "============================================================"
echo "ALL 5 LIVE AWS SSE WORKFLOW SCENARIOS PASSED"
echo "============================================================"
