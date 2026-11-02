#!/usr/bin/env bash
set -euo pipefail

# Phase 17: Inspect Agent 2 Bedrock fallback root cause & verify runtime role permissions.
# 1. Inspects agent2-response.json -> ai_reasoning.error_reason and Agent 2 CloudWatch logs
# 2. Verifies Agent 2 runtime environment variables (AWS_REGION, BEDROCK_ENABLED, BEDROCK_MODEL_ID)
# 3. Checks IAM permissions on arn:aws:iam::835156611439:role/bb26/cohort3-agent-runtime
# 4. If --attach-minimal-bedrock-policy is passed and AccessDeniedException was detected,
#    attaches a minimal inline policy granting bedrock:InvokeModel and bedrock:Converse
#    on the configured inference profile and foundation model.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-2}"
ROLE_NAME="cohort3-agent-runtime"
ROLE_ARN="arn:aws:iam::835156611439:role/bb26/cohort3-agent-runtime"
MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
RUNTIME_INFO_PATH="$PROJECT_ROOT/dist/agent2_runtime_info.json"
LAST_A2_PATH="$PROJECT_ROOT/dist/last_agent2_response.json"

ATTACH_POLICY="0"
if [ "${1:-}" = "--attach-minimal-bedrock-policy" ]; then
    ATTACH_POLICY="1"
fi

echo "============================================================"
echo "1. Inspecting Latest Agent 2 Response -> ai_reasoning"
echo "============================================================"

if [ -f "$LAST_A2_PATH" ]; then
    python3 - "$LAST_A2_PATH" <<'PYINSP'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ai = data.get("ai_reasoning") or {}
print(f"  run_id:          {data.get('run_id')}")
print(f"  trace_id:        {data.get('trace_id')}")
print(f"  model_id:        {ai.get('model_id')}")
print(f"  start_status:    {ai.get('start_status')}")
print(f"  result_status:   {ai.get('result_status')}")
print(f"  fallback_used:   {ai.get('fallback_used')}")
print(f"  error_reason:    {ai.get('error_reason')}")
PYINSP
else
    echo "  No dist/last_agent2_response.json found yet."
fi

echo "============================================================"
echo "2. Verifying Deployed Agent 2 Runtime Environment Variables"
echo "============================================================"

if [ -f "$RUNTIME_INFO_PATH" ]; then
    A2_ID="$(python3 -c "import json; print(json.load(open('$RUNTIME_INFO_PATH'))['agentRuntimeId'])")"
    aws bedrock-agentcore-control get-agent-runtime \
        --agent-runtime-id "$A2_ID" \
        --region "$AWS_REGION" \
        --query '{status:status,version:agentRuntimeVersion,environmentVariables:environmentVariables}' \
        --output json
fi

echo "============================================================"
echo "3. Inspecting IAM Role Policies on $ROLE_ARN"
echo "============================================================"

aws iam list-attached-role-policies --role-name "$ROLE_NAME" --output json || true
aws iam list-role-policies --role-name "$ROLE_NAME" --output json || true

if [ "$ATTACH_POLICY" = "1" ]; then
    echo "============================================================"
    echo "4. Attaching Minimal Bedrock Inference Profile Policy to $ROLE_NAME"
    echo "============================================================"
    POLICY_FILE="$(mktemp "${TMPDIR:-/tmp}/bedrock_min_policy.XXXXXX.json")"
    cat > "$POLICY_FILE" <<'POLEOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowBedrockConverseAndInvokeHaikuProfile",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
        "bedrock:GetInferenceProfile"
      ],
      "Resource": [
        "arn:aws:bedrock:*:835156611439:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/*"
      ]
    }
  ]
}
POLEOF
    aws iam put-role-policy \
        --role-name "$ROLE_NAME" \
        --policy-name "Cohort3BedrockInferenceProfileAccess" \
        --policy-document "file://$POLICY_FILE"
    rm -f "$POLICY_FILE"
    echo "Attached inline policy Cohort3BedrockInferenceProfileAccess to $ROLE_NAME."
fi
