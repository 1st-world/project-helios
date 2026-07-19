"""Azure OpenAI integration and response streaming."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from openai import APIConnectionError, APIError, AzureOpenAI, RateLimitError

from config import Settings
from models.message import Message
from services.usage_service import UsageService


logger = logging.getLogger(__name__)


class AIService:
    def __init__(self, settings: Settings, usage_service: UsageService) -> None:
        self.settings = settings
        self.usage_service = usage_service
        self.api_key = settings.azure_openai_api_key
        self.endpoint = settings.azure_openai_endpoint
        self.api_version = settings.azure_openai_api_version
        self.deployment = settings.azure_openai_deployment
        self.deployments = [self.deployment] if self.deployment else []
        self.client = None
        if settings.azure_configured:
            self._create_client()

    @property
    def configured(self) -> bool:
        return self.client is not None and bool(self.deployment)

    def _create_client(self) -> None:
        if not all((self.api_key, self.endpoint, self.api_version)):
            self.client = None
            return
        self.client = AzureOpenAI(api_key=self.api_key, azure_endpoint=self.endpoint, api_version=self.api_version)

    def configure(self, api_key: str | None, endpoint: str | None, api_version: str | None,
                  deployment: str | None, deployments: list[str] | None) -> None:
        if api_key:
            self.api_key = api_key.strip()
        if endpoint:
            self.endpoint = endpoint.strip()
        if api_version:
            self.api_version = api_version.strip()
        cleaned_deployments = [name.strip() for name in (deployments or []) if name.strip()]
        if deployment and deployment.strip() not in cleaned_deployments:
            cleaned_deployments.append(deployment.strip())
        if cleaned_deployments:
            self.deployments = list(dict.fromkeys(cleaned_deployments))
        if deployment:
            self.deployment = deployment.strip()
        elif self.deployments and not self.deployment:
            self.deployment = self.deployments[0]
        if self.deployment and self.deployment not in self.deployments:
            self.deployments.append(self.deployment)
        self._create_client()

    def connection_info(self) -> dict[str, object]:
        return {"configured": self.configured, "endpoint": self.endpoint, "api_version": self.api_version,
                "deployment": self.deployment, "deployments": self.deployments}

    async def stream(self, instructions: str, input_messages: list[dict[str, str]]) -> AsyncGenerator[dict[str, Any], None]:
        if not self.client:
            raise RuntimeError("Azure OpenAI is not configured. Add the required values to .env.")
        started = time.perf_counter()
        response_usage = None
        try:
            stream = await asyncio.to_thread(self.client.responses.create, model=self.deployment,
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
            logger.exception("Azure API failure")
            raise RuntimeError("Azure OpenAI request failed. Check logs and deployment settings.") from exc
        except Exception:
            logger.exception("Unexpected streaming interruption")
            raise

    async def summarize_memory(self, previous_summary: str, messages: list[Message]) -> str:
        """Create a compact factual memory without modifying the visible transcript."""
        if not self.client:
            raise RuntimeError("Azure OpenAI is not configured.")
        transcript = "\n".join(f"{message.role.upper()}: {message.content}" for message in messages)
        instructions = (
            "Summarize the conversation memory for a future assistant turn. Preserve user goals, "
            "decisions, constraints, important facts, unresolved questions, and relevant code or file names. "
            "Do not invent facts. Be compact and write Markdown bullet points."
        )
        request = "Existing memory:\n" + (previous_summary or "(none)") + "\n\nNew transcript to incorporate:\n" + transcript
        try:
            response = await asyncio.to_thread(self.client.responses.create, model=self.deployment,
                                               instructions=instructions, input=request)
            return response.output_text.strip()
        except (APIConnectionError, RateLimitError, APIError) as exc:
            logger.exception("Azure API failure while compacting conversation")
            raise RuntimeError("Could not compact conversation memory.") from exc
