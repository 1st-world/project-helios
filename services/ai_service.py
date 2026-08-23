"""Azure OpenAI integration with dynamic ConnectionProfile resolution."""

import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from models.message import Message
from models.profile import ConnectionProfile
from services.profile_service import ProfileService
from services.usage_service import UsageService

logger = logging.getLogger(__name__)


class AIService:
    def __init__(self, usage_service: UsageService, profile_service: ProfileService | None = None) -> None:
        self.usage_service = usage_service
        self.profile_service = profile_service
        self._clients: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        """Closes all cached client connections when the application shuts down."""
        for cached in self._clients.values():
            await cached["client"].close()
        self._clients.clear()

    async def _get_client_and_profile(self, profile_id: str | None = None) -> tuple[AsyncOpenAI, ConnectionProfile]:
        target_profile: ConnectionProfile | None = None
        if self.profile_service:
            if profile_id:
                target_profile = self.profile_service.get_profile(profile_id)
                if not target_profile:
                    raise RuntimeError("Connection profile not found.")
            else:
                target_profile = self.profile_service.get_active_profile()
        if not target_profile or not target_profile.is_configured:
            raise RuntimeError("No configured Azure OpenAI profile found. Please add connection details in Settings.")
        raw_endpoint = target_profile.endpoint.strip().rstrip("/")
        if raw_endpoint.endswith("/openai/v1"):
            base_url = raw_endpoint + "/"
        else:
            base_url = f"{raw_endpoint}/openai/v1/"
        cached = self._clients.get(target_profile.id)
        if cached:
            if cached["endpoint"] == target_profile.endpoint and cached["api_key"] == target_profile.api_key:
                return cached["client"], target_profile
            await cached["client"].close()
        client = AsyncOpenAI(
            api_key=target_profile.api_key,
            base_url=base_url,
        )
        self._clients[target_profile.id] = {
            "client": client,
            "endpoint": target_profile.endpoint,
            "api_key": target_profile.api_key
        }
        return client, target_profile

    @property
    def configured(self) -> bool:
        if not self.profile_service:
            return False
        active = self.profile_service.get_active_profile()
        return active.is_configured if active else False

    @property
    def active_deployment(self) -> str:
        if not self.profile_service:
            return "Not configured"
        active = self.profile_service.get_active_profile()
        return active.deployment if (active and active.is_configured) else "Not configured"

    async def stream(self, instructions: str, input_messages: list[dict[str, str]],
                     profile_id: str | None = None) -> AsyncGenerator[dict[str, Any], None]:
        client, profile = await self._get_client_and_profile(profile_id)
        started = time.perf_counter()
        response_usage = None
        try:
            stream = await client.responses.create(
                model=profile.deployment,
                instructions=instructions, 
                input=input_messages, 
                stream=True
            )
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield {"type": "delta", "text": event.delta}
                elif event.type == "response.completed":
                    response_usage = getattr(event.response, "usage", None)
            elapsed = int((time.perf_counter() - started) * 1000)
            usage_summary = self.usage_service.summarize(
                response_usage,
                elapsed,
                input_price_per_million=profile.input_price_per_million,
                output_price_per_million=profile.output_price_per_million,
                long_context_threshold=profile.long_context_threshold,
                long_input_price_per_million=profile.long_input_price_per_million,
                long_output_price_per_million=profile.long_output_price_per_million,
            ).to_dict()
            profile_summary = {
                "id": profile.id,
                "name": profile.name,
                "deployment": profile.deployment,
            }
            yield {"type": "usage", "usage": usage_summary, "profile": profile_summary}
            yield {"type": "done"}
        except (APIConnectionError, RateLimitError, APIError) as exc:
            logger.exception("Azure API failure for profile %s (%s)", profile.name, profile.id)
            raise RuntimeError(f"Azure OpenAI request failed for '{profile.name}'. Check credentials and deployment.") from exc
        except Exception:
            logger.exception("Unexpected streaming interruption")
            raise

    async def summarize_memory(self, previous_summary: str, messages: list[Message],
                               profile_id: str | None = None) -> str:
        """Create a compact factual memory without modifying the visible transcript."""
        client, profile = await self._get_client_and_profile(profile_id)
        transcript = "\n".join(f"{message.role.upper()}: {message.content}" for message in messages)
        instructions = (
            "Summarize the conversation memory for a future assistant turn. "
            "Preserve user goals, decisions, constraints, important facts, "
            "unresolved questions, and relevant code or file names. "
            "Do not invent facts or infer missing information. "
            "Be compact and write Markdown bullet points only."
        )
        request = "Existing memory:\n" + (previous_summary or "(none)") + "\n\nNew transcript to incorporate:\n" + transcript
        try:
            response = await client.responses.create(
                model=profile.deployment,
                instructions=instructions, 
                input=request
            )
            return response.output_text.strip()
        except (APIConnectionError, RateLimitError, APIError) as exc:
            logger.exception("Azure API failure while compacting conversation")
            raise RuntimeError("Could not compact conversation memory.") from exc
