"""AgentCore runtime integration boundary, remote runtime client, and safe error sanitization."""

import json
import re
import uuid
from typing import Any, Protocol

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.exceptions import BotoCoreError, ClientError

from common.exceptions import IntegrationError

AGENTCORE_RUNTIME_ROLE_ARN = "arn:aws:iam::835156611439:role/bb26/cohort3-agent-runtime"
DEFAULT_AGENT1_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/"
    "cohort3_weather_network_intelligence-aRoVQk71fj"
)
DEFAULT_AGENT2_RUNTIME_NAME = "cohort3_network_resilience"

_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(aws_access_key_id|aws_secret_access_key|aws_session_token|password|secret|token|credential)"
    r"\s*[:=]\s*([^\s,;'\"]+)"
)
_AWS_KEY_ID_RE = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")


def sanitize_runtime_error_message(message: str) -> str:
    """Redact credentials, tokens, or secrets from error messages before returning."""
    sanitized = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", message)
    sanitized = _AWS_KEY_ID_RE.sub("[REDACTED_AWS_KEY_ID]", sanitized)
    return sanitized[:300]


def create_agentcore_app(*, debug: bool = False) -> BedrockAgentCoreApp:
    """Create a configured Amazon Bedrock AgentCore Runtime application instance."""
    return BedrockAgentCoreApp(debug=debug)


def generate_runtime_session_id() -> str:
    """Generate a unique >=33-char session ID required by AgentCore invoke_agent_runtime."""
    return f"session-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}"


class AgentCoreInvokerPort(Protocol):
    """Port for invoking deployed Amazon Bedrock AgentCore runtimes."""

    def invoke_runtime(
        self,
        *,
        runtime_arn: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]: ...


class BotoAgentCoreRuntimeClient:
    """Invokes deployed Amazon Bedrock AgentCore runtimes via boto3 bedrock-agentcore client."""

    def __init__(
        self,
        *,
        region_name: str = "us-east-2",
        client: Any | None = None,
        control_client: Any | None = None,
    ) -> None:
        self.region_name = region_name
        self._client = client
        self._control_client = control_client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("bedrock-agentcore", region_name=self.region_name)
        return self._client

    @property
    def control_client(self) -> Any:
        if self._control_client is None:
            self._control_client = boto3.client(
                "bedrock-agentcore-control",
                region_name=self.region_name,
            )
        return self._control_client

    def discover_runtime_arn(self, runtime_name: str = DEFAULT_AGENT2_RUNTIME_NAME) -> str:
        """Look up an existing AgentCore runtime ARN by name."""
        try:
            response = self.control_client.list_agent_runtimes(maxResults=100)
            summaries = response.get("agentRuntimes", [])
            for item in summaries:
                if isinstance(item, dict) and item.get("agentRuntimeName") == runtime_name:
                    arn = item.get("agentRuntimeArn")
                    if isinstance(arn, str) and arn.strip():
                        return arn.strip()
        except (BotoCoreError, ClientError, KeyError, TypeError) as exc:
            safe_msg = sanitize_runtime_error_message(str(exc))
            raise IntegrationError(
                f"Failed to discover AgentCore runtime '{runtime_name}': {safe_msg}"
            ) from exc
        raise IntegrationError(f"AgentCore runtime '{runtime_name}' was not found.")

    def invoke_runtime(
        self,
        *,
        runtime_arn: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_session_id = session_id or generate_runtime_session_id()
        body_bytes = json.dumps(payload).encode("utf-8")
        try:
            response = self.client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=resolved_session_id,
                contentType="application/json",
                accept="application/json",
                payload=body_bytes,
            )
            stream = response.get("response") or response.get("body")
            if hasattr(stream, "read"):
                raw_bytes = stream.read()
                raw_text = (
                    raw_bytes.decode("utf-8")
                    if isinstance(raw_bytes, (bytes, bytearray))
                    else str(raw_bytes)
                )
            elif isinstance(stream, (bytes, bytearray)):
                raw_text = stream.decode("utf-8")
            elif isinstance(response, dict) and "run_id" in response:
                return response
            else:
                raw_text = str(stream or "{}")

            parsed = json.loads(raw_text)
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if not isinstance(parsed, dict):
                raise IntegrationError(
                    f"Expected JSON object from AgentCore runtime, got {type(parsed).__name__}"
                )
            return parsed
        except IntegrationError:
            raise
        except (BotoCoreError, ClientError, ValueError, KeyError, TypeError) as exc:
            safe_msg = sanitize_runtime_error_message(str(exc))
            raise IntegrationError(
                f"AgentCore invocation failed for '{runtime_arn}': {safe_msg}"
            ) from exc


__all__ = [
    "AGENTCORE_RUNTIME_ROLE_ARN",
    "DEFAULT_AGENT1_RUNTIME_ARN",
    "DEFAULT_AGENT2_RUNTIME_NAME",
    "AgentCoreInvokerPort",
    "BedrockAgentCoreApp",
    "BotoAgentCoreRuntimeClient",
    "create_agentcore_app",
    "generate_runtime_session_id",
    "sanitize_runtime_error_message",
]
