"""Manage Azure OpenAI clients, stream replies, and account for conversation summaries."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from models.message import Message
from models.profile import ConnectionProfile
from services.attachment_errors import ProviderAttachmentError
from services.context_budget import ContextWindowExceeded
from services.call_usage_store import CallUsageStore, utc_timestamp
from services.profile_service import ProfileService
from services.prompt_builder import PromptBuilder
from services.summary_usage_store import SummaryUsageStore
from services.usage_service import UsageService

logger = logging.getLogger(__name__)


class AIService:
    """Cache profile-specific clients and coordinate reply streams and summary usage."""

    def __init__(
        self,
        usage_service: UsageService,
        profile_service: ProfileService | None = None,
        *,
        summary_usage_store: SummaryUsageStore | None = None,
        call_usage_store: CallUsageStore | None = None,
        max_output_tokens: int = 4096,
    ) -> None:
        """Bind profile and usage services and initialize the client cache."""
        self.summary_usage_store = summary_usage_store or SummaryUsageStore()
        self.call_usage_store = call_usage_store or CallUsageStore()
        self.usage_service = usage_service
        self.profile_service = profile_service
        self._clients: dict[str, dict[str, Any]] = {}
        if max_output_tokens < 1:
            raise ValueError("The output token limit must be positive.")
        self.max_output_tokens = max_output_tokens

    def output_limit(self, profile: ConnectionProfile) -> int:
        """Use the configured output cap within any explicitly declared model limit."""
        return min(
            self.max_output_tokens,
            profile.model_max_output_tokens or self.max_output_tokens,
        )

    async def close(self) -> None:
        """Closes all cached client connections when the application shuts down."""
        try:
            for cached in self._clients.values():
                await cached["client"].close()
        finally:
            self._clients.clear()
            try:
                self.summary_usage_store.close()
            finally:
                self.call_usage_store.close()

    def _record_call(self, record: dict) -> None:
        """Keep accounting failures from repeating requests or discarding usable replies."""
        try:
            self.call_usage_store.record(record)
        except Exception:
            self.call_usage_store.recording_errors += 1
            logger.exception("Could not record API call usage")

    def _begin_call(
        self, profile: ConnectionProfile, kind: str,
        conversation_id: str | None,
    ) -> dict:
        """Persist request metadata and prices without credentials or prompt content."""
        record = {
            "id": str(uuid4()), "source": "live", "kind": kind,
            "conversation_id": conversation_id, "status": "in_progress",
            "profile_id": profile.id, "profile_name": profile.name,
            "deployment": profile.deployment,
            "started_at": utc_timestamp(datetime.now(timezone.utc)),
            "finished_at": None, "response_id": None, "usage": None,
            "price_snapshot": {
                "currency": "USD",
                **{name: getattr(profile, name) for name in (
                    "input_price_per_million", "output_price_per_million",
                    "long_context_threshold", "long_input_price_per_million",
                    "long_output_price_per_million",
                    "cache_read_price_per_million",
                    "cache_write_price_per_million",
                    "long_cache_read_price_per_million",
                    "long_cache_write_price_per_million",
                )},
            },
        }
        self._record_call(record)
        return record

    def _finish_call(self, record: dict, **details: object) -> None:
        """Finalize a call once, retaining provider results after consumer cancellation."""
        if record["status"] != "in_progress":
            return
        finished = datetime.now(timezone.utc)
        record.update(
            finished_at=utc_timestamp(finished),
            response_time_ms=max(0, int((
                finished - datetime.fromisoformat(record["started_at"])
            ).total_seconds() * 1000)),
            **details,
        )
        self._record_call(record)

    def _resolve_profile(
        self, profile_id: str | None = None
    ) -> ConnectionProfile:
        """Resolve a requested or active profile and reject missing connection details."""
        target_profile: ConnectionProfile | None = None
        if self.profile_service:
            if profile_id:
                target_profile = self.profile_service.get_profile(profile_id)
                if not target_profile:
                    raise RuntimeError("Connection profile not found.")
            else:
                target_profile = self.profile_service.get_active_profile()
        if not target_profile or not target_profile.is_configured:
            raise RuntimeError(
                "No configured Azure OpenAI profile found. Please add connection details in Settings."
            )
        return target_profile

    def resolve_profile_id(self, profile_id: str | None = None) -> str:
        """Pin the request's profile before streaming or background work starts."""
        return self._resolve_profile(profile_id).id

    async def _get_client_and_profile(
        self, profile_id: str | None = None
    ) -> tuple[AsyncOpenAI, ConnectionProfile]:
        """Reuse a matching client or replace it when endpoint or credentials change."""
        target_profile = self._resolve_profile(profile_id)
        raw_endpoint = target_profile.endpoint.strip().rstrip("/")
        if raw_endpoint.endswith("/openai/v1"):
            base_url = raw_endpoint + "/"
        else:
            base_url = f"{raw_endpoint}/openai/v1/"
        cached = self._clients.get(target_profile.id)
        if cached:
            if (
                cached["endpoint"] == target_profile.endpoint
                and cached["api_key"] == target_profile.api_key
            ):
                return cached["client"], target_profile
            await cached["client"].close()
        client = AsyncOpenAI(
            api_key=target_profile.api_key,
            base_url=base_url,
            max_retries=0,
        )
        self._clients[target_profile.id] = {
            "client": client,
            "endpoint": target_profile.endpoint,
            "api_key": target_profile.api_key,
        }
        return client, target_profile

    @property
    def configured(self) -> bool:
        """Return whether the active profile has all required connection details."""
        if not self.profile_service:
            return False
        active = self.profile_service.get_active_profile()
        return active.is_configured if active else False

    @property
    def active_deployment(self) -> str:
        """Return the active deployment name or its unconfigured display label."""
        if not self.profile_service:
            return "Not configured"
        active = self.profile_service.get_active_profile()
        return (
            active.deployment
            if (active and active.is_configured)
            else "Not configured"
        )

    async def stream(
        self,
        instructions: str,
        input_messages: list[dict],
        profile_id: str | None = None,
        *,
        conversation_id: str | None = None,
        call_kind: str = "chat",
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield reply deltas, usage, and completion events, surfacing provider failures."""
        client, profile = await self._get_client_and_profile(profile_id)
        profile = replace(profile)
        if call_kind not in {"chat", "regeneration"}:
            raise ValueError("Unsupported reply call kind.")
        call = self._begin_call(profile, call_kind, conversation_id)
        started = time.perf_counter()
        response_usage = None
        completed = False
        stream = None

        def usage_event(raw_usage: object | None) -> dict:
            """Normalize terminal usage against the profile snapshot used for this request."""
            return {
                "type": "usage",
                "call_id": call["id"],
                "usage": self.usage_service.summarize(
                    raw_usage,
                    int((time.perf_counter() - started) * 1000),
                    input_price_per_million=profile.input_price_per_million,
                    output_price_per_million=profile.output_price_per_million,
                    long_context_threshold=profile.long_context_threshold,
                    long_input_price_per_million=profile.long_input_price_per_million,
                    long_output_price_per_million=profile.long_output_price_per_million,
                    cache_read_price_per_million=profile.cache_read_price_per_million,
                    cache_write_price_per_million=profile.cache_write_price_per_million,
                    long_cache_read_price_per_million=profile.long_cache_read_price_per_million,
                    long_cache_write_price_per_million=profile.long_cache_write_price_per_million,
                ).to_dict(),
                "profile": {
                    "id": profile.id,
                    "name": profile.name,
                    "deployment": profile.deployment,
                },
            }

        try:
            stream = await client.responses.create(
                model=profile.deployment,
                instructions=instructions,
                input=input_messages,
                stream=True,
                max_output_tokens=self.output_limit(profile),
            )
            async for event in stream:
                if event.type == "response.created":
                    response_id = getattr(
                        getattr(event, "response", None), "id", None
                    )
                    if response_id:
                        call["response_id"] = response_id
                        self._record_call(call)
                elif event.type == "response.output_text.delta":
                    yield {"type": "delta", "text": event.delta}
                elif event.type == "response.completed":
                    completed = True
                    response_usage = getattr(event.response, "usage", None)
                    self._finish_call(
                        call, status="completed",
                        response_id=getattr(event.response, "id", None),
                        usage=usage_event(response_usage)["usage"],
                    )
                elif event.type in {"response.failed", "response.incomplete"}:
                    response = getattr(event, "response", None)
                    error = getattr(response, "error", None)
                    raw_usage = getattr(response, "usage", None)
                    reason = getattr(
                        getattr(response, "incomplete_details", None),
                        "reason", None,
                    )
                    self._finish_call(
                        call, status=event.type.removeprefix("response."),
                        response_id=getattr(response, "id", None),
                        error_code=getattr(error, "code", None),
                        incomplete_reason=reason,
                        usage=usage_event(raw_usage)["usage"],
                    )
                    if (
                        getattr(error, "code", None)
                        == "context_length_exceeded"
                    ):
                        raise ContextWindowExceeded(
                            "The model's input context limit was exceeded."
                        )
                    if raw_usage is not None:
                        terminal_usage = usage_event(raw_usage)
                        terminal_usage["response_status"] = getattr(
                            response, "status", None
                        )
                        yield terminal_usage
                    reason = getattr(
                        getattr(response, "incomplete_details", None),
                        "reason",
                        None,
                    )
                    attachment_error = ProviderAttachmentError.from_provider(
                        error, input_messages
                    )
                    if attachment_error:
                        raise attachment_error
                    raise RuntimeError(
                        "Azure OpenAI response did not complete."
                        + (f" Reason: {reason}." if reason else "")
                    )
                elif event.type == "error":
                    self._finish_call(
                        call, status="failed",
                        error_code=getattr(event, "code", None),
                    )
                    if (
                        getattr(event, "code", None)
                        == "context_length_exceeded"
                    ):
                        raise ContextWindowExceeded(
                            "The model's input context limit was exceeded."
                        )
                    attachment_error = ProviderAttachmentError.from_provider(
                        event, input_messages
                    )
                    if attachment_error:
                        raise attachment_error
                    raise RuntimeError(
                        "Azure OpenAI returned a streaming error."
                    )
            if not completed:
                self._finish_call(
                    call, status="interrupted",
                    interruption_reason="stream_ended_without_terminal_response",
                )
                raise RuntimeError(
                    "Azure OpenAI stream ended before the response completed."
                )
            final_usage = usage_event(response_usage)
            final_usage["usage"] = call["usage"]
            yield final_usage
            yield {"type": "done"}
        except (APIConnectionError, RateLimitError, APIError) as exc:
            self._finish_call(
                call, status="failed", error_code=exc.code,
                http_status=getattr(exc, "status_code", None),
            )
            if exc.code == "context_length_exceeded":
                raise ContextWindowExceeded(
                    "The model's input context limit was exceeded."
                ) from exc
            attachment_error = ProviderAttachmentError.from_provider(
                exc, input_messages
            )
            if attachment_error:
                logger.warning(
                    "Attachment request rejected for profile %s: %s",
                    profile.id, attachment_error.code,
                )
                raise attachment_error from exc
            logger.exception(
                "Azure API failure for profile %s (%s)",
                profile.name,
                profile.id,
            )
            raise RuntimeError(
                f"Azure OpenAI request failed for '{profile.name}'. Check credentials and deployment."
            ) from exc
        except (ContextWindowExceeded, ProviderAttachmentError):
            raise
        except (asyncio.CancelledError, GeneratorExit):
            self._finish_call(call, status="cancelled")
            raise
        except Exception:
            self._finish_call(call, status="failed")
            logger.exception("Unexpected streaming interruption")
            raise
        finally:
            if stream is not None and callable(getattr(stream, "close", None)):
                await stream.close()

    async def summarize_memory(
        self,
        previous_summary: str,
        messages: list[Message],
        profile_id: str | None = None,
        *,
        conversation_id: str | None = None,
    ) -> str:
        """Create a compact factual memory without modifying the visible transcript."""
        client, profile = await self._get_client_and_profile(profile_id)
        profile = replace(profile)
        instructions, request = PromptBuilder.build_memory(
            previous_summary, messages
        )
        started = time.perf_counter()
        call = self._begin_call(profile, "summary", conversation_id)
        try:
            response = await client.responses.create(
                model=profile.deployment,
                instructions=instructions,
                input=request,
                max_output_tokens=self.output_limit(profile),
            )
            # Account before validation or memory adoption, even if the caller is stale.
            raw_usage = getattr(response, "usage", None)
            usage = None
            if raw_usage is not None:
                usage = self.usage_service.summarize(
                    raw_usage,
                    int((time.perf_counter() - started) * 1000),
                    input_price_per_million=profile.input_price_per_million,
                    output_price_per_million=profile.output_price_per_million,
                    long_context_threshold=profile.long_context_threshold,
                    long_input_price_per_million=profile.long_input_price_per_million,
                    long_output_price_per_million=profile.long_output_price_per_million,
                    cache_read_price_per_million=profile.cache_read_price_per_million,
                    cache_write_price_per_million=profile.cache_write_price_per_million,
                    long_cache_read_price_per_million=profile.long_cache_read_price_per_million,
                    long_cache_write_price_per_million=profile.long_cache_write_price_per_million,
                ).to_dict()
            self._finish_call(
                call, status=response.status,
                response_id=getattr(response, "id", None), usage=usage,
                incomplete_reason=getattr(
                    getattr(response, "incomplete_details", None), "reason", None
                ),
                error_code=getattr(getattr(response, "error", None), "code", None),
            )
            try:
                self.summary_usage_store.record(
                    conversation_id=conversation_id,
                    usage_call_id=call["id"],
                    response_id=getattr(response, "id", None),
                    profile_id=profile.id,
                    deployment=profile.deployment,
                    status=response.status,
                    incomplete_reason=getattr(
                        getattr(response, "incomplete_details", None),
                        "reason",
                        None,
                    ),
                    max_output_tokens=getattr(
                        response, "max_output_tokens", None
                    ),
                    usage=usage,
                )
            except Exception:
                self.summary_usage_store.recording_errors += 1
                logger.exception("Could not record summary usage")
            if response.status != "completed":
                if (
                    getattr(getattr(response, "error", None), "code", None)
                    == "context_length_exceeded"
                ):
                    raise ContextWindowExceeded(
                        "The summary input context limit was exceeded."
                    )
                logger.warning(
                    "Conversation memory response was not completed (status=%s, reason=%s, max_output_tokens=%s)",
                    response.status,
                    getattr(
                        getattr(response, "incomplete_details", None),
                        "reason",
                        None,
                    ),
                    getattr(response, "max_output_tokens", None),
                )
                return ""
            return response.output_text.strip()
        except (APIConnectionError, RateLimitError, APIError) as exc:
            self._finish_call(
                call, status="failed", error_code=exc.code,
                http_status=getattr(exc, "status_code", None),
            )
            if exc.code == "context_length_exceeded":
                raise ContextWindowExceeded(
                    "The summary input context limit was exceeded."
                ) from exc
            logger.exception("Azure API failure while compacting conversation")
            raise RuntimeError(
                "Could not compact conversation memory."
            ) from exc
        except asyncio.CancelledError:
            self._finish_call(call, status="cancelled")
            raise
        except Exception:
            self._finish_call(call, status="failed")
            raise
