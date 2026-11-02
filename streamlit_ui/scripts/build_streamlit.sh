#!/usr/bin/env bash
set -euo pipefail

# Validate the Streamlit Command Center UI build without starting it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAM_UI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$STREAM_UI_DIR/.." && pwd)"
STREAM_UI_NAME="$(basename "$STREAM_UI_DIR")"

VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"
REQUIREMENTS_FILE="$STREAM_UI_DIR/requirements.txt"
STREAMLIT_ENTRYPOINT="$STREAM_UI_DIR/dashboard.py"

trap 'printf "STREAMLIT_BUILD_VALIDATION=FAILED (line %s)\n" "$LINENO" >&2' ERR

fail() {
    trap - ERR
    printf 'ERROR: %s\n' "$*" >&2
    printf 'STREAMLIT_BUILD_VALIDATION=FAILED\n'
    exit 1
}

step() {
    printf '\n===== %s =====\n' "$1"
}

if [[ -f "$VENV_DIR/bin/activate" ]]; then
    ACTIVATE_SCRIPT="$VENV_DIR/bin/activate"
elif [[ -f "$VENV_DIR/Scripts/activate" ]]; then
    ACTIVATE_SCRIPT="$VENV_DIR/Scripts/activate"
else
    fail "Virtual environment not found at $VENV_DIR. Run $SCRIPT_DIR/bootstrap_streamlit.sh first."
fi

step "Activate repository virtual environment"
set +u
# shellcheck disable=SC1090
source "$ACTIVATE_SCRIPT"
set -u
PYTHON_BIN="$(command -v python)"
echo "VIRTUAL_ENV=${VIRTUAL_ENV:-$VENV_DIR}"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

step "Verify Python 3.13"
PYTHON_VERSION="$(python -c 'import platform; print(platform.python_version())')"
python -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 13) else 1)' \
    || fail "Python $PYTHON_VERSION at $PYTHON_BIN; Python 3.13+ is required."
echo "PYTHON_VERSION=$PYTHON_VERSION"
echo "BUILD_PYTHON=PASSED"

step "Verify Streamlit"
command -v streamlit >/dev/null 2>&1 || fail "streamlit executable not found in $VENV_DIR."
MIN_STREAMLIT_VERSION="$(sed -n 's/^streamlit>=\([0-9.]*\).*/\1/p' "$REQUIREMENTS_FILE" | head -n 1)"
STREAMLIT_VERSION="$(python - "${MIN_STREAMLIT_VERSION:-0}" <<'PY'
import sys

import streamlit

def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3] if part.isdigit())

if as_tuple(streamlit.__version__) < as_tuple(sys.argv[1]):
    sys.exit(f"Streamlit {streamlit.__version__} is older than the required {sys.argv[1]}")
print(streamlit.__version__)
PY
)" || fail "Streamlit version check failed."
echo "STREAMLIT_VERSION=$STREAMLIT_VERSION"
echo "BUILD_STREAMLIT=PASSED"

step "Compile Streamlit source"
[[ -f "$STREAMLIT_ENTRYPOINT" ]] || fail "Streamlit entrypoint not found at $STREAMLIT_ENTRYPOINT."
python -m compileall -q -x '[/\\](runtime|logs)[/\\]' "$STREAM_UI_DIR" || fail "Streamlit source failed to compile."
echo "BUILD_COMPILE=PASSED"

step "Run Streamlit tests"
python -c 'import pytest' >/dev/null 2>&1 || fail "pytest is not installed in $VENV_DIR. Run $SCRIPT_DIR/bootstrap_streamlit.sh."
mapfile -t STREAMLIT_TESTS < <(find "$REPO_ROOT/tests" -type f -name 'test_streamlit*.py' 2>/dev/null | sort)
((${#STREAMLIT_TESTS[@]} > 0)) || fail "No Streamlit tests (tests/**/test_streamlit*.py) found."
python -m pytest -q -p no:cacheprovider "${STREAMLIT_TESTS[@]}" || fail "Streamlit tests failed."
echo "BUILD_TESTS=PASSED"

step "Verify backend API client"
python - "$STREAM_UI_NAME" <<'PY' || fail "Backend API client verification failed."
import importlib
import sys

client = importlib.import_module(f"{sys.argv[1]}.api_client")
cfg = client.load_ui_config({})
assert cfg.api_base_url == "http://127.0.0.1:8000", cfg
assert cfg.data_mode == "aws_live", cfg
api = client.RunApiClient("http://127.0.0.1:8000/")
assert api.base_url == "http://127.0.0.1:8000"
assert client.build_api_url(api.base_url, "/api/runs/WX-1/evidence", {"trace_id": "T-1"}) == (
    "http://127.0.0.1:8000/api/runs/WX-1/evidence?trace_id=T-1"
)
for method in ("health_check", "create_run", "get_run", "approve_run", "reject_run",
               "get_evidence", "stream_run", "replay_run_stream"):
    assert callable(getattr(api, method)), method
assert client.is_approval_actionable("PAUSE", "PENDING")
assert not client.is_approval_actionable("ALLOW", "NOT_REQUIRED")
print("RunApiClient endpoints and config verified (no network calls)")
PY
echo "BUILD_API_CLIENT=PASSED"

step "Verify SSE parser"
python - "$STREAM_UI_NAME" <<'PY' || fail "SSE parser verification failed."
import importlib
import sys

client = importlib.import_module(f"{sys.argv[1]}.api_client")
wire = [
    ": heartbeat", "",
    "id: EVT-1", "event: run_started", 'data: {"sequence": 1, "run_id": "WX-B",', 'data: "trace_id": "T-B"}', "",
    "id: EVT-2", "event: approval_pending", 'data: {"sequence": 2, "run_id": "WX-B", "status": "PAUSED"}', "",
]
events = list(client.iter_sse_events_from_lines(wire))
assert [e["event_type"] for e in events] == ["run_started", "approval_pending"], events
seen: set[str] = set()
last = 0
accepted = []
for event in events + events:
    ok, last = client.should_accept_sse_event(event, seen_event_ids=seen, last_sequence=last)
    if ok:
        accepted.append(event["event_id"])
assert accepted == ["EVT-1", "EVT-2"], accepted
assert client.map_event_to_high_level_stage("approval_pending") == "ACTION"
print("SSE frames parsed, heartbeat skipped, duplicates rejected")
PY
echo "BUILD_SSE_PARSER=PASSED"

step "Credential pattern scan"
SCAN_EXCLUDES=(--exclude-dir=runtime --exclude-dir=logs --exclude-dir=__pycache__ --exclude-dir=.venv)
FINDINGS="$(
    cd "$STREAM_UI_DIR"
    {
        grep -rIEno "${SCAN_EXCLUDES[@]}" \
            -e 'AKIA[0-9A-Z]{16}' \
            -e 'ASIA[0-9A-Z]{16}' \
            -e '-----BEGIN ([A-Z]+ )*PRIVATE KEY-----' \
            . || true
        grep -rIEnoi "${SCAN_EXCLUDES[@]}" \
            -e 'aws_secret_access_key[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9/+=]{40}' \
            -e 'aws_session_token[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9/+=]{100,}' \
            -e '(password|passwd|secret|api[_-]?key|access[_-]?token)[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"'[:space:]]{12,}["'"'"']' \
            . || true
    } | cut -d: -f1,2 | sort -u
)"
if [[ -n "$FINDINGS" ]]; then
    echo "Potential credentials found (file:line, values not printed):" >&2
    printf '%s\n' "$FINDINGS" | sed 's/^/  /' >&2
    fail "Credential pattern scan found matches in $STREAM_UI_DIR."
fi
echo "Scanned $STREAM_UI_DIR for AWS keys, private keys, and hardcoded secrets: no matches."
echo "BUILD_CREDENTIAL_SCAN=PASSED"

echo
echo "STREAMLIT_BUILD_VALIDATION=PASSED"
