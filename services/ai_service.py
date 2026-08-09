"""Azure OpenAI integration with dynamic ConnectionProfile resolution."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from openai import APIConnectionError, APIError, AzureOpenAI, RateLimitError

from config import Settings
from models.message import Message
from models.profile import ConnectionProfile
from services.profile_service import ProfileService
from services.usage_service import UsageService

logger = logging.getLogger(__name__)


class AIService:
    def __init__(self, settings: Settings, usage_service: UsageService, profile_service: ProfileService | None = None) -> None:
        self.settings = settings
        self.usage_service = usage_service
        self.profile_service = profile_service
        self._clients: dict[str, AzureOpenAI] = {}

    def _get_client_and_profile(self, profile_id: str | None = None) -> tuple[AzureOpenAI, ConnectionProfile]:
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

        cache_key = f"{target_profile.endpoint}|{target_profile.api_version}|{target_profile.api_key}"
        if cache_key not in self._clients:
            self._clients[cache_key] = AzureOpenAI(
                api_key=target_profile.api_key,
                azure_endpoint=target_profile.endpoint,
                api_version=target_profile.api_version,
            )
        return self._clients[cache_key], target_profile

    @property
    def configured(self) -> bool:
        try:
            _, profile = self._get_client_and_profile()
            return profile.is_configured
        except RuntimeError:
            return False

    @property
    def active_deployment(self) -> str:
        try:
            _, profile = self._get_client_and_profile()
            return profile.deployment or "Not configured"
        except RuntimeError:
            return "Not configured"

    async def stream(self, instructions: str, input_messages: list[dict[str, str]],
                     profile_id: str | None = None) -> AsyncGenerator[dict[str, Any], None]:
        client, profile = self._get_client_and_profile(profile_id)
        started = time.perf_counter()
        response_usage = None
        try:
            stream = await asyncio.to_thread(client.responses.create, model=profile.deployment,
                                             instructions=instructions, input=input_messages, stream=True)
            for event in stream:
                if event.type == "response.output_text.delta":
                    yield {"type": "delta", "text": event.delta}
                elif event.type == "response.completed":
                    response_usage = getattr(event.response, "usage", None)
            elapsed = int((time.perf_counter() - started) * 1000)
            yield {"type": "usage", "usage": self.usage_service.summarize(response_usage, elapsed).to_dict()}
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
        client, profile = self._get_client_and_profile(profile_id)
        transcript = "\n".join(f"{message.role.upper()}: {message.content}" for message in messages)
        instructions = (
            "Summarize the conversation memory for a future assistant turn. Preserve user goals, "
            "decisions, constraints, important facts, unresolved questions, and relevant code or file names. "
            "Do not invent facts. Be compact and write Markdown bullet points."
        )
        request = "Existing memory:\n" + (previous_summary or "(none)") + "\n\nNew transcript to incorporate:\n" + transcript
        try:
            response = await asyncio.to_thread(client.responses.create, model=profile.deployment,
                                               instructions=instructions, input=request)
            return response.output_text.strip()
        except (APIConnectionError, RateLimitError, APIError) as exc:
            logger.exception("Azure API failure while compacting conversation")
            raise RuntimeError("Could not compact conversation memory.") from exc
