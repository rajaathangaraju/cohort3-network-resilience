#!/usr/bin/env bash
set -euo pipefail

# Single management entrypoint for the Network Resilience Command Center Streamlit UI.
# Usage: streamlit.sh {start|stop|restart|status|logs [-f]|test|help}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAM_UI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$STREAM_UI_DIR/.." && pwd)"
STREAM_UI_NAME="$(basename "$STREAM_UI_DIR")"

STREAMLIT_ENTRYPOINT="$STREAM_UI_DIR/dashboard.py"
RUNTIME_DIR="$STREAM_UI_DIR/runtime"
LOG_DIR="$STREAM_UI_DIR/logs"
PID_FILE="$RUNTIME_DIR/streamlit.pid"
LOG_FILE="$LOG_DIR/streamlit.log"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"

export COMMAND_CENTER_API_BASE_URL="${COMMAND_CENTER_API_BASE_URL:-http://127.0.0.1:8000}"
export COMMAND_CENTER_DATA_MODE="${COMMAND_CENTER_DATA_MODE:-aws_live}"
export STREAMLIT_HOST="${STREAMLIT_HOST:-0.0.0.0}"
export STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"

EXPECTED_API_BASE_URL="${EXPECTED_API_BASE_URL:-http://127.0.0.1:8000}"
START_TIMEOUT_SECONDS="${STREAMLIT_START_TIMEOUT_SECONDS:-45}"
STOP_TIMEOUT_SECONDS="${STREAMLIT_STOP_TIMEOUT_SECONDS:-15}"
API_BASE_URL="${COMMAND_CENTER_API_BASE_URL%/}"
LOCAL_URL="http://127.0.0.1:${STREAMLIT_PORT}"

if [[ -x "$VENV_DIR/bin/python" ]]; then
    VENV_BIN="$VENV_DIR/bin"
elif [[ -x "$VENV_DIR/Scripts/python.exe" ]]; then
    # Windows venv layout when run from Git Bash.
    VENV_BIN="$VENV_DIR/Scripts"
else
    VENV_BIN="$VENV_DIR/bin"
fi
PYTHON_BIN="$VENV_BIN/python"
STREAMLIT_BIN="$VENV_BIN/streamlit"

BACKEND_HEALTH_ENDPOINT=""
TEST_FAILURES=0

usage() {
    cat <<'EOF'
Usage:

  streamlit.sh start
  streamlit.sh stop
  streamlit.sh restart
  streamlit.sh status
  streamlit.sh logs [-f]
  streamlit.sh test
  streamlit.sh help

Environment overrides (defaults):
  COMMAND_CENTER_API_BASE_URL   http://127.0.0.1:8000
  COMMAND_CENTER_DATA_MODE      aws_live
  STREAMLIT_HOST                0.0.0.0
  STREAMLIT_PORT                8501
EOF
}

log() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
err() { printf 'ERROR: %s\n' "$*" >&2; }

ensure_dirs() {
    mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
}

require_runtime() {
    if [[ ! -x "$PYTHON_BIN" ]]; then
        err "Python interpreter not found at $PYTHON_BIN. Run $SCRIPT_DIR/bootstrap_streamlit.sh first."
        return 1
    fi
    if [[ ! -x "$STREAMLIT_BIN" ]]; then
        err "Streamlit executable not found at $STREAMLIT_BIN. Run $SCRIPT_DIR/bootstrap_streamlit.sh first."
        return 1
    fi
    if [[ ! -f "$STREAMLIT_ENTRYPOINT" ]]; then
        err "Streamlit entrypoint not found at $STREAMLIT_ENTRYPOINT."
        return 1
    fi
}

pid_alive() {
    [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null
}

process_command() {
    local pid="$1" cmd=""
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ -z "$cmd" && -r "/proc/$pid/cmdline" ]]; then
        cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    fi
    printf '%s' "$cmd"
}

command_is_this_streamlit() {
    local cmd="$1"
    [[ "$cmd" == *streamlit* && "$cmd" == *"$STREAM_UI_NAME"[/\\]dashboard.py* ]]
}

is_this_streamlit() {
    local pid="$1"
    pid_alive "$pid" || return 1
    command_is_this_streamlit "$(process_command "$pid")"
}

command_targets_port() {
    local cmd="$1"
    if [[ "$cmd" =~ --server\.port[=[:space:]]+([0-9]+) ]]; then
        [[ "${BASH_REMATCH[1]}" == "$STREAMLIT_PORT" ]]
    else
        [[ "$STREAMLIT_PORT" == "8501" ]]
    fi
}

is_this_streamlit_on_port() {
    local pid="$1" cmd
    pid_alive "$pid" || return 1
    cmd="$(process_command "$pid")"
    command_is_this_streamlit "$cmd" && command_targets_port "$cmd"
}

process_state() {
    local pid="$1" state=""
    state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -z "$state" && -r "/proc/$pid/status" ]]; then
        state="$(awk '/^State:/ {print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)"
    fi
    printf '%s' "${state:-RUNNING}"
}

read_pid_file() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(tr -cd '0-9' <"$PID_FILE")"
    [[ -n "$pid" ]] || return 1
    printf '%s' "$pid"
}

discover_streamlit_pids() {
    local pid
    if ps -eo pid= >/dev/null 2>&1; then
        while read -r pid; do
            if [[ "$pid" != "$$" ]] && is_this_streamlit_on_port "$pid"; then
                printf '%s\n' "$pid"
            fi
        done < <(ps -eo pid= 2>/dev/null)
    elif [[ -d /proc ]]; then
        for pid in /proc/[0-9]*; do
            pid="${pid#/proc/}"
            if [[ "$pid" != "$$" ]] && is_this_streamlit_on_port "$pid"; then
                printf '%s\n' "$pid"
            fi
        done
    fi
    return 0
}

port_listening() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn "sport = :$port" 2>/dev/null | awk 'NR > 1 {found = 1} END {exit !found}'
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    elif command -v netstat >/dev/null 2>&1; then
        netstat -an 2>/dev/null | grep -Eiq "[:.]${port}[[:space:]].*LISTEN"
    else
        "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(1)
    sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
    fi
}

port_owner_description() {
    local port="$1" owners=""
    if command -v ss >/dev/null 2>&1; then
        owners="$(ss -ltnp "sport = :$port" 2>/dev/null | grep -o 'users:(([^)]*))' | sort -u | tr '\n' ' ' || true)"
    elif command -v lsof >/dev/null 2>&1; then
        owners="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 {print $1 "(pid " $2 ")"}' | sort -u | tr '\n' ' ' || true)"
    fi
    printf '%s' "${owners:-unknown process}"
}

http_status() {
    local url="$1" code=""
    if command -v curl >/dev/null 2>&1; then
        code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
    else
        code="$("$PYTHON_BIN" - "$url" <<'PY' 2>/dev/null || true
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print("000")
PY
)"
    fi
    if [[ ! "$code" =~ ^[0-9]{3}$ ]]; then
        code="000"
    fi
    printf '%s' "$code"
}

backend_available() {
    if [[ "$(http_status "$API_BASE_URL/health")" == "200" ]]; then
        BACKEND_HEALTH_ENDPOINT="/health"
        return 0
    fi
    if [[ "$(http_status "$API_BASE_URL/openapi.json")" == "200" ]]; then
        BACKEND_HEALTH_ENDPOINT="/openapi.json"
        return 0
    fi
    BACKEND_HEALTH_ENDPOINT=""
    return 1
}

start_streamlit() {
    require_runtime
    ensure_dirs

    local pid=""
    if pid="$(read_pid_file)"; then
        if is_this_streamlit_on_port "$pid"; then
            log "STREAMLIT_STATUS=ALREADY_RUNNING"
            log "PID=$pid"
            return 0
        fi
        if is_this_streamlit "$pid"; then
            err "Streamlit PID $pid is already running on a different port than $STREAMLIT_PORT. Use '$0 restart' to move it."
            log "STREAMLIT_STARTED=FAILED"
            return 1
        fi
        warn "Removing stale PID file $PID_FILE (PID $pid is not this Streamlit app)."
        rm -f "$PID_FILE"
    fi

    local running
    running="$(discover_streamlit_pids)"
    if [[ -n "$running" ]]; then
        pid="${running%%$'\n'*}"
        printf '%s\n' "$pid" >"$PID_FILE"
        log "STREAMLIT_STATUS=ALREADY_RUNNING"
        log "PID=$pid"
        return 0
    fi

    if port_listening "$STREAMLIT_PORT"; then
        err "Port $STREAMLIT_PORT is already in use by $(port_owner_description "$STREAMLIT_PORT"). Not starting Streamlit; the existing process was not touched."
        log "STREAMLIT_STARTED=FAILED"
        log "PORT_STATUS=IN_USE"
        return 1
    fi

    if backend_available; then
        log "BACKEND_STATUS=CONNECTED"
    else
        log "BACKEND_STATUS=UNAVAILABLE"
        warn "Command Center backend is not responding at $API_BASE_URL. Streamlit will start and show 'Backend Unavailable' until it is reachable."
    fi

    cd "$REPO_ROOT"
    nohup "$STREAMLIT_BIN" run "$STREAMLIT_ENTRYPOINT" \
        --server.address "$STREAMLIT_HOST" \
        --server.port "$STREAMLIT_PORT" \
        --server.headless true \
        >"$LOG_FILE" 2>&1 &
    local new_pid=$!
    printf '%s\n' "$new_pid" >"$PID_FILE"

    local deadline=$((SECONDS + START_TIMEOUT_SECONDS)) code="000" listening="NO"
    while ((SECONDS < deadline)); do
        if ! pid_alive "$new_pid"; then
            err "Streamlit exited during startup. Last log lines:"
            tail -n 40 "$LOG_FILE" >&2 || true
            rm -f "$PID_FILE"
            log "STREAMLIT_STARTED=FAILED"
            return 1
        fi
        if port_listening "$STREAMLIT_PORT"; then
            listening="YES"
            code="$(http_status "$LOCAL_URL")"
            if [[ "$code" != "000" ]]; then
                break
            fi
        fi
        sleep 1
    done

    if [[ "$listening" != "YES" || "$code" == "000" ]]; then
        err "Streamlit (PID $new_pid) did not answer on $LOCAL_URL within ${START_TIMEOUT_SECONDS}s. Inspect '$0 logs' or run '$0 stop'."
        log "STREAMLIT_STARTED=FAILED"
        return 1
    fi
    if ! is_this_streamlit "$new_pid"; then
        warn "Could not verify the command line of PID $new_pid; '$0 stop' will refuse to signal it."
    fi

    log "STREAMLIT_STARTED=PASSED"
    log "PID=$new_pid"
    log "PORT=$STREAMLIT_PORT"
    log "HTTP_STATUS=$code"
    log "LOCAL_URL=$LOCAL_URL"
    log "API_BASE_URL=$API_BASE_URL"
}

terminate_verified_pid() {
    local pid="$1"
    if ! is_this_streamlit "$pid"; then
        warn "PID $pid is not this Streamlit app; not signalling it."
        return 0
    fi

    log "Sending SIGTERM to Streamlit PID $pid ..."
    kill -TERM "$pid" 2>/dev/null || true
    local deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
    while ((SECONDS < deadline)) && pid_alive "$pid"; do
        sleep 1
    done

    if pid_alive "$pid" && is_this_streamlit "$pid"; then
        warn "PID $pid did not exit within ${STOP_TIMEOUT_SECONDS}s; sending SIGKILL to the verified Streamlit PID."
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
    if pid_alive "$pid"; then
        err "Streamlit PID $pid is still running."
        return 1
    fi
}

stop_streamlit() {
    local pid="" pids=""
    if pid="$(read_pid_file)"; then
        if is_this_streamlit "$pid"; then
            pids="$pid"
        elif pid_alive "$pid"; then
            warn "PID $pid in $PID_FILE belongs to a different process; it will not be signalled. Removing stale PID file."
            rm -f "$PID_FILE"
        else
            warn "Removing stale PID file $PID_FILE (PID $pid is not running)."
            rm -f "$PID_FILE"
        fi
    fi

    if [[ -z "$pids" ]]; then
        pids="$(discover_streamlit_pids)"
    fi
    if [[ -z "$pids" ]]; then
        rm -f "$PID_FILE"
        log "STREAMLIT_STATUS=ALREADY_STOPPED"
        return 0
    fi

    for pid in $pids; do
        terminate_verified_pid "$pid"
    done
    rm -f "$PID_FILE"

    local deadline=$((SECONDS + 10))
    while ((SECONDS < deadline)) && port_listening "$STREAMLIT_PORT"; do
        sleep 1
    done
    if port_listening "$STREAMLIT_PORT"; then
        err "Port $STREAMLIT_PORT is still listening ($(port_owner_description "$STREAMLIT_PORT")); it was not started by this script and was not touched."
        log "STREAMLIT_STOPPED=FAILED"
        return 1
    fi
    log "STREAMLIT_STOPPED=PASSED"
}

restart_streamlit() {
    stop_streamlit
    start_streamlit
    log "STREAMLIT_RESTARTED=PASSED"
}

status_streamlit() {
    local pid="" state="STOPPED" process="NOT_RUNNING" port_state="NOT_LISTENING" code backend="UNAVAILABLE" rc=0
    if pid="$(read_pid_file)"; then
        if is_this_streamlit "$pid"; then
            state="RUNNING"
            process="$(process_state "$pid")"
        elif pid_alive "$pid"; then
            process="PID_NOT_STREAMLIT"
        else
            process="STALE_PID_FILE"
        fi
    else
        local running
        running="$(discover_streamlit_pids)"
        pid="${running%%$'\n'*}"
        if [[ -n "$pid" ]]; then
            state="RUNNING"
            process="RUNNING_WITHOUT_PID_FILE"
        fi
    fi
    if port_listening "$STREAMLIT_PORT"; then
        port_state="LISTENING"
    fi
    code="$(http_status "$LOCAL_URL")"
    if backend_available; then
        backend="CONNECTED"
    fi

    log "STREAMLIT_STATUS=$state"
    log "PID=${pid:-none}"
    log "PROCESS_STATUS=$process"
    log "PORT=$STREAMLIT_PORT"
    log "PORT_STATUS=$port_state"
    log "HTTP_STATUS=$code"
    log "BACKEND_STATUS=$backend"
    log "API_BASE_URL=$API_BASE_URL"
    log "STREAMLIT_ENTRYPOINT=$STREAMLIT_ENTRYPOINT"
    log "LOG_FILE=$LOG_FILE"

    if [[ "$state" == "RUNNING" ]]; then
        if [[ "$port_state" != "LISTENING" || "$code" != "200" || "$process" == "RUNNING_WITHOUT_PID_FILE" ]]; then
            rc=1
        fi
    elif [[ "$process" != "NOT_RUNNING" || "$port_state" == "LISTENING" ]]; then
        rc=1
    else
        rc=3
    fi
    if [[ "$backend" != "CONNECTED" ]]; then
        warn "Command Center backend is not responding at $API_BASE_URL."
    fi
    return "$rc"
}

logs_streamlit() {
    if [[ ! -f "$LOG_FILE" ]]; then
        err "No Streamlit log file yet at $LOG_FILE. Start Streamlit with: $0 start"
        return 1
    fi
    case "${1:-}" in
        "") tail -n 100 "$LOG_FILE" ;;
        -f | --follow) tail -n 100 -f "$LOG_FILE" ;;
        *)
            err "Unknown logs option: $1"
            usage >&2
            return 2
            ;;
    esac
}

record_check() {
    local name="$1" result="$2" detail="${3:-}"
    log "${name}=${result}${detail:+ ($detail)}"
    if [[ "$result" != "PASSED" ]]; then
        TEST_FAILURES=$((TEST_FAILURES + 1))
    fi
}

run_python() {
    (cd "$REPO_ROOT" && PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "$@")
}

test_streamlit() {
    require_runtime
    TEST_FAILURES=0
    local pid="" code output

    if pid="$(read_pid_file)" && is_this_streamlit "$pid"; then
        record_check "CHECK_PID_RUNNING" "PASSED" "PID $pid"
    else
        record_check "CHECK_PID_RUNNING" "FAILED" "no verified Streamlit PID in $PID_FILE"
    fi

    if port_listening "$STREAMLIT_PORT"; then
        record_check "CHECK_PORT_LISTENING" "PASSED" "port $STREAMLIT_PORT"
    else
        record_check "CHECK_PORT_LISTENING" "FAILED" "port $STREAMLIT_PORT"
    fi

    code="$(http_status "$LOCAL_URL")"
    if [[ "$code" == "200" ]]; then
        record_check "CHECK_STREAMLIT_HTTP" "PASSED" "$LOCAL_URL -> $code"
    else
        record_check "CHECK_STREAMLIT_HTTP" "FAILED" "$LOCAL_URL -> $code"
    fi

    code="$(http_status "$API_BASE_URL/")"
    if [[ "$code" != "000" ]]; then
        record_check "CHECK_BACKEND_RESPONDS" "PASSED" "$API_BASE_URL -> HTTP $code"
    else
        record_check "CHECK_BACKEND_RESPONDS" "FAILED" "$API_BASE_URL unreachable"
    fi

    if backend_available; then
        record_check "CHECK_BACKEND_HEALTH" "PASSED" "$BACKEND_HEALTH_ENDPOINT -> 200"
    else
        record_check "CHECK_BACKEND_HEALTH" "FAILED" "/health and /openapi.json did not return 200"
    fi

    if output="$(run_python - "$API_BASE_URL" <<'PY' 2>&1
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1] + "/openapi.json", timeout=5) as response:
    paths = json.load(response).get("paths", {})
required = {"/api/runs/stream": "post", "/api/runs/{run_id}/stream": "get"}
missing = [f"{method.upper()} {path}" for path, method in required.items() if method not in paths.get(path, {})]
if missing:
    print("missing: " + ", ".join(missing))
    sys.exit(1)
print("POST /api/runs/stream, GET /api/runs/{run_id}/stream")
PY
)"; then
        record_check "CHECK_SSE_ENDPOINT" "PASSED" "$output"
    else
        record_check "CHECK_SSE_ENDPOINT" "FAILED" "${output##*$'\n'}"
    fi

    if output="$(run_python -c "from ${STREAM_UI_NAME}.api_client import RunApiClient, load_ui_config, normalize_api_run; print(RunApiClient.__name__)" 2>&1)"; then
        record_check "CHECK_API_CLIENT_IMPORT" "PASSED" "${STREAM_UI_NAME}.api_client.$output"
    else
        record_check "CHECK_API_CLIENT_IMPORT" "FAILED" "${output##*$'\n'}"
    fi

    if output="$(run_python - "$STREAM_UI_NAME" <<'PY' 2>&1
import importlib
import sys

client = importlib.import_module(f"{sys.argv[1]}.api_client")
wire = [": heartbeat", "", "id: EVT-1", "event: connected", 'data: {"sequence": 1,', 'data: "run_id": "WX-T"}', ""]
events = list(client.iter_sse_events_from_lines(wire))
assert events == [{"sequence": 1, "run_id": "WX-T", "event_id": "EVT-1", "event_type": "connected"}], events
seen: set[str] = set()
assert client.should_accept_sse_event(events[0], seen_event_ids=seen, last_sequence=0) == (True, 1)
assert client.should_accept_sse_event(events[0], seen_event_ids=seen, last_sequence=1) == (False, 1)
print("iter_sse_events_from_lines, should_accept_sse_event")
PY
)"; then
        record_check "CHECK_SSE_PARSER_IMPORT" "PASSED" "$output"
    else
        record_check "CHECK_SSE_PARSER_IMPORT" "FAILED" "${output##*$'\n'}"
    fi

    local configured running_url=""
    configured="$(run_python -c "from ${STREAM_UI_NAME}.api_client import load_ui_config; print(load_ui_config().api_base_url)" 2>/dev/null || true)"
    if [[ -n "$pid" && -r "/proc/$pid/environ" ]]; then
        running_url="$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | sed -n 's/^COMMAND_CENTER_API_BASE_URL=//p' | head -n 1 || true)"
    fi
    if [[ "$configured" == "$EXPECTED_API_BASE_URL" && (-z "$running_url" || "${running_url%/}" == "$EXPECTED_API_BASE_URL") ]]; then
        record_check "CHECK_API_BASE_URL" "PASSED" "$configured${running_url:+; running process: ${running_url%/}}"
    else
        record_check "CHECK_API_BASE_URL" "FAILED" "configured=${configured:-unresolved}${running_url:+, running process=${running_url}}, expected=$EXPECTED_API_BASE_URL"
    fi

    if ((TEST_FAILURES == 0)); then
        log "STREAMLIT_RUNTIME_VALIDATION=PASSED"
        return 0
    fi
    log "STREAMLIT_RUNTIME_VALIDATION=FAILED ($TEST_FAILURES check(s) failed)"
    return 1
}

main() {
    local command="${1:-help}"
    if (($# > 0)); then
        shift
    fi
    case "$command" in
        start) start_streamlit ;;
        stop) stop_streamlit ;;
        restart) restart_streamlit ;;
        status) status_streamlit ;;
        logs) logs_streamlit "$@" ;;
        test) test_streamlit ;;
        help | -h | --help) usage ;;
        *)
            printf 'Unknown command: %s\n\n' "$command" >&2
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
