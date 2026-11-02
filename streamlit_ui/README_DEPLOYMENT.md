# Streamlit Command Center: Deployment and Operations

The Streamlit UI lives in `streamlit_ui/` (entrypoint `streamlit_ui/dashboard.py`). It is a client of the
Command Center backend (`command_center.backend.main:app`) and reads all run, risk, governance, and evidence
data from that API, streaming run progress over SSE.

All scripts are in `streamlit_ui/scripts/` and resolve their paths from their own location, so they can be run
from any working directory. They require bash (Amazon Linux / Ubuntu on EC2).

| Script | Purpose |
| --- | --- |
| `bootstrap_streamlit.sh` | Check for Python 3.13+, create `.venv` only if missing, install `streamlit_ui/requirements.txt`, create `runtime/` and `logs/`. Does not start Streamlit. |
| `build_streamlit.sh` | Activate `.venv`, verify Python and Streamlit, compile sources, run the Streamlit tests, verify the API client and SSE parser, scan for committed credentials. |
| `streamlit.sh` | Lifecycle management: `start`, `stop`, `restart`, `status`, `logs [-f]`, `test`, `help`. |
| `deploy_streamlit.sh` | Runs bootstrap, build, `streamlit.sh restart`, `streamlit.sh status`, `streamlit.sh test`, in that order. |

## Prerequisites

- Python 3.13+ available as `python3.13`, `python3`, or `python` (or set `PYTHON=/path/to/python3.13`).
- The backend running and reachable from the instance, normally at `http://127.0.0.1:8000`.

## Deploy

```bash
./streamlit_ui/scripts/deploy_streamlit.sh
```

Prints `STREAMLIT_DEPLOYMENT=PASSED` on success. On failure it prints
`STREAMLIT_DEPLOYMENT=FAILED (step: <name>, exit code: <n>)` and stops at that step.

## Manage

```bash
./streamlit_ui/scripts/streamlit.sh start      # STREAMLIT_STARTED=PASSED, or STREAMLIT_STATUS=ALREADY_RUNNING
./streamlit_ui/scripts/streamlit.sh stop       # STREAMLIT_STOPPED=PASSED, or STREAMLIT_STATUS=ALREADY_STOPPED
./streamlit_ui/scripts/streamlit.sh restart    # STREAMLIT_RESTARTED=PASSED
./streamlit_ui/scripts/streamlit.sh status     # key=value report
./streamlit_ui/scripts/streamlit.sh logs       # last 100 lines
./streamlit_ui/scripts/streamlit.sh logs -f    # follow
./streamlit_ui/scripts/streamlit.sh test       # STREAMLIT_RUNTIME_VALIDATION=PASSED
```

`status` exit codes: `0` healthy, `1` unhealthy (port, HTTP, PID file, or backend problem), `3` cleanly stopped.

`test` is read-only. It checks the PID, the port, Streamlit HTTP, backend reachability, `/health` (or
`/openapi.json`), that the SSE routes `POST /api/runs/stream` and `GET /api/runs/{run_id}/stream` are published
in the backend OpenAPI schema, the API client and SSE parser imports, and that the API base URL is
`http://127.0.0.1:8000`. It never creates, approves, or rejects a run.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMMAND_CENTER_API_BASE_URL` | `http://127.0.0.1:8000` | Backend URL used by the UI |
| `COMMAND_CENTER_DATA_MODE` | `aws_live` | Data mode requested from the backend |
| `STREAMLIT_HOST` | `0.0.0.0` | Streamlit bind address |
| `STREAMLIT_PORT` | `8501` | Streamlit port |
| `VENV_DIR` | `<repo>/.venv` | Virtual environment location |
| `STREAMLIT_START_TIMEOUT_SECONDS` | `45` | Startup wait before failing |
| `STREAMLIT_STOP_TIMEOUT_SECONDS` | `15` | SIGTERM grace period before SIGKILL |

Runtime files:

- PID: `streamlit_ui/runtime/streamlit.pid`
- Log: `streamlit_ui/logs/streamlit.log`

Both directories are git-ignored.

## Process safety

- The PID file is never trusted on its own. Before signalling a process, `stop` confirms (via
  `ps -p PID -o command=`, or `/proc/PID/cmdline` as a fallback) that it is a `streamlit` process running
  `streamlit_ui/dashboard.py`. A PID that fails this check is never signalled; the stale PID file is removed.
- `SIGKILL` is sent only to the same verified PID, and only after the SIGTERM grace period expires.
- If the port is held by anything else, `start` refuses to start and leaves that process alone.
- The scripts never stop the FastAPI backend or any other Python process.

## Security

- No credentials are stored or read by these scripts. The backend uses the EC2 instance role.
- Restrict inbound port 8501 in the EC2 security group to trusted source CIDRs (VPN or office ranges). Do not
  open it to `0.0.0.0/0`. If the UI is fronted by a reverse proxy or SSM port forwarding, set
  `STREAMLIT_HOST=127.0.0.1`.
- Keep the backend private. `scripts/manage_backend_aws_live.sh` binds to `0.0.0.0` by default; start it with
  `COMMAND_CENTER_HOST=127.0.0.1` so port 8000 is reachable only from the instance, and do not open 8000 in
  the security group.
- Approve and Reject are enabled only when the backend reports a pending, actionable approval. Nothing is
  auto-approved.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `BACKEND_STATUS=UNAVAILABLE` | Start the backend and confirm `curl http://127.0.0.1:8000/health`. Streamlit still starts and shows "Backend Unavailable". |
| `PORT_STATUS=IN_USE` on start | Another process holds the port. Identify it (`ss -ltnp 'sport = :8501'`) or choose another `STREAMLIT_PORT`. |
| `already running on a different port` | Use `streamlit.sh restart` to move the instance to the new `STREAMLIT_PORT`. |
| `STREAMLIT_STARTED=FAILED` with an exit | Run `streamlit.sh logs` and look at the traceback. |
| Bootstrap reports Python older than 3.13 | Install Python 3.13, then remove or rename the old `.venv`. Bootstrap never rebuilds an existing venv. |
