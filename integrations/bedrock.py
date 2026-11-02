"""Amazon Bedrock reasoning abstraction, runtime adapter, and deterministic fallback.

Uses the standard AWS SDK credential provider chain via boto3 without ever hardcoding
credentials, session tokens, or model IDs in business logic.
"""

import json
import os
import re
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import boto3
import botocore.httpsession
import certifi
import structlog
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from agents.network_resilience.prompts import (
    AGENT2_SYSTEM_PROMPT,
    build_reasoning_user_prompt,
)
from common.config import Settings
from common.exceptions import ApplicationError, IntegrationError
from common.logging import log_agent_operation
from contracts.recommendation import (
    BedrockReasoningContext,
    BedrockReasoningOutput,
    BedrockReasoningTrace,
)

# Redact only actual secret assignments or key patterns while preserving safe AWS error codes
# like "AccessDeniedException" or "UnrecognizedClientException"
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(aws_access_key_id|aws_secret_access_key|aws_session_token|password|secret|token|credential)"
    r"\s*[:=]\s*([^\s,;'\"]+)"
)
_AWS_KEY_ID_RE = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")


def _sanitize_error_message(message: str) -> str:
    sanitized = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", message)
    sanitized = _AWS_KEY_ID_RE.sub("[REDACTED_AWS_KEY_ID]", sanitized)
    return sanitized[:400]


RECOVERABLE_BEDROCK_ERRORS = (
    ApplicationError,
    ValidationError,
    BotoCoreError,
    ClientError,
    TimeoutError,
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
)


_SYSTEM_CA_CANDIDATES: tuple[str, ...] = (
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/cert.pem",
)


def resolve_ca_bundle() -> str | bool:
    """Resolve a valid TLS CA bundle path and clear broken CA env vars in container runtimes.

    Amazon Bedrock AgentCore Runtime containers may set AWS_CA_BUNDLE, REQUESTS_CA_BUNDLE, or
    SSL_CERT_FILE to a host path that does not exist inside the container, or botocore's bundled
    cacert.pem may be absent in --only-binary wheel installs, causing:
      SSLError: [Errno 2] No such file or directory
    This function checks packaged certifi, system CA paths, and Python ssl default paths,
    and pins AWS_CA_BUNDLE / SSL_CERT_FILE / REQUESTS_CA_BUNDLE to the verified file while
    keeping full SSL certificate verification enabled (never verify=False).
    """
    candidates: list[str] = []

    for env_key in ("AWS_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        env_val = os.getenv(env_key, "").strip()
        if env_val:
            if Path(env_val).is_file():
                candidates.append(env_val)
            else:
                # Remove broken CA path env var so botocore does not prioritize a missing file
                os.environ.pop(env_key, None)

    certifi_bundle = certifi.where()
    if certifi_bundle and Path(certifi_bundle).is_file():
        candidates.append(certifi_bundle)

    default_verify = ssl.get_default_verify_paths()
    for path_attr in (default_verify.cafile, default_verify.openssl_cafile):
        if path_attr and Path(path_attr).is_file():
            candidates.append(path_attr)

    for sys_path in _SYSTEM_CA_CANDIDATES:
        if Path(sys_path).is_file():
            candidates.append(sys_path)

    if candidates:
        resolved = candidates[0]
        os.environ["AWS_CA_BUNDLE"] = resolved
        os.environ["REQUESTS_CA_BUNDLE"] = resolved
        os.environ["SSL_CERT_FILE"] = resolved
        # Also override botocore.httpsession.get_cert_path so botocore always uses the verified file
        botocore.httpsession.get_cert_path = lambda _verify: resolved
        return resolved

    return True


def create_client(settings: Settings) -> Any:
    """Create a boto3 bedrock-runtime client using the default AWS credential chain.

    Uses the certifi CA bundle when available so TLS validation succeeds inside
    minimal AgentCore containers, and never disables certificate verification.
    """
    boto_config = BotocoreConfig(
        connect_timeout=settings.bedrock_timeout_seconds,
        read_timeout=settings.bedrock_timeout_seconds,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    return boto3.client(
        "bedrock-runtime",
        region_name=settings.aws_region,
        config=boto_config,
        verify=resolve_ca_bundle(),
    )


class BedrockReasoningPort(Protocol):
    """Port interface for generative network resilience explanation."""

    @property
    def model_id(self) -> str: ...

    def generate_explanation(self, context: BedrockReasoningContext) -> BedrockReasoningOutput: ...


class DeterministicFallbackReasoningAdapter:
    """Deterministic fallback reasoning generator used when Bedrock is disabled or unavailable."""

    @property
    def model_id(self) -> str:
        return "deterministic-fallback"

    def generate_explanation(self, context: BedrockReasoningContext) -> BedrockReasoningOutput:
        situation_summary = (
            f"{context.weather_event} ({context.weather_severity}) in {context.affected_region} "
            f"affects {len(context.affected_network_elements)} network element(s): "
            f"{context.affected_network_elements}."
        )
        risk_explanation = (
            f"Deterministic risk score is {context.risk_score}/100 ({context.overall_risk}), "
            f"with network_risk={context.network_risk}, service_risk={context.service_risk}, "
            f"and exposed services={context.affected_services}."
        )
        recommendation_rationale = context.deterministic_rationale
        operator_summary = (
            f"Recommended action '{context.recommended_action}' for targets "
            f"{context.target_network_elements} under deterministic governance controls."
        )
        return BedrockReasoningOutput(
            situation_summary=situation_summary,
            risk_explanation=risk_explanation,
            recommendation_rationale=recommendation_rationale,
            operator_summary=operator_summary,
        )


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from a model completion string."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch == "{":
            try:
                parsed, _ = decoder.raw_decode(text[idx:])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise IntegrationError("Bedrock response did not contain a JSON object.")
    try:
        parsed_fallback = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise IntegrationError(f"Malformed JSON in Bedrock response: {exc}") from exc
    if not isinstance(parsed_fallback, dict):
        raise IntegrationError("Bedrock JSON response root must be an object.")
    return parsed_fallback


class BedrockRuntimeAdapter:
    """Amazon Bedrock Converse / InvokeModel adapter implementing BedrockReasoningPort."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or Settings()
        if not self.settings.bedrock_model_id or not self.settings.bedrock_model_id.strip():
            raise IntegrationError(
                "BEDROCK_MODEL_ID must be configured when BedrockRuntimeAdapter is used."
            )
        self._model_id = self.settings.bedrock_model_id.strip()
        self._client = client or create_client(self.settings)

    @property
    def model_id(self) -> str:
        return self._model_id

    def _invoke_converse_or_model(self, user_prompt: str) -> str:
        """Invoke Bedrock via Converse API, with InvokeModel fallback if Converse is denied."""
        try:
            response = self._client.converse(
                modelId=self._model_id,
                system=[{"text": AGENT2_SYSTEM_PROMPT}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": user_prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": self.settings.bedrock_max_tokens,
                    "temperature": self.settings.bedrock_temperature,
                },
            )
            content_blocks = response["output"]["message"]["content"]
            return "".join(
                block.get("text", "") for block in content_blocks if isinstance(block, dict)
            )
        except ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code", "ClientError")
            err_msg = exc.response.get("Error", {}).get("Message", str(exc))
            # If the IAM role has bedrock:InvokeModel but not bedrock:Converse, try invoke_model
            if hasattr(self._client, "invoke_model") and err_code in {
                "AccessDeniedException",
                "ValidationException",
            }:
                try:
                    body_bytes = json.dumps(
                        {
                            "anthropic_version": "bedrock-2023-05-31",
                            "max_tokens": self.settings.bedrock_max_tokens,
                            "temperature": self.settings.bedrock_temperature,
                            "system": AGENT2_SYSTEM_PROMPT,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": user_prompt}],
                                }
                            ],
                        }
                    ).encode("utf-8")
                    inv_resp = self._client.invoke_model(
                        modelId=self._model_id,
                        contentType="application/json",
                        accept="application/json",
                        body=body_bytes,
                    )
                    raw_stream = inv_resp.get("body")
                    raw_body = (
                        raw_stream.read().decode("utf-8")
                        if hasattr(raw_stream, "read")
                        else str(raw_stream)
                    )
                    inv_data = json.loads(raw_body)
                    return "".join(
                        block.get("text", "")
                        for block in inv_data.get("content", [])
                        if isinstance(block, dict)
                    )
                except RECOVERABLE_BEDROCK_ERRORS:
                    pass
            safe_msg = _sanitize_error_message(f"{err_code}: {err_msg}")
            raise IntegrationError(f"Bedrock invocation failed ({err_code}): {safe_msg}") from exc
        except RECOVERABLE_BEDROCK_ERRORS as exc:
            safe_msg = _sanitize_error_message(str(exc))
            raise IntegrationError(
                f"Bedrock converse invocation failed ({type(exc).__name__}): {safe_msg}"
            ) from exc

    def generate_explanation(self, context: BedrockReasoningContext) -> BedrockReasoningOutput:
        user_prompt = build_reasoning_user_prompt(context)
        raw_text = self._invoke_converse_or_model(user_prompt)
        parsed = _extract_json_object(raw_text)
        try:
            return BedrockReasoningOutput.model_validate(parsed)
        except ValidationError as exc:
            raise IntegrationError(f"Bedrock response failed schema validation: {exc}") from exc


class ResilientBedrockReasoningService:
    """Executes BedrockReasoningPort with automatic, auditable fallback to deterministic text.

    Guarantees that any Bedrock error (auth, throttling, timeout, malformed output, disabled model)
    is caught, logged safely without credentials, and falls back to deterministic explanations
    without ever affecting governance or authorizing actions.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        primary_adapter: BedrockReasoningPort | None = None,
        fallback_adapter: BedrockReasoningPort | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.fallback_adapter: BedrockReasoningPort = (
            fallback_adapter or DeterministicFallbackReasoningAdapter()
        )
        self.primary_adapter = primary_adapter
        self.logger = structlog.get_logger("bedrock_reasoning")

    def generate_with_fallback(self, context: BedrockReasoningContext) -> BedrockReasoningTrace:
        adapter = self.primary_adapter
        if adapter is None and self.settings.bedrock_enabled:
            try:
                adapter = BedrockRuntimeAdapter(settings=self.settings)
            except RECOVERABLE_BEDROCK_ERRORS as exc:
                safe_err = _sanitize_error_message(str(exc))
                log_agent_operation(
                    self.logger,
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    agent_name="network_resilience",
                    operation="bedrock_adapter_init",
                    result="fallback",
                    reason=safe_err,
                )
                fallback_output = self.fallback_adapter.generate_explanation(context)
                return BedrockReasoningTrace(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    model_id=self.fallback_adapter.model_id,
                    operation="generate_resilience_explanation",
                    start_status="SKIPPED_INIT_ERROR",
                    result_status="FALLBACK_USED",
                    fallback_used=True,
                    error_reason=safe_err,
                    recorded_at=datetime.now(UTC),
                    reasoning=fallback_output,
                )

        if adapter is None:
            fallback_output = self.fallback_adapter.generate_explanation(context)
            return BedrockReasoningTrace(
                run_id=context.run_id,
                trace_id=context.trace_id,
                model_id=self.fallback_adapter.model_id,
                operation="generate_resilience_explanation",
                start_status="BEDROCK_DISABLED",
                result_status="FALLBACK_USED",
                fallback_used=True,
                error_reason="BEDROCK_ENABLED is false; using deterministic explanation.",
                recorded_at=datetime.now(UTC),
                reasoning=fallback_output,
            )

        try:
            output = adapter.generate_explanation(context)
            validated_output = BedrockReasoningOutput.model_validate(output.model_dump())
            log_agent_operation(
                self.logger,
                run_id=context.run_id,
                trace_id=context.trace_id,
                agent_name="network_resilience",
                operation="bedrock_generate_explanation",
                result="success",
                model_id=adapter.model_id,
                fallback_used=False,
            )
            return BedrockReasoningTrace(
                run_id=context.run_id,
                trace_id=context.trace_id,
                model_id=adapter.model_id,
                operation="generate_resilience_explanation",
                start_status="STARTED",
                result_status="SUCCEEDED",
                fallback_used=False,
                error_reason=None,
                recorded_at=datetime.now(UTC),
                reasoning=validated_output,
            )
        except RECOVERABLE_BEDROCK_ERRORS as exc:
            safe_err = _sanitize_error_message(str(exc))
            log_agent_operation(
                self.logger,
                run_id=context.run_id,
                trace_id=context.trace_id,
                agent_name="network_resilience",
                operation="bedrock_generate_explanation",
                result="fallback",
                model_id=adapter.model_id,
                fallback_used=True,
                error_type=type(exc).__name__,
                reason=safe_err,
            )
            fallback_output = self.fallback_adapter.generate_explanation(context)
            return BedrockReasoningTrace(
                run_id=context.run_id,
                trace_id=context.trace_id,
                model_id=adapter.model_id,
                operation="generate_resilience_explanation",
                start_status="STARTED",
                result_status="FALLBACK_USED",
                fallback_used=True,
                error_reason=f"{type(exc).__name__}: {safe_err}",
                recorded_at=datetime.now(UTC),
                reasoning=fallback_output,
            )
