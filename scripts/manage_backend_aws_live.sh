#!/usr/bin/env bash
set -euo pipefail

# Manage the Network Resilience Command Center Backend service in AWS_LIVE mode.
#
# Usage:
#   ./scripts/manage_backend_aws_live.sh start
#   ./scripts/manage_backend_aws_live.sh stop
#   ./scripts/manage_backend_aws_live.sh restart
#   ./scripts/manage_backend_aws_live.sh status
#   ./scripts/manage_backend_aws_live.sh logs
#   ./scripts/manage_backend_aws_live.sh foreground

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
PID_FILE="$DIST_DIR/command_center_backend.pid"
LOG_FILE="$DIST_DIR/command_center_backend.log"
RUNTIME_INFO_PATH="$DIST_DIR/agent2_runtime_info.json"

AWS_REGION="${AWS_REGION:-us-east-2}"
HOST="${COMMAND_CENTER_HOST:-0.0.0.0}"
PORT="${COMMAND_CENTER_PORT:-8000}"

AGENT1_RUNTIME_ARN="${AGENT1_RUNTIME_ARN:-arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_weather_network_intelligence-aRoVQk71fj}"

# Discover Agent 2 Runtime ARN from env, dist/agent2_runtime_info.json, or default
if [ -n "${AGENT2_RUNTIME_ARN:-}" ]; then
    RESOLVED_AGENT2_ARN="$AGENT2_RUNTIME_ARN"
elif [ -f "$RUNTIME_INFO_PATH" ]; then
    RESOLVED_AGENT2_ARN="$(python3 -c "import json; print(json.load(open('$RUNTIME_INFO_PATH')).get('agentRuntimeArn', ''))" 2>/dev/null || true)"
else
    RESOLVED_AGENT2_ARN="arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_network_resilience-ph5xzN4X1u"
fi

if [ -z "${RESOLVED_AGENT2_ARN:-}" ]; then
    RESOLVED_AGENT2_ARN="arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/cohort3_network_resilience-ph5xzN4X1u"
fi

mkdir -p "$DIST_DIR"
cd "$PROJECT_ROOT"

if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

start_backend() {
    if is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "Command Center Backend (AWS_LIVE) is already running (PID=$pid, http://$HOST:$PORT)."
        return 0
    fi

    echo "============================================================"
    echo "Starting Command Center Backend in AWS_LIVE mode"
    echo "  URL:         http://$HOST:$PORT"
    echo "  Region:      $AWS_REGION"
    echo "  Agent 1 ARN: $AGENT1_RUNTIME_ARN"
    echo "  Agent 2 ARN: $RESOLVED_AGENT2_ARN"
    echo "  Log File:    $LOG_FILE"
    echo "============================================================"

    export AWS_REGION="$AWS_REGION"
    export COMMAND_CENTER_DATA_MODE="aws_live"
    export AGENT1_RUNTIME_ARN="$AGENT1_RUNTIME_ARN"
    export AGENT2_RUNTIME_ARN="$RESOLVED_AGENT2_ARN"

    nohup python3 -m command_center.backend.service \
        --host "$HOST" \
        --port "$PORT" \
        --data-mode aws_live >"$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # Wait briefly to confirm process stays up
    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "Started Command Center Backend successfully (PID=$new_pid)."
        echo "Check status: ./scripts/manage_backend_aws_live.sh status"
        echo "View logs:    ./scripts/manage_backend_aws_live.sh logs"
        echo "Stop server:  ./scripts/manage_backend_aws_live.sh stop"
    else
        echo "ERROR: Backend failed to start. Recent log output:" >&2
        cat "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop_backend() {
    if ! is_running; then
        echo "Command Center Backend is not currently running."
        rm -f "$PID_FILE"
        return 0
    fi

    local pid
    pid="$(cat "$PID_FILE")"
    echo "Stopping Command Center Backend (PID=$pid) ..."
    kill "$pid" 2>/dev/null || true

    for _ in $(seq 1 20); do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 0.25
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "Process still alive; sending SIGKILL to PID=$pid ..."
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    echo "Command Center Backend stopped."
}

status_backend() {
    if is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "STATUS: RUNNING (PID=$pid, mode=aws_live, endpoint=http://$HOST:$PORT)"
        echo "Agent 1 ARN: $AGENT1_RUNTIME_ARN"
        echo "Agent 2 ARN: $RESOLVED_AGENT2_ARN"
        echo "Log file:    $LOG_FILE"
    else
        echo "STATUS: STOPPED"
        return 1
    fi
}

foreground_backend() {
    if is_running; then
        echo "ERROR: Background instance is already running (PID=$(cat "$PID_FILE")). Run './scripts/manage_backend_aws_live.sh stop' first." >&2
        exit 1
    fi
    export AWS_REGION="$AWS_REGION"
    export COMMAND_CENTER_DATA_MODE="aws_live"
    export AGENT1_RUNTIME_ARN="$AGENT1_RUNTIME_ARN"
    export AGENT2_RUNTIME_ARN="$RESOLVED_AGENT2_ARN"

    echo "Starting Command Center Backend in foreground (AWS_LIVE mode on http://$HOST:$PORT) ..."
    exec python3 -m command_center.backend.service \
        --host "$HOST" \
        --port "$PORT" \
        --data-mode aws_live
}

ACTION="${1:-status}"

case "$ACTION" in
    start)
        start_backend
        ;;
    stop)
        stop_backend
        ;;
    restart)
        stop_backend
        start_backend
        ;;
    status)
        status_backend
        ;;
    logs)
        if [ -f "$LOG_FILE" ]; then
            tail -n 50 -f "$LOG_FILE"
        else
            echo "No log file found at $LOG_FILE"
        fi
        ;;
    foreground)
        foreground_backend
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|foreground}" >&2
        exit 1
        ;;
esac
