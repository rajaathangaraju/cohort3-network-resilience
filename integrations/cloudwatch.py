"""CloudWatch log discovery, correlation, Logs Insights fallback, and evidence writer."""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from common.tracing import generate_prefixed_id
from contracts.action_result import EvidenceRecord
from integrations.agentcore import (
    DEFAULT_AGENT1_RUNTIME_ARN,
    DEFAULT_AGENT2_RUNTIME_NAME,
    sanitize_runtime_error_message,
)

AGENT1_DEFAULT_LOG_GROUP = (
    "/aws/bedrock-agentcore/runtimes/cohort3_weather_network_intelligence-aRoVQk71fj-DEFAULT"
)

STAGE_MAPPING: dict[str, str] = {
    "input_received": "WEATHER",
    "input_validation": "GEO_CORRELATION",
    "inventory_enrichment": "ASSET_IMPACT",
    "service_impact_analysis": "ASSET_IMPACT",
    "risk_assessment": "RISK_ASSESSMENT",
    "recommendation": "RECOMMENDATION",
    "governance_decision": "GOVERNANCE",
    "human_approval": "APPROVAL",
    "action_execution": "ACTION",
    "verification": "VERIFICATION",
}


def discover_agent2_log_group(
    *,
    region_name: str = "us-east-2",
    runtime_info_path: Path | None = None,
    control_client: Any | None = None,
    logs_client: Any | None = None,
) -> tuple[str | None, str | None]:
    """Dynamically discover the deployed Agent 2 log group and runtime ARN without hardcoding."""
    runtime_arn: str | None = None
    runtime_id: str | None = None

    if runtime_info_path is not None and runtime_info_path.exists():
        try:
            info = json.loads(runtime_info_path.read_text(encoding="utf-8"))
            lg = info.get("logGroup")
            arn = info.get("agentRuntimeArn")
            rid = info.get("agentRuntimeId")
            if isinstance(arn, str) and arn.strip():
                runtime_arn = arn.strip()
            if isinstance(rid, str) and rid.strip():
                runtime_id = rid.strip()
            if isinstance(lg, str) and lg.strip():
                return lg.strip(), runtime_arn
        except (OSError, ValueError, KeyError):
            pass

    if not runtime_id:
        try:
            ctrl = control_client or boto3.client(
                "bedrock-agentcore-control", region_name=region_name
            )
            resp = ctrl.list_agent_runtimes(maxResults=100)
            for item in resp.get("agentRuntimes", []):
                if (
                    isinstance(item, dict)
                    and item.get("agentRuntimeName") == DEFAULT_AGENT2_RUNTIME_NAME
                ):
                    rid = item.get("agentRuntimeId")
                    arn = item.get("agentRuntimeArn")
                    if isinstance(rid, str) and rid.strip():
                        runtime_id = rid.strip()
                    if isinstance(arn, str) and arn.strip():
                        runtime_arn = arn.strip()
                    break
        except (BotoCoreError, ClientError, KeyError, TypeError):
            pass

    if runtime_id:
        return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT", runtime_arn

    try:
        l_client = logs_client or boto3.client("logs", region_name=region_name)
        groups_resp = l_client.describe_log_groups(
            logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/cohort3_network_resilience"
        )
        for grp in groups_resp.get("logGroups", []):
            name = grp.get("logGroupName")
            if isinstance(name, str) and name.strip():
                return name.strip(), runtime_arn
    except (BotoCoreError, ClientError, KeyError, TypeError):
        pass

    return None, runtime_arn


def filter_cloudwatch_events_by_correlation(
    raw_events: list[dict[str, Any]],
    *,
    run_id: str,
    trace_id: str,
    session_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter CloudWatch log events strictly to records matching run_id, trace_id, or session_id.

    Deduplicates and sorts chronologically by timestamp. Never includes unrelated log records
    and redacts any credential/token assignments.
    """
    tokens = [t for t in [run_id.strip(), trace_id.strip(), *(session_ids or [])] if t]
    if not tokens:
        return []

    seen: set[tuple[Any, str, str]] = set()
    matched: list[dict[str, Any]] = []

    for event in raw_events:
        message = str(event.get("message", ""))
        stream_name = str(event.get("logStreamName", ""))
        group_name = str(event.get("logGroupName", ""))
        haystack = f"{message} {stream_name}"

        if any(tok in haystack for tok in tokens):
            ts = event.get("timestamp")
            sanitized_msg = sanitize_runtime_error_message(message)
            dedup_key = (ts, group_name, sanitized_msg)
            if dedup_key not in seen:
                seen.add(dedup_key)
                matched.append(
                    {
                        "timestamp": ts,
                        "logGroupName": group_name,
                        "logStreamName": stream_name,
                        "message": sanitized_msg,
                    }
                )

    matched.sort(key=lambda item: int(item.get("timestamp") or 0))
    return matched


def query_cloudwatch_logs_insights(
    *,
    log_group_names: list[str],
    run_id: str,
    trace_id: str,
    session_ids: list[str] | None = None,
    region_name: str = "us-east-2",
    logs_client: Any | None = None,
    lookback_minutes: int = 60,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Execute a CloudWatch Logs Insights query filtering on run_id, trace_id, or session ID."""
    valid_groups = [g for g in log_group_names if g]
    if not valid_groups:
        return []

    client = logs_client or boto3.client("logs", region_name=region_name)
    now = datetime.now(UTC)
    start_epoch = int((now - timedelta(minutes=lookback_minutes)).timestamp())
    end_epoch = int((now + timedelta(minutes=2)).timestamp())

    clauses: list[str] = []
    for token in [run_id.strip(), trace_id.strip(), *(session_ids or [])]:
        if token:
            safe_tok = token.replace('"', "")
            clauses.append(f'@message like "{safe_tok}"')
            clauses.append(f'@logStream like "{safe_tok}"')

    if not clauses:
        return []

    filter_expr = " or ".join(clauses)
    query_string = (
        "fields @timestamp, @log, @logStream, @message "
        f"| filter {filter_expr} "
        "| sort @timestamp asc "
        f"| limit {limit}"
    )

    try:
        start_resp = client.start_query(
            logGroupNames=valid_groups,
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=query_string,
            limit=limit,
        )
        query_id = start_resp.get("queryId")
        if not isinstance(query_id, str) or not query_id:
            return []

        for _ in range(15):
            res = client.get_query_results(queryId=query_id)
            status = res.get("status", "")
            if status in {"Complete", "Failed", "Cancelled", "Timeout"}:
                if status != "Complete":
                    return []
                rows = res.get("results", [])
                events: list[dict[str, Any]] = []
                for row in rows:
                    row_map = {
                        col.get("field"): col.get("value", "")
                        for col in row
                        if isinstance(col, dict)
                    }
                    ts_str = str(row_map.get("@timestamp", ""))
                    ts_ms = 0
                    if ts_str:
                        try:
                            parsed_dt = datetime.fromisoformat(
                                ts_str.replace(" ", "T") + ("+00:00" if "+" not in ts_str else "")
                            )
                            ts_ms = int(parsed_dt.timestamp() * 1000)
                        except ValueError:
                            ts_ms = 0
                    log_field = str(row_map.get("@log", ""))
                    group_name = log_field.split(":", 1)[1] if ":" in log_field else log_field
                    events.append(
                        {
                            "timestamp": ts_ms,
                            "logGroupName": group_name,
                            "logStreamName": str(row_map.get("@logStream", "")),
                            "message": str(row_map.get("@message", "")),
                        }
                    )
                return filter_cloudwatch_events_by_correlation(
                    events,
                    run_id=run_id,
                    trace_id=trace_id,
                    session_ids=session_ids,
                )
            time.sleep(0.5)
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
        return []

    return []


def fetch_correlated_cloudwatch_events(
    *,
    log_group_names: list[str],
    run_id: str,
    trace_id: str,
    session_ids: list[str] | None = None,
    region_name: str = "us-east-2",
    logs_client: Any | None = None,
    limit_per_group: int = 200,
    lookback_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Query CloudWatch log groups and return only events matching run_id, trace_id, or session ID.

    Uses a 3-tier strategy so newly written AgentCore logs are reliably captured:
    1. Targeted `filter_log_events` with `filterPattern` and `startTime` across each log group.
    2. Newest log-stream scan (`describe_log_streams(orderBy='LastEventTime', descending=True)`
       + `filter_log_events(logStreamNames=...)`) to avoid scanning only old streams.
    3. CloudWatch Logs Insights query fallback (`query_cloudwatch_logs_insights`).
    """
    client = logs_client or boto3.client("logs", region_name=region_name)
    collected: list[dict[str, Any]] = []
    start_time_ms = int(
        (datetime.now(UTC) - timedelta(minutes=lookback_minutes)).timestamp() * 1000
    )

    search_terms = [t for t in [run_id.strip(), trace_id.strip(), *(session_ids or [])] if t]

    for group in log_group_names:
        if not group:
            continue

        # Strategy 1: Targeted filterPattern search per correlation token
        for term in search_terms:
            try:
                resp = client.filter_log_events(
                    logGroupName=group,
                    filterPattern=f'"{term}"',
                    startTime=start_time_ms,
                    limit=limit_per_group,
                )
                for item in resp.get("events", []):
                    if isinstance(item, dict):
                        item_with_group = dict(item)
                        item_with_group["logGroupName"] = group
                        collected.append(item_with_group)
            except (BotoCoreError, ClientError, TypeError):
                pass

        # Strategy 2: Scan the 5 most recent log streams by LastEventTime (or unfiltered fallback)
        try:
            streams_resp = client.describe_log_streams(
                logGroupName=group,
                orderBy="LastEventTime",
                descending=True,
                limit=5,
            )
            stream_names = [
                s["logStreamName"]
                for s in streams_resp.get("logStreams", [])
                if isinstance(s, dict) and s.get("logStreamName")
            ]
            if stream_names:
                recent_resp = client.filter_log_events(
                    logGroupName=group,
                    logStreamNames=stream_names,
                    startTime=start_time_ms,
                    limit=limit_per_group,
                )
            else:
                recent_resp = client.filter_log_events(
                    logGroupName=group,
                    limit=limit_per_group,
                )
            for item in recent_resp.get("events", []):
                if isinstance(item, dict):
                    item_with_group = dict(item)
                    item_with_group["logGroupName"] = group
                    collected.append(item_with_group)
        except (BotoCoreError, ClientError, AttributeError, TypeError):
            # Fallback for mock clients implementing only basic filter_log_events
            try:
                basic_resp = client.filter_log_events(
                    logGroupName=group,
                    limit=limit_per_group,
                )
                for item in basic_resp.get("events", []):
                    if isinstance(item, dict):
                        item_with_group = dict(item)
                        item_with_group["logGroupName"] = group
                        collected.append(item_with_group)
            except (BotoCoreError, ClientError):
                continue

    matched = filter_cloudwatch_events_by_correlation(
        collected,
        run_id=run_id,
        trace_id=trace_id,
        session_ids=session_ids,
    )

    # Strategy 3: CloudWatch Logs Insights fallback if filter_log_events returned 0 matches
    if not matched and hasattr(client, "start_query"):
        matched = query_cloudwatch_logs_insights(
            log_group_names=log_group_names,
            run_id=run_id,
            trace_id=trace_id,
            session_ids=session_ids,
            region_name=region_name,
            logs_client=client,
            lookback_minutes=lookback_minutes,
            limit=limit_per_group,
        )

    return matched


def build_evidence_index(
    *,
    run_id: str,
    trace_id: str,
    agent1_response: dict[str, Any] | None,
    agent2_response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the canonical evidence-index.json list across all required stages."""
    now_iso = datetime.now(UTC).isoformat()
    index: list[dict[str, Any]] = []

    if agent1_response is not None:
        index.append(
            {
                "evidence_id": generate_prefixed_id("EVD"),
                "run_id": run_id,
                "trace_id": trace_id,
                "stage": "WEATHER",
                "event_type": "AGENT1_WEATHER_INGESTED",
                "status": "COMPLETED",
                "recorded_at": str(agent1_response.get("created_at", now_iso)),
                "source": "weather_network_intelligence",
                "summary": (
                    f"Agent 1 ingested {agent1_response.get('weather_event')} "
                    f"({agent1_response.get('severity')}) for "
                    f"{agent1_response.get('affected_region')}."
                ),
                "synthetic": True,
            }
        )
        index.append(
            {
                "evidence_id": generate_prefixed_id("EVD"),
                "run_id": run_id,
                "trace_id": trace_id,
                "stage": "GEO_CORRELATION",
                "event_type": "AGENT1_GEO_CORRELATED",
                "status": "COMPLETED",
                "recorded_at": str(agent1_response.get("created_at", now_iso)),
                "source": "weather_network_intelligence",
                "summary": (
                    f"Agent 1 correlated affected elements "
                    f"{agent1_response.get('affected_network_elements')}."
                ),
                "synthetic": True,
            }
        )

    raw_evidence = agent2_response.get("evidence", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, EvidenceRecord):
                ev_dict = item.model_dump(mode="json")
            elif isinstance(item, dict):
                ev_dict = dict(item)
            else:
                continue

            raw_stage = str(ev_dict.get("stage", ""))
            mapped_stage = STAGE_MAPPING.get(raw_stage, raw_stage.upper() or "EVIDENCE")
            index.append(
                {
                    "evidence_id": str(ev_dict.get("evidence_id", generate_prefixed_id("EVD"))),
                    "run_id": str(ev_dict.get("run_id", run_id)),
                    "trace_id": str(ev_dict.get("trace_id", trace_id)),
                    "stage": mapped_stage,
                    "event_type": str(ev_dict.get("event_type", "WORKFLOW_EVENT")),
                    "status": str(ev_dict.get("status", "RECORDED")),
                    "recorded_at": str(ev_dict.get("recorded_at", now_iso)),
                    "source": str(ev_dict.get("source", "network_resilience")),
                    "summary": sanitize_runtime_error_message(str(ev_dict.get("summary", ""))),
                    "synthetic": bool(ev_dict.get("synthetic", True)),
                }
            )

    index.append(
        {
            "evidence_id": generate_prefixed_id("EVD"),
            "run_id": run_id,
            "trace_id": trace_id,
            "stage": "EVIDENCE",
            "event_type": "EVIDENCE_BUNDLE_INDEXED",
            "status": "COMPLETED",
            "recorded_at": now_iso,
            "source": "evidence_collector",
            "summary": f"Normalized evidence index generated for run_id={run_id}.",
            "synthetic": True,
        }
    )
    return index


def write_run_evidence_bundle(
    *,
    output_root: Path,
    run_id: str,
    trace_id: str,
    agent1_response: dict[str, Any],
    agent2_response: dict[str, Any],
    cloudwatch_events: list[dict[str, Any]],
    runtime_metadata: dict[str, Any] | None = None,
) -> Path:
    """Write the structured evidence files under evidence/<run_id>/."""
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    evidence_index = build_evidence_index(
        run_id=run_id,
        trace_id=trace_id,
        agent1_response=agent1_response,
        agent2_response=agent2_response,
    )

    agent1_cw_count = sum(
        1
        for e in cloudwatch_events
        if "weather_network_intelligence" in str(e.get("logGroupName", ""))
        or "weather_network_intelligence" in str(e.get("message", ""))
    )
    agent2_cw_count = sum(
        1
        for e in cloudwatch_events
        if "network_resilience" in str(e.get("logGroupName", ""))
        or '"agent_name": "network_resilience"' in str(e.get("message", ""))
        or '"agent_name":"network_resilience"' in str(e.get("message", ""))
    )

    workflow_summary = {
        "run_id": run_id,
        "trace_id": trace_id,
        "schema_version": agent2_response.get("schema_version", "0.1"),
        "team_code": agent2_response.get("team_code", "TEAM_PENDING"),
        "risk_assessment": agent2_response.get("risk_assessment"),
        "ai_reasoning": agent2_response.get("ai_reasoning"),
        "recommendation": agent2_response.get("recommendation"),
        "governance": agent2_response.get("governance"),
        "approval": agent2_response.get("approval"),
        "execution": agent2_response.get("execution"),
        "verification": agent2_response.get("verification"),
        "workflow_stages": agent2_response.get("workflow_stages", []),
        "evidence_count": len(evidence_index),
        "cloudwatch_event_count": len(cloudwatch_events),
        "agent1_cloudwatch_event_count": agent1_cw_count,
        "agent2_cloudwatch_event_count": agent2_cw_count,
        "synthetic": True,
    }

    resolved_metadata = runtime_metadata or {
        "run_id": run_id,
        "trace_id": trace_id,
        "aws_region": "us-east-2",
        "agent1_runtime_arn": DEFAULT_AGENT1_RUNTIME_ARN,
        "agent2_runtime_arn": None,
        "agent1_session_id": None,
        "agent2_session_id": None,
        "recorded_at": datetime.now(UTC).isoformat(),
    }

    (run_dir / "agent1-response.json").write_text(
        json.dumps(agent1_response, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "agent2-response.json").write_text(
        json.dumps(agent2_response, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "workflow-summary.json").write_text(
        json.dumps(workflow_summary, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "cloudwatch-events.json").write_text(
        json.dumps(cloudwatch_events, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "evidence-index.json").write_text(
        json.dumps(evidence_index, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "runtime-metadata.json").write_text(
        json.dumps(resolved_metadata, indent=2) + "\n", encoding="utf-8"
    )
    return run_dir
