#!/usr/bin/env bash
set -euo pipefail

# Declared Judging Evidence & Submission Automation Script
# Sequence:
#   DECLARE -> UPLOAD DECLARATION TO S3 -> VERIFY DECLARATION EXISTS
#   -> RUN REAL AWS AGENT 1 -> AGENT 2 -> VALIDATE CORRELATION & GOVERNANCE
#   -> COLLECT EVIDENCE -> VALIDATE LOCAL & CLOUDWATCH EVIDENCE
#   -> UPLOAD EVIDENCE TO S3 -> VERIFY S3 EVIDENCE -> PRINT FINAL SUMMARY
#
# Supported commands:
#   ./scripts/run_declared_scenario.sh --scenario deny
#   ./scripts/run_declared_scenario.sh --scenario allow
#   ./scripts/run_declared_scenario.sh --scenario pause
#   ./scripts/run_declared_scenario.sh --scenario pause --approve
#   ./scripts/run_declared_scenario.sh --scenario pause --reject
#   ./scripts/run_declared_scenario.sh --all-scenarios

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EXPECTED_ACCOUNT="835156611439"
AWS_REGION="${AWS_REGION:-us-east-2}"
SUBMISSION_BUCKET="cohort3-agentcore-835156611439"

SCENARIO=""
APPROVAL_INSTRUCTION="NONE"
RUN_ALL="0"

while [ $# -gt 0 ]; do
    case "$1" in
        --scenario)
            if [ $# -lt 2 ]; then
                echo "ERROR: --scenario requires a value (deny|allow|pause)" >&2
                exit 2
            fi
            SCENARIO="$(echo "$2" | tr '[:upper:]' '[:lower:]')"
            shift 2
            ;;
        --approve)
            APPROVAL_INSTRUCTION="APPROVED"
            shift
            ;;
        --reject)
            APPROVAL_INSTRUCTION="REJECTED"
            shift
            ;;
        --all-scenarios)
            RUN_ALL="1"
            shift
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

fail_stage() {
    local stage_name="$1"
    local reason="$2"
    echo "============================================================" >&2
    echo "FAILED STAGE: $stage_name" >&2
    echo "REASON:       $reason" >&2
    echo "DECLARED_RUN=FAILED" >&2
    echo "============================================================" >&2
    exit 1
}

validate_aws_identity() {
    local id_json
    if ! id_json="$(aws sts get-caller-identity --region "$AWS_REGION" --output json 2>/dev/null)"; then
        fail_stage "[1] AWS identity" "aws sts get-caller-identity failed in region $AWS_REGION"
    fi
    local acct
    acct="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("Account",""))' <<<"$id_json")"
    if [ "$acct" != "$EXPECTED_ACCOUNT" ]; then
        fail_stage "[1] AWS identity" "Account mismatch: expected $EXPECTED_ACCOUNT, got '${acct:-NONE}'"
    fi
    echo "AWS_IDENTITY_VALIDATION=PASSED"
}

run_single_declared_scenario() {
    local scen="$1"
    local appr="$2"

    case "$scen" in
        deny|allow|pause) ;;
        *)
            fail_stage "Argument validation" "Unsupported scenario '$scen' (expected deny, allow, or pause)"
            ;;
    esac

    if [ "$scen" != "pause" ] && [ "$appr" != "NONE" ]; then
        fail_stage "Argument validation" "--approve/--reject are only supported with --scenario pause"
    fi

    # Step 2: Generate unique RUN_ID and TRACE_ID before execution
    local suffix scen_upper
    suffix="$(python3 -c 'import secrets; print(secrets.token_hex(4).upper())')"
    scen_upper="$(echo "$scen" | tr '[:lower:]' '[:upper:]')"
    local run_id="WX-${scen_upper}-${suffix}"
    local trace_id="TRACE-${scen_upper}-${suffix}"

    echo "============================================================"
    echo "DECLARED JUDGING RUN"
    echo "============================================================"
    echo "Scenario: $scen"
    echo "Approval: $appr"
    echo "Run ID:   $run_id"
    echo "Trace ID: $trace_id"
    echo ""

    # Step 1: Validate AWS Identity
    validate_aws_identity
    printf "%-32s PASS\n" "[1] AWS identity"

    # Step 3: Create declaration before invoking Agent 1
    mkdir -p "$REPO_ROOT/declared"
    local decl_rel="declared/${run_id}.json"
    local decl_path="$REPO_ROOT/$decl_rel"

    if ! python3 - "$run_id" "$trace_id" "$scen" "$appr" "$decl_path" <<'PYDECL'
import json
from pathlib import Path
import sys

run_id, trace_id, scen, appr, out_file = sys.argv[1:6]

if scen == "allow":
    weather_event = "HIGH_WIND"
    severity = "LOW"
    affected_region = "Region-B"
elif scen == "deny":
    weather_event = "SEVERE_STORM"
    severity = "HIGH"
    affected_region = "Region-C"
else:
    weather_event = "SEVERE_STORM"
    severity = "HIGH"
    affected_region = "Demo-Region-A"

declaration = {
    "run_id": run_id,
    "trace_id": trace_id,
    "scenario": scen,
    "approval_instruction": appr,
    "weather_event": weather_event,
    "severity": severity,
    "affected_region": affected_region,
    "synthetic": True,
}

Path(out_file).write_text(json.dumps(declaration, indent=2) + "\n", encoding="utf-8")
PYDECL
    then
        fail_stage "[2] Declaration created" "Failed to write $decl_rel"
    fi
    printf "%-32s PASS\n" "[2] Declaration created"

    # Step 4: Validate declaration JSON
    if ! python3 -m json.tool "$decl_path" >/dev/null 2>&1; then
        fail_stage "[2] Declaration JSON validation" "Invalid JSON in $decl_rel"
    fi
    echo "DECLARATION_JSON_VALIDATION=PASSED"

    # Step 5: Upload declaration before run
    local decl_s3_uri="s3://${SUBMISSION_BUCKET}/${decl_rel}"
    if ! aws s3 cp "$decl_path" "$decl_s3_uri" --region "$AWS_REGION" >/dev/null; then
        fail_stage "[3] Declaration uploaded" "aws s3 cp failed for $decl_s3_uri"
    fi
    printf "%-32s PASS\n" "[3] Declaration uploaded"

    # Step 6: Prove declaration exists in S3 BEFORE Agent 1 invocation
    local head_json
    if ! head_json="$(aws s3api head-object --bucket "$SUBMISSION_BUCKET" --key "$decl_rel" --region "$AWS_REGION" --output json 2>/dev/null)"; then
        fail_stage "[4] Declaration S3 verified" "s3api head-object failed for s3://${SUBMISSION_BUCKET}/${decl_rel}"
    fi
    local decl_last_modified decl_etag
    decl_last_modified="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("LastModified",""))' <<<"$head_json")"
    decl_etag="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ETag",""))' <<<"$head_json")"
    echo "DECLARATION_S3_VERIFICATION=PASSED (LastModified=${decl_last_modified}, ETag=${decl_etag})"
    printf "%-32s PASS\n" "[4] Declaration S3 verified"

    # Step 7: Execute real AWS Agent 1 -> Agent 2 workflow
    local cmd_args=(--scenario "$scen" --run-id "$run_id" --trace-id "$trace_id")
    if [ "$appr" = "APPROVED" ]; then
        cmd_args+=(--approve)
    elif [ "$appr" = "REJECTED" ]; then
        cmd_args+=(--reject)
    fi

    if ! "$REPO_ROOT/scripts/test_agent1_to_agent2_aws.sh" "${cmd_args[@]}"; then
        fail_stage "[5/6] AWS Agent 1 -> Agent 2 workflow" "test_agent1_to_agent2_aws.sh failed for $run_id"
    fi
    printf "%-32s PASS\n" "[5] Agent 1 invocation"
    printf "%-32s PASS\n" "[6] Agent 2 invocation"

    # Step 8 & Step 9: Validate Correlation and Governance Result
    local run_cache_dir="$REPO_ROOT/dist/runs/$run_id"
    local a1_resp_file="$run_cache_dir/last_agent1_response.json"
    local a2_resp_file="$run_cache_dir/last_agent2_response.json"
    local meta_resp_file="$run_cache_dir/last_runtime_metadata.json"

    if [ ! -f "$a1_resp_file" ] || [ ! -f "$a2_resp_file" ]; then
        fail_stage "[7] Correlation" "Missing Agent 1 or Agent 2 response files in $run_cache_dir"
    fi

    local gov_tuple
    if ! gov_tuple="$(python3 - "$decl_path" "$a1_resp_file" "$a2_resp_file" "$scen" "$appr" <<'PYCORR'
import json
from pathlib import Path
import sys

decl = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
a1 = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
a2 = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
scen = sys.argv[4]
appr = sys.argv[5]

d_run = decl.get("run_id")
d_trace = decl.get("trace_id")
a1_run = a1.get("run_id")
a1_trace = a1.get("trace_id")
a2_run = a2.get("run_id")
a2_trace = a2.get("trace_id")

if a1_run != d_run or a1_trace != d_trace:
    raise SystemExit(f"Agent 1 mismatch vs declared: Agent1=({a1_run}, {a1_trace}) Declared=({d_run}, {d_trace})")
if a2_run != d_run or a2_trace != d_trace:
    raise SystemExit(f"Agent 2 mismatch vs declared: Agent2=({a2_run}, {a2_trace}) Declared=({d_run}, {d_trace})")
if a1_run != a2_run or a1_trace != a2_trace:
    raise SystemExit(f"Agent 1 vs Agent 2 mismatch: Agent1=({a1_run}, {a1_trace}) Agent2=({a2_run}, {a2_trace})")

gov = (a2.get("governance") or {}).get("decision")
apr = (a2.get("approval") or {}).get("status")
exe = (a2.get("execution") or {}).get("status")
ver = (a2.get("verification") or {}).get("status")

if scen == "deny":
    if (gov, apr, exe, ver) != ("DENY", "NOT_REQUIRED", "BLOCKED", "BLOCKED"):
        raise SystemExit(f"DENY validation failed: got ({gov}, {apr}, {exe}, {ver})")
elif scen == "allow":
    if (gov, apr, exe, ver) != ("ALLOW", "NOT_REQUIRED", "EXECUTED", "PASSED"):
        raise SystemExit(f"ALLOW validation failed: got ({gov}, {apr}, {exe}, {ver})")
elif scen == "pause" and appr == "NONE":
    if gov != "PAUSE" or apr != "PENDING" or exe != "BLOCKED" or ver not in ("NOT_STARTED", "BLOCKED"):
        raise SystemExit(f"PAUSE validation failed: got ({gov}, {apr}, {exe}, {ver})")
elif scen == "pause" and appr == "APPROVED":
    if (gov, apr, exe, ver) != ("PAUSE", "APPROVED", "EXECUTED", "PASSED"):
        raise SystemExit(f"PAUSE+APPROVED validation failed: got ({gov}, {apr}, {exe}, {ver})")
elif scen == "pause" and appr == "REJECTED":
    if (gov, apr, exe, ver) != ("PAUSE", "REJECTED", "BLOCKED", "BLOCKED"):
        raise SystemExit(f"PAUSE+REJECTED validation failed: got ({gov}, {apr}, {exe}, {ver})")

print(f"{gov}|{apr}|{exe}|{ver}")
PYCORR
)"; then
        fail_stage "[7/8] Correlation or Governance validation" "Validation failed for $run_id"
    fi

    echo "DECLARED_RUN_CORRELATION=PASSED"
    printf "%-32s PASS\n" "[7] Correlation"
    echo "SCENARIO_VALIDATION=PASSED"
    printf "%-32s PASS\n" "[8] Governance result"

    # Step 10: Collect Evidence
    if ! "$REPO_ROOT/scripts/collect_run_evidence.sh" --run-id "$run_id" --trace-id "$trace_id"; then
        fail_stage "[9] Evidence collection" "collect_run_evidence.sh failed for $run_id"
    fi
    printf "%-32s PASS\n" "[9] Evidence collection"

    # Step 11: Validate Local Evidence
    local ev_dir="$REPO_ROOT/evidence/$run_id"
    local required_files=(
        "agent1-response.json"
        "agent2-response.json"
        "cloudwatch-events.json"
        "evidence-index.json"
        "runtime-metadata.json"
        "workflow-summary.json"
    )
    for req_file in "${required_files[@]}"; do
        if [ ! -s "$ev_dir/$req_file" ]; then
            fail_stage "[9] Local evidence validation" "Missing or empty $ev_dir/$req_file"
        fi
        if ! python3 -m json.tool "$ev_dir/$req_file" >/dev/null 2>&1; then
            fail_stage "[9] Local evidence validation" "Invalid JSON in $ev_dir/$req_file"
        fi
    done
    echo "LOCAL_EVIDENCE_VALIDATION=PASSED"

    # Step 12: Verify CloudWatch Evidence
    local cw_counts
    if ! cw_counts="$(python3 - "$ev_dir/cloudwatch-events.json" "$ev_dir/runtime-metadata.json" "$run_id" "$trace_id" <<'PYCW'
import json
from pathlib import Path
import sys

cw_path = Path(sys.argv[1])
meta_path = Path(sys.argv[2])
run_id = sys.argv[3]
trace_id = sys.argv[4]

events = json.loads(cw_path.read_text(encoding="utf-8"))
meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

if not isinstance(events, list) or len(events) == 0:
    raise SystemExit("CloudWatch events total is 0")

a1_sess = str(meta.get("agent1_session_id") or "")
a2_sess = str(meta.get("agent2_session_id") or "")

correlated_total = 0
a1_count = 0
a2_count = 0

for ev in events:
    if not isinstance(ev, dict):
        continue
    raw_str = json.dumps(ev)
    is_corr = (
        (run_id and run_id in raw_str)
        or (trace_id and trace_id in raw_str)
        or (a1_sess and a1_sess in raw_str)
        or (a2_sess and a2_sess in raw_str)
    )
    if not is_corr:
        continue
    correlated_total += 1
    lg = str(ev.get("logGroupName", ""))
    msg = str(ev.get("message", ""))
    if "weather_network_intelligence" in lg or "weather_network_intelligence" in msg:
        a1_count += 1
    if "network_resilience" in lg or '"agent_name": "network_resilience"' in msg or '"agent_name":"network_resilience"' in msg:
        a2_count += 1

if correlated_total <= 0:
    raise SystemExit("No explicitly correlated CloudWatch events found")
if a1_count <= 0:
    raise SystemExit(f"Expected >=1 Agent 1 CloudWatch event, found {a1_count}")
if a2_count <= 0:
    raise SystemExit(f"Expected >=1 Agent 2 CloudWatch event, found {a2_count}")

print(f"{len(events)}|{a1_count}|{a2_count}")
PYCW
)"; then
        fail_stage "[10] CloudWatch correlation" "CloudWatch correlation failed for $run_id"
    fi

    local cw_total cw_a1 cw_a2
    IFS='|' read -r cw_total cw_a1 cw_a2 <<<"$cw_counts"
    echo "CLOUDWATCH_CORRELATION=PASSED"
    echo "CLOUDWATCH_TOTAL_EVENTS=$cw_total"
    echo "AGENT1_CLOUDWATCH_EVENTS=$cw_a1"
    echo "AGENT2_CLOUDWATCH_EVENTS=$cw_a2"
    printf "%-32s PASS\n" "[10] CloudWatch correlation"

    # Step 15 & Step 16: Save declaration copy in local evidence & create submission-summary.json
    cp "$decl_path" "$ev_dir/declared.json"

    if ! python3 - "$ev_dir/submission-summary.json" "$run_id" "$trace_id" "$scen" "$appr" "$SUBMISSION_BUCKET" "$gov_tuple" "$cw_total" "$cw_a1" "$cw_a2" "$ev_dir/runtime-metadata.json" "$decl_last_modified" <<'PYSUM'
import json
from pathlib import Path
import sys

(
    out_file,
    run_id,
    trace_id,
    scen,
    appr,
    bucket,
    gov_tuple,
    cw_total,
    cw_a1,
    cw_a2,
    meta_file,
    decl_ts,
) = sys.argv[1:13]

gov, apr_status, exe_status, ver_status = gov_tuple.split("|")
meta = json.loads(Path(meta_file).read_text(encoding="utf-8")) if Path(meta_file).exists() else {}

summary = {
    "run_id": run_id,
    "trace_id": trace_id,
    "scenario": scen,
    "approval_instruction": appr,
    "declaration_s3_uri": f"s3://{bucket}/declared/{run_id}.json",
    "declaration_s3_last_modified": decl_ts,
    "evidence_s3_prefix": f"s3://{bucket}/evidence/{run_id}/",
    "governance_decision": gov,
    "approval_status": apr_status,
    "execution_status": exe_status,
    "verification_status": ver_status,
    "cloudwatch_total_events": int(cw_total),
    "agent1_cloudwatch_events": int(cw_a1),
    "agent2_cloudwatch_events": int(cw_a2),
    "agent1_runtime_arn": meta.get(
        "agent1_runtime_arn",
        "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_weather_network_intelligence-aRoVQk71fj",
    ),
    "agent2_runtime_arn": meta.get(
        "agent2_runtime_arn",
        "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_network_resilience-ph5xzN4X1u",
    ),
    "synthetic": True,
}

Path(out_file).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
PYSUM
    then
        fail_stage "[11] Submission summary creation" "Failed to create submission-summary.json"
    fi
    python3 -m json.tool "$ev_dir/submission-summary.json" >/dev/null

    # Step 13: Mirror Evidence to S3
    local ev_s3_prefix="s3://${SUBMISSION_BUCKET}/evidence/${run_id}/"
    if ! aws s3 sync "$ev_dir/" "$ev_s3_prefix" --region "$AWS_REGION" >/dev/null; then
        fail_stage "[11] Evidence S3 upload" "aws s3 sync failed for $ev_s3_prefix"
    fi
    printf "%-32s PASS\n" "[11] Evidence S3 upload"

    # Step 14: Verify S3 Evidence
    local verify_s3_files=(
        "agent1-response.json"
        "agent2-response.json"
        "cloudwatch-events.json"
        "evidence-index.json"
        "runtime-metadata.json"
        "workflow-summary.json"
        "declared.json"
        "submission-summary.json"
    )
    if [ -f "$ev_dir/workflow-stream.sse" ]; then
        verify_s3_files+=("workflow-stream.sse")
    fi

    for s3_file in "${verify_s3_files[@]}"; do
        if ! aws s3api head-object --bucket "$SUBMISSION_BUCKET" --key "evidence/${run_id}/${s3_file}" --region "$AWS_REGION" >/dev/null 2>&1; then
            fail_stage "[12] Evidence S3 verification" "Missing S3 object s3://${SUBMISSION_BUCKET}/evidence/${run_id}/${s3_file}"
        fi
    done

    echo "EVIDENCE_S3_VERIFICATION=PASSED"
    printf "%-32s PASS\n" "[12] Evidence S3 verification"
    echo ""
    echo "DECLARED_RUN=PASSED"
    echo "============================================================"

    LAST_DECLARED_RUN_ID="$run_id"
    LAST_DECLARED_TRACE_ID="$trace_id"
}

LAST_DECLARED_RUN_ID=""
LAST_DECLARED_TRACE_ID=""

if [ "$RUN_ALL" = "1" ]; then
    if [ -n "$SCENARIO" ] || [ "$APPROVAL_INSTRUCTION" != "NONE" ]; then
        fail_stage "Argument validation" "--all-scenarios cannot be combined with --scenario, --approve, or --reject"
    fi

    ALL_RUN_RECORDS=()

    # 1. deny
    run_single_declared_scenario "deny" "NONE"
    ALL_RUN_RECORDS+=("deny|NONE|$LAST_DECLARED_RUN_ID|$LAST_DECLARED_TRACE_ID")

    # 2. allow
    run_single_declared_scenario "allow" "NONE"
    ALL_RUN_RECORDS+=("allow|NONE|$LAST_DECLARED_RUN_ID|$LAST_DECLARED_TRACE_ID")

    # 3. pause
    run_single_declared_scenario "pause" "NONE"
    ALL_RUN_RECORDS+=("pause|NONE|$LAST_DECLARED_RUN_ID|$LAST_DECLARED_TRACE_ID")

    # 4. pause + APPROVED
    run_single_declared_scenario "pause" "APPROVED"
    ALL_RUN_RECORDS+=("pause|APPROVED|$LAST_DECLARED_RUN_ID|$LAST_DECLARED_TRACE_ID")

    # 5. pause + REJECTED
    run_single_declared_scenario "pause" "REJECTED"
    ALL_RUN_RECORDS+=("pause|REJECTED|$LAST_DECLARED_RUN_ID|$LAST_DECLARED_TRACE_ID")

    # Step 18: Final Manifest (evidence/submission-runs.json)
    mkdir -p "$REPO_ROOT/evidence"
    MANIFEST_PATH="$REPO_ROOT/evidence/submission-runs.json"

    python3 - "$MANIFEST_PATH" "$SUBMISSION_BUCKET" "${ALL_RUN_RECORDS[@]}" <<'PYMANIFEST'
import json
from pathlib import Path
import sys

out_path = Path(sys.argv[1])
bucket = sys.argv[2]
records = sys.argv[3:]

runs = []
for rec in records:
    scen, appr, r_id, t_id = rec.split("|")
    entry = {
        "scenario": scen,
        "approval_instruction": appr,
        "run_id": r_id,
        "trace_id": t_id,
        "declaration": f"s3://{bucket}/declared/{r_id}.json",
        "evidence": f"s3://{bucket}/evidence/{r_id}/",
    }
    runs.append(entry)

if len(runs) != 5:
    raise SystemExit(f"Expected 5 runs in submission-runs.json, got {len(runs)}")

out_path.write_text(json.dumps({"runs": runs}, indent=2) + "\n", encoding="utf-8")
PYMANIFEST

    python3 -m json.tool "$MANIFEST_PATH" >/dev/null
    aws s3 cp "$MANIFEST_PATH" "s3://${SUBMISSION_BUCKET}/evidence/submission-runs.json" --region "$AWS_REGION" >/dev/null
    aws s3api head-object --bucket "$SUBMISSION_BUCKET" --key "evidence/submission-runs.json" --region "$AWS_REGION" >/dev/null

    # Step 20: Final S3 Validation & Condensed Inventory
    echo "============================================================"
    echo "FINAL 5-SCENARIO SUBMISSION S3 VALIDATION"
    echo "============================================================"
    echo "--- S3 Declared Objects (s3://${SUBMISSION_BUCKET}/declared/) ---"
    for rec in "${ALL_RUN_RECORDS[@]}"; do
        IFS='|' read -r s_name s_appr r_id t_id <<<"$rec"
        aws s3api head-object --bucket "$SUBMISSION_BUCKET" --key "declared/${r_id}.json" --region "$AWS_REGION" >/dev/null
        echo "  [OK] s3://${SUBMISSION_BUCKET}/declared/${r_id}.json (${s_name}, approval=${s_appr})"
    done

    echo "--- S3 Evidence Prefixes (s3://${SUBMISSION_BUCKET}/evidence/) ---"
    for rec in "${ALL_RUN_RECORDS[@]}"; do
        IFS='|' read -r s_name s_appr r_id t_id <<<"$rec"
        aws s3api head-object --bucket "$SUBMISSION_BUCKET" --key "evidence/${r_id}/submission-summary.json" --region "$AWS_REGION" >/dev/null
        echo "  [OK] s3://${SUBMISSION_BUCKET}/evidence/${r_id}/ (${s_name}, approval=${s_appr})"
    done

    echo "--- Final Manifest ---"
    echo "  [OK] s3://${SUBMISSION_BUCKET}/evidence/submission-runs.json"
    echo "============================================================"
    echo "ALL_DECLARED_SCENARIOS=PASSED"
    echo "============================================================"
else
    if [ -z "$SCENARIO" ]; then
        fail_stage "Argument validation" "Specify --scenario <deny|allow|pause> or --all-scenarios"
    fi
    run_single_declared_scenario "$SCENARIO" "$APPROVAL_INSTRUCTION"
fi
