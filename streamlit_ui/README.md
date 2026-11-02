# Streamlit UI

The Streamlit Command Center is a presentation and operator-interaction layer.
Risk scores, recommendations, governance decisions, approval, execution, and
verification states all come from the Command Center Backend API; the UI never
calculates or fabricates them. When the backend is unreachable the dashboard
shows **Backend Unavailable** with a **RETRY CONNECTION** button instead of
falling back to local demo data.

## 1. Start the Command Center Backend

The backend requires Python 3.13. From the project folder:

```powershell
cd "C:\Users\VegadN\OneDrive - Tata Communications\Projects\BBAmazon\cohort3-network-resilience"
python -m pip install -e ".[dev,dashboard]" uvicorn
```

Run it in `aws_live` mode (invokes the deployed AgentCore Agent 1 and Agent 2;
requires AWS credentials) or in `mock` mode (local agents, no AWS calls):

```powershell
$env:COMMAND_CENTER_DATA_MODE = "mock"   # or "aws_live"
python -m uvicorn command_center.backend.main:app --host 127.0.0.1 --port 8000
```

Check it with `http://127.0.0.1:8000/health` or browse `http://127.0.0.1:8000/docs`.

## 2. Start the Streamlit dashboard

The dashboard runs on Python 3.10 or newer:

```powershell
python -m pip install -r streamlit_ui\requirements.txt --timeout 120 --retries 5
python -m streamlit run streamlit_ui/dashboard.py
```

Open the printed URL, usually `http://localhost:8501`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMMAND_CENTER_API_BASE_URL` | `http://127.0.0.1:8000` | Backend base URL (falls back to `NETWORK_RESILIENCE_API_URL`) |
| `COMMAND_CENTER_DATA_MODE` | `aws_live` | Requested data mode shown in the UI; the backend's own mode is read from `/health` |
| `SSE_CONNECT_TIMEOUT_SECONDS` | `10` | HTTP connect timeout |
| `SSE_READ_TIMEOUT_SECONDS` | `300` | Maximum wait between SSE frames |
| `SSE_RECONNECT_DELAY_SECONDS` | `2` | Delay before reconnecting an interrupted stream with `Last-Event-ID` |

## Operator flow

1. Pick a scenario (`PAUSE`, `ALLOW`, `DENY`) or **Custom inputs** to choose
   weather event, severity, and region, then click **START RESILIENCE ANALYSIS**.
2. Progress streams live from `POST /api/runs/stream`: the 7-step workflow bar
   (Weather, Geo, Asset, Risk, Action, Verify, Evidence), KPI cards, and the live
   activity log update per event. On completion the dashboard loads the
   authoritative `GET /api/runs/{run_id}` and `GET /api/runs/{run_id}/evidence`.
3. **APPROVE** / **REJECT** are enabled only when governance is `PAUSE` and
   approval is `PENDING`. Rejection requires a reason. After a decision the UI
   replays the post-decision events from `GET /api/runs/{run_id}/stream` using
   `Last-Event-ID`, then refreshes the run and evidence.
4. **VIEW EVIDENCE** shows application evidence and CloudWatch counts (total,
   Agent 1, Agent 2), the normalized evidence table, and sanitized raw JSON.
5. The Bedrock reasoning panel shows the model, result status, and the four
   reasoning fields, with a warning banner when the deterministic fallback was used.

Backend endpoints used: `GET /health` (falls back to `GET /openapi.json`),
`POST /api/runs/stream`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/stream`,
`POST /api/runs/{run_id}/approve`, `POST /api/runs/{run_id}/reject`,
`GET /api/runs/{run_id}/evidence`.

If PowerShell blocks virtual-environment activation, run this once for the
current user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
