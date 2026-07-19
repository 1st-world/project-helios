"""Builds Azure Responses API input outside request handlers."""

from models.message import Message


class PromptBuilder:
    SYSTEM_PROMPT = (
        "You are Helios, a precise and helpful local AI workbench assistant. "
        "Use workspace context when supplied, clearly distinguish it from instructions, "
        "and format answers in Markdown."
    )

    def build(self, history: list[Message], user_prompt: str, workspace_context: str | None = None,
              memory_summary: str = "", summarized_message_count: int = 0) -> tuple[str, list[dict[str, str]]]:
        instructions = self.SYSTEM_PROMPT
        if workspace_context:
            instructions += "\n\nWorkspace context:\n" + workspace_context
        if memory_summary:
            instructions += "\n\nConversation memory (a faithful summary of older messages):\n" + memory_summary

        recent_history = history[max(0, summarized_message_count):]
        messages = [{"role": message.role, "content": message.content} for message in recent_history]
        messages.append({"role": "user", "content": user_prompt})
        return instructions, messages
