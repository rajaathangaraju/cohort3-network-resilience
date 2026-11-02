#!/usr/bin/env bash
set -euo pipefail

# Deploy the Streamlit Command Center UI: bootstrap -> build -> restart -> status -> test.
# Start/stop logic lives only in streamlit.sh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run_step() {
    local name="$1"
    shift
    printf '\n######## %s ########\n' "$name"
    if "$@"; then
        return 0
    else
        local rc=$?
        printf 'STREAMLIT_DEPLOYMENT=FAILED (step: %s, exit code: %s)\n' "$name" "$rc"
        exit "$rc"
    fi
}

run_step "bootstrap" bash "$SCRIPT_DIR/bootstrap_streamlit.sh"
run_step "build" bash "$SCRIPT_DIR/build_streamlit.sh"
run_step "restart" bash "$SCRIPT_DIR/streamlit.sh" restart
run_step "status" bash "$SCRIPT_DIR/streamlit.sh" status
run_step "test" bash "$SCRIPT_DIR/streamlit.sh" test

printf '\nSTREAMLIT_DEPLOYMENT=PASSED\n'
