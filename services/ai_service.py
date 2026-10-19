"""Manage Foundry Responses clients, stream replies, and account for conversation summaries."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from copy import copy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from models.message import Message
from models.profile import ConnectionProfile
from services.attachment_errors import ProviderAttachmentError
from services.call_usage_store import CallUsageStore, utc_timestamp
from services.profile_service import ProfileService
from services.model_context import MemoryCapacity
from services.prompt_builder import PromptBuilder
from services.provider_errors import (
    ConnectionConfigurationError, ProviderFailure, provider_metadata,
    safe_identifier,
)
from services.response_outcome import ResponseAccumulator, ResponseOutcome
from services.summary_usage_store import SummaryUsageStore
from services.usage_service import UsageService
from services.work_run_store import WorkRunStore

logger = logging.getLogger(__name__)


class _ProfileSnapshot:
    """Expose one copied connection to a request without rereading mutable profiles."""

    def __init__(self, profile: ConnectionProfile) -> None:
        """Keep credentials, deployment declarations, and pricing fixed for one request."""
        self.profile = replace(profile)

    def get_profile(self, profile_id: str) -> ConnectionProfile | None:
        """Return the captured connection only for its pinned identifier."""
        return self.profile if self.profile.id == profile_id else None

    def get_active_profile(self) -> ConnectionProfile:
        """Return the captured connection regardless of later active selection changes."""
        return self.profile


class AIService:
    """Cache profile-specific clients and coordinate reply streams and summary usage."""

    def __init__(
        self,
        usage_service: UsageService,
        profile_service: ProfileService | None = None,
        *,
        summary_usage_store: SummaryUsageStore | None = None,
        call_usage_store: CallUsageStore | None = None,
        work_run_store: WorkRunStore | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        """Bind profile and usage services and initialize the client cache."""
        self.summary_usage_store = summary_usage_store or SummaryUsageStore()
        self.call_usage_store = call_usage_store or CallUsageStore()
        self.work_run_store = work_run_store or WorkRunStore()
        self._run_id: str | None = None
        self._before_run_call: Callable[[], None] | None = None
        self._prepare_run_call: Callable[[], tuple[str, list[dict]]] | None = None
        self.usage_service = usage_service
        self.profile_service = profile_service
        self._clients: dict[tuple[str, str, str], dict[str, Any]] = {}
        if max_output_tokens is not None and max_output_tokens < 1:
            raise ValueError("The output token limit must be positive.")
        self.max_output_tokens = max_output_tokens
        self._before_summary: Callable[[], None] | None = None
        self._prepare_reply: Callable[[], tuple[str, list[dict]]] | None = None

    def output_limit(self, profile: ConnectionProfile) -> int | None:
        """Return the smallest explicit output cap or defer to the provider."""
        limits = (
            value
            for value in (self.max_output_tokens, profile.output_token_limit)
            if value is not None
        )
        return min(limits, default=None)

    def _output_options(self, profile: ConnectionProfile) -> dict:
        """Include an SDK output limit only when an application or profile cap is set."""
        limit = self.output_limit(profile)
        return {"max_output_tokens": limit} if limit is not None else {}

    def with_output_limit(self, max_output_tokens: int | None) -> "AIService":
        """Snapshot output policy while sharing clients and persistent usage stores."""
        if max_output_tokens is not None and max_output_tokens < 1:
            raise ValueError("The output token limit must be positive.")
        snapshot = copy(self)
        snapshot.max_output_tokens = max_output_tokens
        return snapshot

    def memory_capacity(self, profile_id: str | None = None) -> MemoryCapacity:
        """Read declared capacity without a provider lookup or response-model interpretation."""
        profile = None
        if self.profile_service:
            profile = (
                self.profile_service.get_profile(profile_id)
                if profile_id else self.profile_service.get_active_profile()
            )
        if profile is None:
            return MemoryCapacity()
        return MemoryCapacity.resolve(profile, self.output_limit(profile))

    def with_profile(
        self, profile: ConnectionProfile, *,
        before_summary: Callable[[], None] | None = None,
        prepare_reply: Callable[[], tuple[str, list[dict]]] | None = None,
    ) -> "AIService":
        """Pin connection data and recheck context immediately before a Provider call."""
        snapshot = copy(self)
        snapshot.profile_service = _ProfileSnapshot(profile)
        snapshot._before_summary = before_summary
        snapshot._prepare_reply = prepare_reply
        return snapshot

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
                try:
                    self.call_usage_store.close()
                finally:
                    self.work_run_store.close()

    def with_run(
        self, run_id: str, *,
        before_call: Callable[[], None],
        prepare_call: Callable[[], tuple[str, list[dict]]] | None = None,
    ) -> "AIService":
        """Bind run state, an authority guard, and optional last-moment input preparation."""
        self.work_run_store.get(run_id)
        if not callable(before_call):
            raise ValueError("Work runs require a per-call permission guard.")
        snapshot = copy(self)
        snapshot._run_id = run_id
        snapshot._before_run_call = before_call
        snapshot._prepare_run_call = prepare_call
        snapshot._prepare_reply = None
        return snapshot

    def _record_call(self, record: dict) -> None:
        """Keep accounting failures from repeating requests or discarding usable replies."""
        try:
            self.call_usage_store.record(record)
        except Exception:
            self.call_usage_store.recording_errors += 1
            self.call_usage_store.notify_changes()
            logger.error("Could not record API call usage")

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
        if self._run_id is not None and kind != "summary":
            self.work_run_store.begin_call(
                self._run_id, record["id"], conversation_id,
            )
            record["run_id"] = self._run_id
        self._record_call(record)
        return record

    def _finish_call(
        self, record: dict, *, tolerate_run_failure: bool = False,
        **details: object,
    ) -> None:
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
        if "run_id" in record:
            try:
                self.work_run_store.end_call(
                    record["run_id"], record["id"], record["status"],
                )
            except Exception as exc:
                logger.error(
                    "Could not persist work run termination (call_id=%s, error_type=%s)",
                    record["id"], type(exc).__name__,
                )
                if not tolerate_run_failure:
                    raise RuntimeError("Work run state could not be saved.") from None

    def _record_outcome(self, call: dict, outcome: ResponseOutcome) -> None:
        """Persist protocol content only for an explicitly bound work run."""
        if "run_id" in call:
            self.work_run_store.record_outcome(
                call["run_id"], call["id"], outcome,
            )

    def _resolve_profile(
        self, profile_id: str | None = None
    ) -> ConnectionProfile:
        """Resolve a requested or active profile and reject missing connection details."""
        target_profile: ConnectionProfile | None = None
        if self.profile_service:
            if profile_id:
                target_profile = self.profile_service.get_profile(profile_id)
                if not target_profile:
                    raise ConnectionConfigurationError("Connection profile not found.")
            else:
                target_profile = self.profile_service.get_active_profile()
        if not target_profile or not target_profile.is_configured:
            raise ConnectionConfigurationError(
                "No configured Azure OpenAI profile found. Please add connection details in Settings."
            )
        return target_profile

    def resolve_profile_id(self, profile_id: str | None = None) -> str:
        """Pin the request's profile before streaming or background work starts."""
        return self._resolve_profile(profile_id).id

    async def _get_client_and_profile(
        self, profile_id: str | None = None
    ) -> tuple[AsyncOpenAI, ConnectionProfile]:
        """Reuse connection snapshots without closing clients still used by in-flight calls."""
        target_profile = self._resolve_profile(profile_id)
        raw_endpoint = target_profile.endpoint.strip().rstrip("/")
        if raw_endpoint.endswith("/openai/v1"):
            base_url = raw_endpoint + "/"
        else:
            base_url = f"{raw_endpoint}/openai/v1/"
        cache_key = (
            target_profile.id, target_profile.endpoint, target_profile.api_key,
        )
        cached = self._clients.get(cache_key)
        if cached:
            return cached["client"], target_profile
        client = AsyncOpenAI(
            api_key=target_profile.api_key,
            base_url=base_url,
            max_retries=0,
        )
        # Retain older connection clients until shutdown so edits do not abort sent requests.
        self._clients[cache_key] = {"client": client}
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

    @staticmethod
    def _provider_failure(
        error: object, profile: ConnectionProfile, inputs: list[dict], *,
        transport: object = None, response_status: str | None = None,
        incomplete_reason: str | None = None,
    ) -> ProviderFailure:
        """Classify one provider failure while retaining explicit attachment error recognition."""
        failure = ProviderFailure.from_provider(
            error, transport=transport, response_status=response_status,
            incomplete_reason=incomplete_reason,
            secrets=(profile.api_key, profile.endpoint),
        )
        if failure.code not in {"provider_invalid_request", "provider_error"}:
            return failure
        attachment = ProviderAttachmentError.from_provider(error, inputs)
        if attachment is not None:
            for name in (
                "http_status", "request_id", "retry_after_seconds",
                "response_status", "incomplete_reason", "provider_code",
            ):
                setattr(attachment, name, getattr(failure, name))
            return attachment
        return failure

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
        events = self._stream_response(
            instructions, input_messages, profile_id,
            conversation_id=conversation_id, call_kind=call_kind,
            text_only=True,
        )
        try:
            async for event in events:
                if event["type"] != "response":
                    yield event
        finally:
            await events.aclose()

    async def stream_response(
        self,
        instructions: str,
        input_messages: list[dict],
        profile_id: str | None = None,
        *,
        conversation_id: str | None = None,
        call_kind: str = "chat",
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        parallel_tool_calls: bool | None = None,
        include: list[str] | None = None,
        store: bool | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield one transient outcome, deltas, and usage without running tools."""
        options = {}
        if tools is not None:
            options["tools"] = tools
        if tool_choice is not None:
            options["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            options["parallel_tool_calls"] = parallel_tool_calls
        if include is not None:
            options["include"] = include
        if store is not None:
            options["store"] = store
        events = self._stream_response(
            instructions, input_messages, profile_id,
            conversation_id=conversation_id, call_kind=call_kind,
            response_options=options,
        )
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    async def _stream_response(
        self,
        instructions: str,
        input_messages: list[dict],
        profile_id: str | None = None,
        *,
        conversation_id: str | None = None,
        call_kind: str = "chat",
        text_only: bool = False,
        response_options: dict | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Account for one SDK stream under the selected text policy."""
        client, profile = await self._get_client_and_profile(profile_id)
        profile = replace(profile)
        if self._prepare_reply is not None and self._run_id is None:
            instructions, input_messages = self._prepare_reply()
        if self._before_run_call is not None:
            self._before_run_call()
        if self._prepare_run_call is not None:
            instructions, input_messages = self._prepare_run_call()
        if call_kind not in {"chat", "regeneration"}:
            raise ValueError("Unsupported reply call kind.")
        call = self._begin_call(profile, call_kind, conversation_id)
        started = time.perf_counter()
        response_usage = None
        completed = False
        stream = None
        transport = None
        has_text = False
        accumulator = ResponseAccumulator()
        outcome = None

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
                **self._output_options(profile),
                **(response_options or {}),
            )
            transport = getattr(stream, "response", None)
            call.update(provider_metadata(
                transport=transport, secrets=(profile.api_key, profile.endpoint),
            ))
            async for event in stream:
                accumulator.observe(event)
                if event.type == "response.created":
                    response_id = safe_identifier(
                        getattr(getattr(event, "response", None), "id", None),
                        (profile.api_key, profile.endpoint),
                    )
                    if response_id:
                        call["response_id"] = response_id
                        self._record_call(call)
                elif event.type in {
                    "response.output_text.delta", "response.refusal.delta",
                }:
                    has_text = has_text or bool(event.delta.strip())
                    yield {"type": "delta", "text": event.delta}
                elif event.type == "response.completed":
                    completed = True
                    response_usage = getattr(event.response, "usage", None)
                    self._finish_call(
                        call, status="completed",
                        response_id=safe_identifier(
                            getattr(event.response, "id", None),
                            (profile.api_key, profile.endpoint),
                        ),
                        usage=usage_event(response_usage)["usage"],
                    )
                    outcome = accumulator.finish(event.response, "completed")
                    self._record_outcome(call, outcome)
                    # Completion is final, regardless of the transport lifetime.
                    break
                elif event.type in {"response.failed", "response.incomplete"}:
                    response = getattr(event, "response", None)
                    error = getattr(response, "error", None)
                    raw_usage = getattr(response, "usage", None)
                    reason = getattr(
                        getattr(response, "incomplete_details", None),
                        "reason", None,
                    )
                    failure = self._provider_failure(
                        error, profile, input_messages, transport=transport,
                        response_status=event.type.removeprefix("response."),
                        incomplete_reason=reason,
                    )
                    self._finish_call(
                        call, status=event.type.removeprefix("response."),
                        response_id=safe_identifier(
                            getattr(response, "id", None),
                            (profile.api_key, profile.endpoint),
                        ),
                        **failure.record_details(),
                        usage=usage_event(raw_usage)["usage"],
                    )
                    outcome = accumulator.finish(
                        response, event.type.removeprefix("response."),
                    )
                    self._record_outcome(call, outcome)
                    yield {
                        "type": "response", "call_id": call["id"],
                        "outcome": outcome,
                    }
                    if raw_usage is not None:
                        terminal_usage = usage_event(raw_usage)
                        terminal_usage["usage"] = call["usage"]
                        terminal_usage["response_status"] = failure.response_status
                        yield terminal_usage
                    raise failure
                elif event.type == "error":
                    failure = self._provider_failure(
                        event, profile, input_messages, transport=transport,
                        response_status="failed",
                    )
                    self._finish_call(
                        call, status="failed", **failure.record_details(),
                    )
                    raise failure
            if not completed:
                failure = ProviderFailure(
                    "provider_stream_interrupted",
                    **provider_metadata(
                        transport=transport, secrets=(profile.api_key, profile.endpoint),
                    ),
                )
                self._finish_call(
                    call, status="interrupted",
                    interruption_reason="stream_ended_without_terminal_response",
                    **failure.record_details(),
                )
                raise failure
            final_usage = usage_event(response_usage)
            final_usage["usage"] = call["usage"]
            empty_failure = None
            if text_only and not has_text:
                empty_failure = ProviderFailure(
                    "provider_response_empty", response_status="completed",
                    **provider_metadata(
                        transport=transport, secrets=(profile.api_key, profile.endpoint),
                    ),
                )
                call.update(empty_failure.record_details())
                self._record_call(call)
            yield {"type": "response", "call_id": call["id"], "outcome": outcome}
            yield final_usage
            if empty_failure is not None:
                raise empty_failure
            yield {"type": "done"}
        except (APIConnectionError, RateLimitError, APIError) as exc:
            failure = self._provider_failure(
                exc, profile, input_messages, transport=transport,
            )
            self._finish_call(
                call, status="failed", tolerate_run_failure=True,
                **failure.record_details(),
            )
            logger.warning("Provider request failed (call_id=%s, code=%s, status=%s)", call["id"], failure.code, failure.http_status)
            raise failure from None
        except ProviderFailure as exc:
            logger.warning("Provider response failed (call_id=%s, code=%s)", call["id"], exc.code)
            raise
        except (asyncio.CancelledError, GeneratorExit):
            self._finish_call(call, status="cancelled", tolerate_run_failure=True)
            raise
        except Exception as exc:
            self._finish_call(call, status="failed")
            logger.error("Unexpected streaming interruption (call_id=%s, error_type=%s)", call["id"], type(exc).__name__)
            raise ProviderFailure() from None
        finally:
            if stream is not None and callable(getattr(stream, "close", None)):
                try:
                    await stream.close()
                except Exception as exc:
                    logger.warning("Provider stream cleanup failed (call_id=%s, error_type=%s)", call["id"], type(exc).__name__)

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
        if self._before_summary is not None:
            self._before_summary()
        instructions, request = PromptBuilder.build_memory(
            previous_summary,
            PromptBuilder(self.work_run_store, conversation_id).summary_messages(
                messages,
            ),
        )
        started = time.perf_counter()
        call = self._begin_call(profile, "summary", conversation_id)
        try:
            response = await client.responses.create(
                model=profile.deployment,
                instructions=instructions,
                input=request,
                **self._output_options(profile),
            )
            # Account before validation or memory adoption, even if the caller is stale.
            metadata = provider_metadata(
                response, secrets=(profile.api_key, profile.endpoint),
            )
            response_id = safe_identifier(
                getattr(response, "id", None), (profile.api_key, profile.endpoint),
            )
            failure = None
            if response.status != "completed":
                failure = self._provider_failure(
                    getattr(response, "error", None), profile, [],
                    response_status=response.status,
                    incomplete_reason=getattr(
                        getattr(response, "incomplete_details", None), "reason", None,
                    ),
                )
                for name, value in metadata.items():
                    setattr(failure, name, value)
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
                call,
                status=(failure.response_status or "failed") if failure else "completed",
                response_id=response_id, usage=usage,
                **(failure.record_details() if failure else metadata),
            )
            try:
                self.summary_usage_store.record(
                    conversation_id=conversation_id,
                    usage_call_id=call["id"],
                    response_id=response_id,
                    profile_id=profile.id,
                    deployment=profile.deployment,
                    status=call["status"],
                    incomplete_reason=failure.incomplete_reason if failure else None,
                    max_output_tokens=getattr(
                        response, "max_output_tokens", None
                    ),
                    usage=usage,
                )
            except Exception:
                self.summary_usage_store.recording_errors += 1
                logger.error("Could not record summary usage")
            if failure is not None:
                raise failure
            return response.output_text.strip()
        except (APIConnectionError, RateLimitError, APIError) as exc:
            failure = self._provider_failure(exc, profile, [])
            self._finish_call(
                call, status="failed", **failure.record_details(),
            )
            logger.warning("Provider summary failed (call_id=%s, code=%s, status=%s)", call["id"], failure.code, failure.http_status)
            raise failure from None
        except ProviderFailure as exc:
            logger.warning("Provider summary response failed (call_id=%s, code=%s)", call["id"], exc.code)
            raise
        except asyncio.CancelledError:
            self._finish_call(call, status="cancelled")
            raise
        except Exception as exc:
            self._finish_call(call, status="failed")
            logger.error("Unexpected summary failure (call_id=%s, error_type=%s)", call["id"], type(exc).__name__)
            raise ProviderFailure() from None
