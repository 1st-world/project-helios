"""Manage Azure OpenAI clients, stream replies, and account for conversation summaries."""

import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from models.message import Message
from models.profile import ConnectionProfile
from services.context_budget import ContextWindowExceeded
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
    ) -> None:
        """Bind profile and usage services and initialize the client cache."""
        self.summary_usage_store = summary_usage_store or SummaryUsageStore()
        self.usage_service = usage_service
        self.profile_service = profile_service
        self._clients: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        """Closes all cached client connections when the application shuts down."""
        for cached in self._clients.values():
            await cached["client"].close()
        self._clients.clear()
        self.summary_usage_store.close()

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
        input_messages: list[dict[str, str]],
        profile_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield reply deltas, usage, and completion events, surfacing provider failures."""
        client, profile = await self._get_client_and_profile(profile_id)
        profile = replace(profile)
        started = time.perf_counter()
        response_usage = None
        completed = False
        stream = None

        def usage_event(raw_usage: object | None) -> dict:
            """Normalize terminal usage against the profile snapshot used for this request."""
            return {
                "type": "usage",
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
            )
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield {"type": "delta", "text": event.delta}
                elif event.type == "response.completed":
                    completed = True
                    response_usage = getattr(event.response, "usage", None)
                elif event.type in {"response.failed", "response.incomplete"}:
                    response = getattr(event, "response", None)
                    error = getattr(response, "error", None)
                    if (
                        getattr(error, "code", None)
                        == "context_length_exceeded"
                    ):
                        raise ContextWindowExceeded(
                            "The model's input context limit was exceeded."
                        )
                    raw_usage = getattr(response, "usage", None)
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
                    raise RuntimeError(
                        "Azure OpenAI response did not complete."
                        + (f" Reason: {reason}." if reason else "")
                    )
                elif event.type == "error":
                    if (
                        getattr(event, "code", None)
                        == "context_length_exceeded"
                    ):
                        raise ContextWindowExceeded(
                            "The model's input context limit was exceeded."
                        )
                    raise RuntimeError(
                        "Azure OpenAI returned a streaming error."
                    )
            if not completed:
                raise RuntimeError(
                    "Azure OpenAI stream ended before the response completed."
                )
            yield usage_event(response_usage)
            yield {"type": "done"}
        except (APIConnectionError, RateLimitError, APIError) as exc:
            if exc.code == "context_length_exceeded":
                raise ContextWindowExceeded(
                    "The model's input context limit was exceeded."
                ) from exc
            logger.exception(
                "Azure API failure for profile %s (%s)",
                profile.name,
                profile.id,
            )
            raise RuntimeError(
                f"Azure OpenAI request failed for '{profile.name}'. Check credentials and deployment."
            ) from exc
        except ContextWindowExceeded:
            raise
        except Exception:
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
        try:
            response = await client.responses.create(
                model=profile.deployment,
                instructions=instructions,
                input=request,
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
            try:
                self.summary_usage_store.record(
                    conversation_id=conversation_id,
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
            if exc.code == "context_length_exceeded":
                raise ContextWindowExceeded(
                    "The summary input context limit was exceeded."
                ) from exc
            logger.exception("Azure API failure while compacting conversation")
            raise RuntimeError(
                "Could not compact conversation memory."
            ) from exc
