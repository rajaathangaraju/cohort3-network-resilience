"""Unit and ASGI HTTP tests for the Agent 2 Amazon Bedrock AgentCore Runtime entrypoint wrapper.

Validates:
1. Valid NetworkRiskEvent is accepted.
2. Existing Agent 2 (NetworkResilienceAgent) is invoked without duplicating logic.
3. run_id is preserved.
4. trace_id is preserved.
5. Existing deterministic risk score is returned.
6. Existing recommendation is returned.
7. Existing governance decision is returned.
8. Pending approval blocks state-changing execution.
9. Malformed input returns a sanitized error.
10. Credentials and session tokens are never exposed.
11. Zero relative imports exist in agents/network_resilience/agentcore_app.py.
"""

import ast
import json
from pathlib import Path
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Scope

from agents.network_resilience.agent import NetworkResilienceAgent
from agents.network_resilience.agentcore_app import (
    app,
    invoke,
    set_agent2,
)
from common.exceptions import InventoryLookupError
from contracts.action_result import Agent2Result
from contracts.governance_decision import ApprovalStatus
from contracts.network_risk_event import NetworkRiskEvent
from tests.integration.test_agent1_agent2 import EXACT_AGENT1_INTEGRATION_PAYLOAD


class SpyAgent2(NetworkResilienceAgent):
    """Spy subclass of NetworkResilienceAgent to verify direct delegation."""

    def __init__(self) -> None:
        super().__init__(strict_region_check=False)
        self.call_count = 0
        self.last_payload: dict[str, Any] | NetworkRiskEvent | None = None

    def process(
        self,
        payload: dict[str, Any] | NetworkRiskEvent,
        *,
        simulated_approval_decision: ApprovalStatus | None = None,
        approver_reference: str = "demo-approver",
        approval_comments: str | None = None,
        force_deny: bool = False,
        force_deny_reason: str | None = None,
    ) -> Agent2Result:
        self.call_count += 1
        self.last_payload = payload
        return super().process(
            payload,
            simulated_approval_decision=simulated_approval_decision,
            approver_reference=approver_reference,
            approval_comments=approval_comments,
            force_deny=force_deny,
            force_deny_reason=force_deny_reason,
        )


class FailingSecretAgent2(NetworkResilienceAgent):
    """Simulates an internal error containing a sensitive token string."""

    def process(
        self,
        payload: dict[str, Any] | NetworkRiskEvent,
        *,
        simulated_approval_decision: ApprovalStatus | None = None,
        approver_reference: str = "demo-approver",
        approval_comments: str | None = None,
        force_deny: bool = False,
        force_deny_reason: str | None = None,
    ) -> Agent2Result:
        del (
            payload,
            simulated_approval_decision,
            approver_reference,
            approval_comments,
            force_deny,
            force_deny_reason,
        )
        raise InventoryLookupError(
            "Adapter failure with aws_session_token=TOP_SECRET_SESSION_TOKEN_123"
        )


def test_agent2_agentcore_wrapper_invokes_existing_agent2_and_preserves_contract() -> None:
    spy = SpyAgent2()
    set_agent2(spy)
    try:
        response = invoke(dict(EXACT_AGENT1_INTEGRATION_PAYLOAD))
        assert isinstance(response, dict)
        assert spy.call_count == 1

        # Validate correlation & contract fields
        assert response["schema_version"] == "0.1"
        assert response["team_code"] == "TEAM_PENDING"
        assert response["run_id"] == "WX-46329ae2b74b"
        assert response["trace_id"] == "7a4106b675e4419f87b9b84d30a67ca4"
        assert response["risk_assessment"]["risk_score"] == 85
        assert response["risk_assessment"]["overall_risk"] == "CRITICAL"
        assert response["recommendation"]["action"] == "REROUTE_TRAFFIC"
        assert response["governance"]["decision"] == "PAUSE"
        assert response["approval"]["status"] == "PENDING"
        assert response["execution"]["status"] == "BLOCKED"
        assert response["verification"]["status"] == "NOT_STARTED"
        assert len(response["evidence"]) == 10

        # Validate round-trip into Agent2Result contract
        validated = Agent2Result.model_validate(response)
        assert validated.run_id == "WX-46329ae2b74b"
        assert validated.trace_id == "7a4106b675e4419f87b9b84d30a67ca4"
    finally:
        set_agent2(None)


def test_agent2_agentcore_wrapper_supports_explicit_approve_and_reject() -> None:
    set_agent2(None)

    approved_payload = dict(EXACT_AGENT1_INTEGRATION_PAYLOAD)
    approved_payload["simulated_approval_decision"] = "APPROVED"
    approved_payload["approver_reference"] = "demo-approver"
    approved_resp = invoke(approved_payload)
    assert isinstance(approved_resp, dict)
    assert approved_resp["governance"]["decision"] == "PAUSE"
    assert approved_resp["approval"]["status"] == "APPROVED"
    assert approved_resp["execution"]["status"] == "EXECUTED"
    assert approved_resp["verification"]["status"] == "PASSED"

    rejected_payload = dict(EXACT_AGENT1_INTEGRATION_PAYLOAD)
    rejected_payload["simulated_approval_decision"] = "REJECTED"
    rejected_payload["approver_reference"] = "demo-approver"
    rejected_resp = invoke(rejected_payload)
    assert isinstance(rejected_resp, dict)
    assert rejected_resp["governance"]["decision"] == "PAUSE"
    assert rejected_resp["approval"]["status"] == "REJECTED"
    assert rejected_resp["execution"]["status"] == "BLOCKED"
    assert rejected_resp["verification"]["status"] == "BLOCKED"


def test_agent2_agentcore_wrapper_rejects_malformed_input_and_redacts_secrets() -> None:
    set_agent2(None)

    non_dict = invoke(["bad", "input"])
    assert isinstance(non_dict, JSONResponse)
    assert non_dict.status_code == 400

    bad_payload = dict(EXACT_AGENT1_INTEGRATION_PAYLOAD)
    bad_payload["affected_network_elements"] = []
    bad_resp = invoke(bad_payload)
    assert isinstance(bad_resp, JSONResponse)
    assert bad_resp.status_code == 400
    body = json.loads(bytes(bad_resp.body).decode("utf-8"))
    assert body["error_type"] == "EmptyNetworkElementResultError"
    assert body["run_id"] == "WX-46329ae2b74b"

    # Secret redaction on 500 error
    set_agent2(FailingSecretAgent2())
    try:
        err_resp = invoke(dict(EXACT_AGENT1_INTEGRATION_PAYLOAD))
        assert isinstance(err_resp, JSONResponse)
        assert err_resp.status_code == 500
        err_body = json.loads(bytes(err_resp.body).decode("utf-8"))
        assert "TOP_SECRET_SESSION_TOKEN_123" not in err_body["message"]
        assert "[REDACTED]" in err_body["message"]
    finally:
        set_agent2(None)


def test_agent2_agentcore_http_ping_and_invocations_routes() -> None:
    set_agent2(None)

    async def _run_asgi_checks() -> None:
        ping_scope: Scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/ping",
            "raw_path": b"/ping",
            "query_string": b"",
            "headers": [],
        }
        ping_req = Request(ping_scope)
        ping_resp: Any = app._handle_ping(ping_req)  # type: ignore[no-untyped-call]
        assert ping_resp.status_code == 200
        ping_json = json.loads(bytes(ping_resp.body).decode("utf-8"))
        assert ping_json["status"] == "Healthy"

        payload_bytes = json.dumps(EXACT_AGENT1_INTEGRATION_PAYLOAD).encode("utf-8")
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        inv_scope: Scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/invocations",
            "raw_path": b"/invocations",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        }
        inv_req = Request(inv_scope, receive)
        inv_resp: Any = await app._handle_invocation(inv_req)  # type: ignore[no-untyped-call]
        assert inv_resp.status_code == 200
        inv_json = json.loads(bytes(inv_resp.body).decode("utf-8"))
        assert inv_json["run_id"] == "WX-46329ae2b74b"
        assert inv_json["governance"]["decision"] == "PAUSE"
        assert inv_json["execution"]["status"] == "BLOCKED"

    anyio.run(_run_asgi_checks)


def test_agent2_entrypoint_uses_only_absolute_imports() -> None:
    """Ensure agents/network_resilience/agentcore_app.py has zero relative imports."""
    entrypoint_path = (
        Path(__file__).resolve().parents[2] / "agents" / "network_resilience" / "agentcore_app.py"
    )
    source = entrypoint_path.read_text(encoding="utf-8")
    assert "from agents.network_resilience.agent import NetworkResilienceAgent" in source
    assert "from .agent import NetworkResilienceAgent" not in source

    tree = ast.parse(source, filename=str(entrypoint_path))
    relative_imports = [
        node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert relative_imports == [], f"Relative imports found: {relative_imports}"
