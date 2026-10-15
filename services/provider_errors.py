"""Classify Azure/Foundry Responses failures and expose bounded diagnostics without private payloads."""

import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from openai import APIConnectionError, APIError, APITimeoutError


ERRORS = {
    "request_failed": (
        "application", "An unexpected application error prevented completion. No automatic retry was made. Check recorded usage before trying again.",
    ),
    "provider_authentication_failed": (
        "authentication", "The provider could not authenticate this Azure connection. Check that the API key and endpoint in Connections belong to the same Azure resource. This connection uses API-key authentication.",
    ),
    "provider_permission_denied": (
        "permission", "The provider denied access. Review Azure resource access, networking rules, and resource status. Valid credentials can still be refused by these controls.",
    ),
    "provider_resource_not_found": (
        "not_found", "The provider could not find the requested resource or API route. Check the endpoint and deployment name in Connections, then verify Responses API support for this deployment in Microsoft Foundry.",
    ),
    "provider_deployment_not_found": (
        "not_found", "The provider could not find the requested deployment. Check the endpoint and deployment name in Connections.",
    ),
    "provider_throttled": (
        "throttling", "The provider throttled this request. Deployment request/token rate limits or temporary service capacity limits can cause this. Follow any supplied retry-after value; if it persists, review deployment quota allocation in Microsoft Foundry.",
    ),
    "provider_quota_exceeded": (
        "quota", "The provider reported a quota limit. Review the deployment's quota allocation and account limits in Microsoft Foundry before trying again.",
    ),
    "provider_timeout": (
        "timeout", "The provider request timed out. Its completion and billed usage may be unknown. Check recorded usage before trying again.",
    ),
    "provider_connection_failed": (
        "connection", "The connection to the provider failed. Check network access and the endpoint, and review recorded usage before trying again.",
    ),
    "provider_unavailable": (
        "unavailable", "The provider could not complete the request because of a service error or backend overload. Try again later; if it persists, check Azure Service Health.",
    ),
    "provider_invalid_request": (
        "invalid_request", "The provider rejected this request. Review the question and selected files, and verify that this deployment supports the Responses API and the submitted parameters in Microsoft Foundry.",
    ),
    "provider_context_length_exceeded": (
        "context_length", "The provider rejected this request because its input context is too large. Reduce the input or choose another deployment before sending again.",
    ),
    "provider_output_limit_reached": (
        "incomplete", "The provider stopped at its output token limit. Review user output limits in Settings and Connections or request a shorter answer before trying again.",
    ),
    "provider_content_filtered": (
        "content_filter", "The provider's content filtering policy blocked the request or response. Review the request and the deployment's content filtering configuration in Microsoft Foundry. No complete answer was saved.",
    ),
    "provider_response_incomplete": (
        "incomplete", "The provider returned an incomplete response. No complete answer was saved. Review the request and recorded usage before trying again.",
    ),
    "provider_response_empty": (
        "empty_response", "The provider completed the request without a text reply. Review the request and recorded usage before trying again.",
    ),
    "provider_stream_interrupted": (
        "interrupted", "The response stream ended before completion. No complete answer was saved, and billed usage may be unknown. Check recorded usage before trying again.",
    ),
    "provider_error": (
        "provider", "The provider could not complete the request. Review the connection, request, and recorded usage before trying again.",
    ),
}

QUOTA_CODES = {
    "insufficient_quota", "quota_exceeded", "credit_balance_exhausted",
    "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}
KNOWN_CODES = QUOTA_CODES | {
    "context_length_exceeded", "rate_limit_exceeded", "slow_down",
    "invalid_api_key", "authentication_error", "permission_denied",
    "deploymentnotfound", "model_not_found", "server_error",
    "service_unavailable_error", "server_is_overloaded", "invalid_prompt",
    "content_filter", "content_policy_violation", "image_content_policy_violation",
}
INCOMPLETE_REASONS = {"max_output_tokens", "content_filter"}
RESPONSE_STATUSES = {
    "completed", "failed", "incomplete", "cancelled", "in_progress", "queued",
}


def _member(value: object, name: str):
    """Read a machine field from SDK objects or decoded provider dictionaries."""
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def known_code(error: object) -> str | None:
    """Accept only recognized error identifiers instead of echoing arbitrary provider values."""
    code = _member(error, "code")
    if not isinstance(code, str):
        body = _member(error, "body")
        if isinstance(body, dict):
            body = body.get("error", body)
            code = _member(body, "code")
    if isinstance(code, str) and code.lower() in KNOWN_CODES:
        return code.lower()
    return None


def safe_identifier(value: object, secrets: tuple[str, ...] = ()) -> str | None:
    """Keep bounded opaque IDs and omit values containing captured connection secrets."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", value):
        return None
    if any(secret and secret in value for secret in secrets):
        return None
    return value


def retry_after(headers) -> float | None:
    """Parse supplied seconds, milliseconds, or an HTTP date without inventing a delay."""
    for name, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(name)
        if not isinstance(value, str):
            continue
        try:
            seconds = float(value) / divisor
        except ValueError:
            if name != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    continue
                seconds = max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds) and 0 <= seconds <= 2**53 - 1:
            return seconds
    return None


def provider_metadata(
    value: object = None, transport: object = None, *, secrets: tuple[str, ...] = (),
) -> dict:
    """Extract response diagnostics only from explicit SDK properties and response headers."""
    response = transport
    if response is None and isinstance(value, APIError):
        response = getattr(value, "response", None)
    headers = getattr(response, "headers", None)
    headers = headers if isinstance(headers, Mapping) else {}
    status = getattr(response, "status_code", None)
    if isinstance(value, APIError) and type(getattr(value, "status_code", None)) is int:
        status = value.status_code
    request_id = (
        getattr(value, "request_id", None) if isinstance(value, APIError)
        else getattr(value, "_request_id", None)
    )
    request_id = safe_identifier(request_id, secrets)
    for name in ("x-request-id", "apim-request-id", "x-ms-request-id"):
        request_id = request_id or safe_identifier(headers.get(name), secrets)
    return {
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "request_id": request_id,
        "retry_after_seconds": retry_after(headers),
    }


class ProviderFailure(RuntimeError):
    """Carry safe user guidance and optional diagnostics for one failed provider attempt."""

    def __init__(
        self, code: str = "provider_error", *, category: str | None = None,
        message: str | None = None, http_status: int | None = None,
        request_id: str | None = None, retry_after_seconds: float | None = None,
        response_status: str | None = None, incomplete_reason: str | None = None,
        provider_code: str | None = None,
    ) -> None:
        """Store controlled guidance separately from the provider's private exception text."""
        default_category, default_message = ERRORS.get(code, ERRORS["provider_error"])
        super().__init__(message or default_message)
        self.code, self.category = code, category or default_category
        self.http_status, self.request_id = http_status, request_id
        self.retry_after_seconds = retry_after_seconds
        self.response_status = (
            response_status if isinstance(response_status, str)
            and response_status in RESPONSE_STATUSES else None
        )
        self.incomplete_reason = (
            incomplete_reason if isinstance(incomplete_reason, str)
            and incomplete_reason in INCOMPLETE_REASONS else None
        )
        self.provider_code = (
            provider_code if isinstance(provider_code, str)
            and provider_code in KNOWN_CODES else None
        )

    def detail(self, *, phase: str = "reply") -> dict:
        """Return a stable client envelope with explicit nulls for absent diagnostics."""
        return {
            "code": self.code, "category": self.category, "message": str(self),
            "http_status": self.http_status, "request_id": self.request_id,
            "retry_after_seconds": self.retry_after_seconds,
            "response_status": self.response_status,
            "incomplete_reason": self.incomplete_reason, "phase": phase,
        }

    def record_details(self) -> dict:
        """Persist diagnostic facts without raw provider messages or unknown machine values."""
        return {
            "app_error_code": self.code, "error_category": self.category,
            "error_code": self.provider_code, "http_status": self.http_status,
            "request_id": self.request_id,
            "retry_after_seconds": self.retry_after_seconds,
            "incomplete_reason": self.incomplete_reason,
        }

    @classmethod
    def from_provider(
        cls, error: object, *, transport: object = None,
        response_status: str | None = None, incomplete_reason: str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> "ProviderFailure":
        """Classify status and known machine fields without interpreting error prose."""
        metadata = provider_metadata(error, transport, secrets=secrets)
        status, code = metadata["http_status"], known_code(error)
        if status == 401:
            result = "provider_authentication_failed"
        elif status == 403:
            result = "provider_permission_denied"
        elif status == 404:
            result = (
                "provider_deployment_not_found"
                if code in {"deploymentnotfound", "model_not_found"}
                else "provider_resource_not_found"
            )
        elif status == 429:
            result = (
                "provider_quota_exceeded" if code in QUOTA_CODES
                else "provider_throttled"
            )
        elif isinstance(error, APITimeoutError) or status == 408:
            result = "provider_timeout"
        elif isinstance(error, APIConnectionError):
            result = "provider_connection_failed"
        elif status is not None and status >= 500:
            result = "provider_unavailable"
        elif code == "context_length_exceeded":
            return ContextWindowExceeded(
                **metadata, provider_code=code, response_status=response_status,
            )
        elif code in QUOTA_CODES:
            result = "provider_quota_exceeded"
        elif code in {"rate_limit_exceeded", "slow_down"}:
            result = "provider_throttled"
        elif code in {"invalid_api_key", "authentication_error"}:
            result = "provider_authentication_failed"
        elif code == "permission_denied":
            result = "provider_permission_denied"
        elif code in {"deploymentnotfound", "model_not_found"}:
            result = "provider_deployment_not_found"
        elif incomplete_reason == "max_output_tokens":
            result = "provider_output_limit_reached"
        elif incomplete_reason == "content_filter" or code in {
            "content_filter", "content_policy_violation",
            "image_content_policy_violation",
        }:
            result = "provider_content_filtered"
        elif response_status == "incomplete":
            result = "provider_response_incomplete"
        elif code in {"server_error", "service_unavailable_error", "server_is_overloaded"}:
            result = "provider_unavailable"
        elif (status is not None and 400 <= status <= 499) or code == "invalid_prompt":
            result = "provider_invalid_request"
        else:
            result = "provider_error"
        return cls(
            result, **metadata, provider_code=code, response_status=response_status,
            incomplete_reason=incomplete_reason,
        )


class ContextWindowExceeded(ProviderFailure):
    """Preserve the existing exception contract for explicit provider context rejection."""

    def __init__(self, message: str = "", **metadata) -> None:
        """Keep context rejection guidance fixed even for legacy internal callers."""
        super().__init__("provider_context_length_exceeded", **metadata)


class ConnectionConfigurationError(RuntimeError):
    """Distinguish controlled connection guidance from unexpected preparation failures."""
