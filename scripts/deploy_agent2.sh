#!/usr/bin/env bash
set -euo pipefail

# Controlled deployment script for Agent 2 (Network Resilience & Governed Action Agent)
# on Amazon Bedrock AgentCore Runtime (us-east-2).
# - Searches existing AgentCore runtimes for cohort3_network_resilience first
# - Uses update-agent-runtime if it already exists, or create-agent-runtime if not
# - Never creates duplicate runtimes, IAM roles, or S3 buckets
# - Polls until status reaches READY and saves runtime metadata to dist/agent2_runtime_info.json

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-2}"
EXPECTED_ACCOUNT_ID="835156611439"
S3_BUCKET="cohort3-agentcore-835156611439"
S3_KEY="agentcore/agent2/agent2_network_resilience.zip"
RUNTIME_NAME="${AGENT2_RUNTIME_NAME:-cohort3_network_resilience}"
RUNTIME_ROLE_ARN="arn:aws:iam::835156611439:role/bb26/cohort3-agent-runtime"
RUNTIME_PYTHON="PYTHON_3_13"
ENTRYPOINT_REL="agents/network_resilience/agentcore_app.py"
ZIP_PATH="$PROJECT_ROOT/dist/agent2_network_resilience.zip"
RUNTIME_INFO_PATH="$PROJECT_ROOT/dist/agent2_runtime_info.json"

REQ_JSON="$(mktemp "${TMPDIR:-/tmp}/agent2_runtime_req.XXXXXX.json")"
RESP_JSON="$(mktemp "${TMPDIR:-/tmp}/agent2_runtime_resp.XXXXXX.json")"
LIST_JSON="$(mktemp "${TMPDIR:-/tmp}/agent2_runtime_list.XXXXXX.json")"
STATUS_JSON="$(mktemp "${TMPDIR:-/tmp}/agent2_runtime_status.XXXXXX.json")"

trap 'rm -f "$REQ_JSON" "$RESP_JSON" "$LIST_JSON" "$STATUS_JSON"' EXIT

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
echo "2. Validating Agent 2 CodeZip Artifact & ARM64 Architecture"
echo "============================================================"

if [ ! -f "$ZIP_PATH" ]; then
    echo "ERROR: ZIP artifact not found at $ZIP_PATH. Run ./scripts/package_agent2.sh first." >&2
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
    if "from agents.network_resilience.agent import NetworkResilienceAgent" not in ep_source:
        raise SystemExit("ERROR: Packaged entrypoint is missing absolute NetworkResilienceAgent import")
    if "from .agent import NetworkResilienceAgent" in ep_source:
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
echo "3. Uploading Agent 2 CodeZip Artifact to S3"
echo "============================================================"
echo "Target S3 URI: s3://$S3_BUCKET/$S3_KEY"

aws s3 cp "$ZIP_PATH" "s3://$S3_BUCKET/$S3_KEY" --region "$AWS_REGION"

echo "============================================================"
echo "4. Searching Existing AgentCore Runtimes for '$RUNTIME_NAME'"
echo "============================================================"

aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" > "$LIST_JSON"

EXISTING_RUNTIME_ID="$(python3 - "$LIST_JSON" "$RUNTIME_NAME" <<'PYFIND'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
target_name = sys.argv[2]
for rt in data.get("agentRuntimes", []):
    if isinstance(rt, dict) and rt.get("agentRuntimeName") == target_name:
        print(rt.get("agentRuntimeId", ""))
        break
PYFIND
)"

if [ -n "$EXISTING_RUNTIME_ID" ]; then
    echo "Found existing Agent 2 runtime: $EXISTING_RUNTIME_ID (will use update-agent-runtime)"
    OPERATION="update"
else
    echo "No existing runtime named '$RUNTIME_NAME' found (will use create-agent-runtime)"
    OPERATION="create"
fi

echo "============================================================"
echo "5. Generating & Validating AgentCore Request JSON ($OPERATION)"
echo "============================================================"

python3 - "$REQ_JSON" "$OPERATION" "$RUNTIME_NAME" "$EXISTING_RUNTIME_ID" "$S3_BUCKET" "$S3_KEY" "$RUNTIME_PYTHON" "$ENTRYPOINT_REL" "$RUNTIME_ROLE_ARN" <<'PYJSON'
import json
from pathlib import Path
import sys

out_path = Path(sys.argv[1])
op = sys.argv[2]
runtime_name = sys.argv[3]
runtime_id = sys.argv[4]
s3_bucket = sys.argv[5]
s3_key = sys.argv[6]
runtime_python = sys.argv[7]
entrypoint_rel = sys.argv[8]
role_arn = sys.argv[9]

payload = {
    "agentRuntimeArtifact": {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": s3_bucket,
                    "prefix": s3_key,
                }
            },
            "runtime": runtime_python,
            "entryPoint": [
                entrypoint_rel,
            ],
        }
    },
    "roleArn": role_arn,
    "networkConfiguration": {
        "networkMode": "PUBLIC",
    },
    "environmentVariables": {
        "AWS_REGION": "us-east-2",
        "BEDROCK_ENABLED": "true",
        "BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "PYTHONUNBUFFERED": "1",
    },
    "description": "Cohort 3 Network Resilience and Governed Action Agent - Python 3.13 ARM64",
}

if op == "update":
    payload["agentRuntimeId"] = runtime_id
else:
    payload["agentRuntimeName"] = runtime_name

text = json.dumps(payload, indent=2)
parsed = json.loads(text)
assert parsed["agentRuntimeArtifact"]["codeConfiguration"]["runtime"] == "PYTHON_3_13"
out_path.write_text(text + "\n", encoding="utf-8")
print(text)
PYJSON

echo "============================================================"
echo "6. Executing AgentCore $OPERATION-agent-runtime"
echo "============================================================"

if [ "$OPERATION" = "update" ]; then
    aws bedrock-agentcore-control update-agent-runtime \
        --region "$AWS_REGION" \
        --cli-input-json "file://$REQ_JSON" > "$RESP_JSON"
else
    aws bedrock-agentcore-control create-agent-runtime \
        --region "$AWS_REGION" \
        --cli-input-json "file://$REQ_JSON" > "$RESP_JSON"
fi

cat "$RESP_JSON"

RUNTIME_ID="$(python3 - "$RESP_JSON" "$EXISTING_RUNTIME_ID" <<'PYID'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("agentRuntimeId") or sys.argv[2])
PYID
)"

if [ -z "$RUNTIME_ID" ]; then
    echo "ERROR: Could not determine agentRuntimeId from response." >&2
    exit 1
fi

echo "============================================================"
echo "7. Polling AgentCore Runtime ($RUNTIME_ID) Until READY"
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
            LOG_GROUP="/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"
            python3 - "$RUNTIME_INFO_PATH" "$RUNTIME_ID" "$CURRENT_ARN" "$CURRENT_VERSION" "$CURRENT_STATUS" "$S3_BUCKET" "$S3_KEY" "$LOG_GROUP" <<'PYSAVE'
import json
from pathlib import Path
import sys

info = {
    "agentRuntimeName": "cohort3_network_resilience",
    "agentRuntimeId": sys.argv[2],
    "agentRuntimeArn": sys.argv[3],
    "agentRuntimeVersion": sys.argv[4],
    "status": sys.argv[5],
    "s3Bucket": sys.argv[6],
    "s3Key": sys.argv[7],
    "logGroup": sys.argv[8],
}
Path(sys.argv[1]).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
PYSAVE
            echo "============================================================"
            echo "AGENT 2 RUNTIME DEPLOYMENT SUCCEEDED"
            echo "RUNTIME_ID=$RUNTIME_ID"
            echo "RUNTIME_ARN=$CURRENT_ARN"
            echo "RUNTIME_STATUS=$CURRENT_STATUS"
            echo "RUNTIME_VERSION=$CURRENT_VERSION"
            echo "S3_ARTIFACT=s3://$S3_BUCKET/$S3_KEY"
            echo "CLOUDWATCH_LOG_GROUP=$LOG_GROUP"
            echo "Saved metadata to: $RUNTIME_INFO_PATH"
            echo "============================================================"
            exit 0
            ;;
        CREATE_FAILED|UPDATE_FAILED|FAILED)
            echo "ERROR: Agent 2 AgentCore runtime failed with status=$CURRENT_STATUS" >&2
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

echo "ERROR: Timed out waiting for Agent 2 runtime $RUNTIME_ID to reach READY." >&2
cat "$STATUS_JSON" >&2
exit 1
