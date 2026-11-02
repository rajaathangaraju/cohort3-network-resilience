#!/usr/bin/env bash
set -euo pipefail

# Controlled deployment update script for EXISTING Agent 1 Amazon Bedrock AgentCore Runtime.
# Updates ONLY the existing runtime: cohort3_weather_network_intelligence-aRoVQk71fj
# Never creates a new runtime, IAM role, or S3 bucket.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-2}"
EXPECTED_ACCOUNT_ID="835156611439"
S3_BUCKET="cohort3-agentcore-835156611439"
S3_KEY="agentcore/agent1/agent1_weather_network_intelligence.zip"
RUNTIME_ID="cohort3_weather_network_intelligence-aRoVQk71fj"
RUNTIME_ROLE_ARN="arn:aws:iam::835156611439:role/bb26/cohort3-agent-runtime"
RUNTIME_PYTHON="PYTHON_3_13"
ENTRYPOINT_REL="agents/weather_network_intelligence/agentcore_app.py"
ZIP_PATH="$PROJECT_ROOT/dist/agent1_weather_network_intelligence.zip"

REQ_JSON="$(mktemp "${TMPDIR:-/tmp}/update_agent1_runtime.XXXXXX.json")"
RESP_JSON="$(mktemp "${TMPDIR:-/tmp}/update_agent1_response.XXXXXX.json")"
STATUS_JSON="$(mktemp "${TMPDIR:-/tmp}/get_agent1_runtime.XXXXXX.json")"

trap 'rm -f "$REQ_JSON" "$RESP_JSON" "$STATUS_JSON"' EXIT

echo "============================================================"
echo "1. Validating Environment & AWS Caller Identity"
echo "============================================================"

if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: aws CLI is not installed or not in PATH." >&2
    exit 1
fi

CALLER_ACCOUNT="$(aws sts get-caller-identity --region "$AWS_REGION" --query 'Account' --output text)"
if [ "$CALLER_ACCOUNT" != "$EXPECTED_ACCOUNT_ID" ]; then
    echo "ERROR: Authenticated AWS account ($CALLER_ACCOUNT) does not match expected account ($EXPECTED_ACCOUNT_ID)." >&2
    exit 1
fi
echo "Authenticated AWS Account: $CALLER_ACCOUNT | Region: $AWS_REGION"

echo "============================================================"
echo "2. Validating CodeZip Artifact & ARM64 Architecture"
echo "============================================================"

if [ ! -f "$ZIP_PATH" ]; then
    echo "ERROR: ZIP artifact not found at $ZIP_PATH. Run ./scripts/package_agent1.sh first." >&2
    exit 1
fi

python3 - "$ZIP_PATH" "$ENTRYPOINT_REL" <<'PYVALIDATE'
from pathlib import Path
import struct
import sys
import zipfile

zip_path = Path(sys.argv[1])
entrypoint_rel = sys.argv[2]

EM_AARCH64 = 183

with zipfile.ZipFile(zip_path, "r") as zf:
    names = zf.namelist()
    if entrypoint_rel not in names:
        raise SystemExit(f"ERROR: Missing entrypoint {entrypoint_rel} inside {zip_path}")

    ep_source = zf.read(entrypoint_rel).decode("utf-8")
    if "from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent" not in ep_source:
        raise SystemExit("ERROR: Packaged entrypoint is missing absolute WeatherNetworkIntelligenceAgent import")
    if "from .agent import WeatherNetworkIntelligenceAgent" in ep_source:
        raise SystemExit("ERROR: Packaged entrypoint still contains relative import")

    so_names = [n for n in names if n.endswith(".so")]
    if not so_names:
        raise SystemExit("ERROR: No native .so libraries found in ZIP archive; expected ARM64 wheels.")

    for so_name in so_names:
        data = zf.read(so_name)
        if len(data) < 20 or data[:4] != b"\x7fELF":
            raise SystemExit(f"ERROR: {so_name} is not a valid ELF shared library")
        e_machine = struct.unpack("<H", data[18:20])[0]
        if e_machine != EM_AARCH64:
            raise SystemExit(f"ERROR: Non-ARM64 shared library in ZIP ({so_name}: e_machine={e_machine})")

print(f"ZIP_VALIDATION=PASSED (size={zip_path.stat().st_size} bytes, {len(so_names)} ARM64 .so files verified)")
PYVALIDATE

echo "============================================================"
echo "3. Uploading CodeZip Artifact to S3"
echo "============================================================"
echo "Target S3 URI: s3://$S3_BUCKET/$S3_KEY"

aws s3 cp "$ZIP_PATH" "s3://$S3_BUCKET/$S3_KEY" --region "$AWS_REGION"

echo "============================================================"
echo "4. Generating & Validating update-agent-runtime Request JSON"
echo "============================================================"

python3 - "$REQ_JSON" "$RUNTIME_ID" "$S3_BUCKET" "$S3_KEY" "$RUNTIME_PYTHON" "$ENTRYPOINT_REL" "$RUNTIME_ROLE_ARN" <<'PYJSON'
import json
from pathlib import Path
import sys

out_path = Path(sys.argv[1])
payload = {
    "agentRuntimeId": sys.argv[2],
    "agentRuntimeArtifact": {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": sys.argv[3],
                    "prefix": sys.argv[4],
                }
            },
            "runtime": sys.argv[5],
            "entryPoint": [
                sys.argv[6],
            ],
        }
    },
    "roleArn": sys.argv[7],
    "networkConfiguration": {
        "networkMode": "PUBLIC",
    },
    "environmentVariables": {
        "AWS_REGION": "us-east-2",
        "PYTHONUNBUFFERED": "1",
    },
    "description": "Cohort 3 Weather and Network Intelligence Agent - Python 3.13 ARM64",
}
text = json.dumps(payload, indent=2)
# Validate JSON round-trip
parsed = json.loads(text)
assert parsed["agentRuntimeId"] == sys.argv[2]
assert parsed["agentRuntimeArtifact"]["codeConfiguration"]["runtime"] == "PYTHON_3_13"
out_path.write_text(text + "\n", encoding="utf-8")
print(text)
PYJSON

echo "============================================================"
echo "5. Updating Existing AgentCore Runtime ($RUNTIME_ID)"
echo "============================================================"

aws bedrock-agentcore-control update-agent-runtime \
    --region "$AWS_REGION" \
    --cli-input-json "file://$REQ_JSON" > "$RESP_JSON"

cat "$RESP_JSON"

echo "============================================================"
echo "6. Polling AgentCore Runtime Status Until READY"
echo "============================================================"

MAX_ATTEMPTS=60
SLEEP_SECONDS=5
ATTEMPT=1

while [ "$ATTEMPT" -le "$MAX_ATTEMPTS" ]; do
    aws bedrock-agentcore-control get-agent-runtime \
        --region "$AWS_REGION" \
        --agent-runtime-id "$RUNTIME_ID" > "$STATUS_JSON"

    READ_OUT="$(python3 - "$STATUS_JSON" <<'PYSTATUS'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = data.get("status", "UNKNOWN")
version = data.get("agentRuntimeVersion", "UNKNOWN")
failure = data.get("failureReason") or ""
arn = data.get("agentRuntimeArn", "")
print(f"{status}|{version}|{arn}|{failure}")
PYSTATUS
)"
    IFS='|' read -r CURRENT_STATUS CURRENT_VERSION CURRENT_ARN FAILURE_REASON <<< "$READ_OUT"
    echo "[Attempt $ATTEMPT/$MAX_ATTEMPTS] status=$CURRENT_STATUS version=$CURRENT_VERSION"

    case "$CURRENT_STATUS" in
        READY)
            echo "============================================================"
            echo "AGENT 1 RUNTIME UPDATE SUCCEEDED"
            echo "RUNTIME_ID=$RUNTIME_ID"
            echo "RUNTIME_ARN=$CURRENT_ARN"
            echo "RUNTIME_STATUS=$CURRENT_STATUS"
            echo "RUNTIME_VERSION=$CURRENT_VERSION"
            echo "S3_ARTIFACT=s3://$S3_BUCKET/$S3_KEY"
            echo "============================================================"
            exit 0
            ;;
        CREATE_FAILED|UPDATE_FAILED|FAILED)
            echo "ERROR: AgentCore runtime update failed with status=$CURRENT_STATUS" >&2
            if [ -n "$FAILURE_REASON" ]; then
                echo "FAILURE_REASON=$FAILURE_REASON" >&2
            fi
            cat "$STATUS_JSON" >&2
            exit 1
            ;;
        UPDATING|CREATING|*)
            sleep "$SLEEP_SECONDS"
            ATTEMPT=$((ATTEMPT + 1))
            ;;
    esac
done

echo "ERROR: Timed out waiting for AgentCore runtime $RUNTIME_ID to reach READY." >&2
cat "$STATUS_JSON" >&2
exit 1
