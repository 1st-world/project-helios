"""Helios local AI Workbench application entry point."""

import json
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel, Field

from config import settings
from services.ai_service import AIService
from services.conversation_service import ConversationManager
from services.memory_service import ConversationMemoryService
from services.profile_service import ProfileService
from services.prompt_builder import PromptBuilder
from services.usage_service import UsageService
from services.workspace_service import WorkspaceAccessError, WorkspaceService


settings.logs_root.mkdir(parents=True, exist_ok=True)
file_handler = RotatingFileHandler(settings.logs_root / "helios.log", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[file_handler, logging.StreamHandler()])
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

profile_service = ProfileService(settings.profiles_path)
conversation_manager = ConversationManager(settings.conversations_root)
workspace_service = WorkspaceService(settings.workspace_root)
prompt_builder = PromptBuilder()
usage_service = UsageService(settings.input_price_per_million, settings.output_price_per_million)
ai_service = AIService(settings, usage_service, profile_service)
memory_service = ConversationMemoryService(ai_service, conversation_manager, settings.max_context_messages, settings.keep_recent_messages)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Helios started")
    yield
    logger.info("Helios stopped")


app = FastAPI(title="Helios AI Workbench", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class ReadFileRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    conversation_id: str | None = None
    workspace_file: str | None = None
    profile_id: str | None = None
    regenerate_message_index: int | None = Field(default=None, ge=0)


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class WorkspaceRootRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class CreateProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=2048)
    api_version: str = Field(default="2024-02-15-preview", max_length=128)
    api_key: str = Field(min_length=1, max_length=1024)
    deployment: str = Field(min_length=1, max_length=256)


class UpdateProfileRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    endpoint: str | None = Field(default=None, max_length=2048)
    api_version: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=1024)
    deployment: str | None = Field(default=None, max_length=256)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"model_name": ai_service.active_deployment})


@app.get("/api/health")
async def health():
    return {"configured": ai_service.configured, "model": ai_service.active_deployment}


@app.get("/api/profiles")
async def list_profiles():
    return profile_service.get_summary()


@app.post("/api/profiles")
async def create_profile(payload: CreateProfileRequest):
    try:
        profile = profile_service.create_profile(payload.name, payload.endpoint, payload.api_version, payload.api_key, payload.deployment)
    except Exception:
        logger.exception("Could not create profile")
        raise HTTPException(status_code=400, detail="Could not create connection profile.")
    return profile_service.get_summary()


@app.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: str, payload: UpdateProfileRequest):
    try:
        profile_service.update_profile(profile_id, payload.name, payload.endpoint, payload.api_version, payload.api_key, payload.deployment)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        logger.exception("Could not update profile %s", profile_id)
        raise HTTPException(status_code=400, detail="Could not update connection profile.")
    return profile_service.get_summary()


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    deleted = profile_service.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return profile_service.get_summary()


@app.post("/api/profiles/{profile_id}/active")
async def set_active_profile(profile_id: str):
    try:
        profile_service.set_active_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return profile_service.get_summary()


@app.get("/api/conversations")
async def conversations():
    return conversation_manager.list()


@app.post("/api/conversations")
async def create_conversation():
    return conversation_manager.create().to_dict()


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    conversation = conversation_manager.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict()


@app.patch("/api/conversations/{conversation_id}")
async def rename_conversation(conversation_id: str, payload: RenameConversationRequest):
    try:
        conversation = conversation_manager.rename(conversation_id, payload.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict(include_messages=False)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    try:
        deleted = conversation_manager.delete(conversation_id)
    except OSError:
        logger.exception("Could not delete conversation: %s", conversation_id)
        raise HTTPException(status_code=500, detail="Could not delete conversation from disk.")
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": True}


@app.patch("/api/conversations/{conversation_id}/messages/{message_index}")
async def edit_message(conversation_id: str, message_index: int, payload: EditMessageRequest):
    try:
        conversation = conversation_manager.edit_user_message(conversation_id, message_index, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict()


@app.get("/api/workspace")
async def workspace_tree():
    return {"root": str(workspace_service.root), "entries": workspace_service.tree()}


@app.put("/api/workspace/root")
async def update_workspace_root(payload: WorkspaceRootRequest):
    try:
        workspace_service.set_root(payload.path)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("Workspace root changed to %s", workspace_service.root)
    return {"root": str(workspace_service.root), "entries": workspace_service.tree()}


@app.post("/api/read-file")
async def read_file(payload: ReadFileRequest):
    try:
        return {"path": payload.path, "content": workspace_service.read_text(payload.path)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except PermissionError:
        logger.warning("Permission denied reading workspace file: %s", payload.path)
        raise HTTPException(status_code=403, detail="Permission denied.")
    except WorkspaceAccessError as exc:
        logger.warning("Workspace access rejected: %s", payload.path)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Unexpected workspace read error")
        raise HTTPException(status_code=500, detail="Could not read file.")


@app.post("/api/chat")
async def chat(payload: ChatRequest):
    if payload.regenerate_message_index is None:
        conversation = conversation_manager.get_or_create(payload.conversation_id)
        history = conversation.messages
        user_prompt = payload.prompt
    else:
        conversation = conversation_manager.get(payload.conversation_id or "")
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if payload.regenerate_message_index >= len(conversation.messages):
            raise HTTPException(status_code=400, detail="Message not found.")
        source_message = conversation.messages[payload.regenerate_message_index]
        if source_message.role != "user":
            raise HTTPException(status_code=400, detail="Only a user message can be regenerated.")
        history = conversation.messages[:payload.regenerate_message_index]
        user_prompt = source_message.content
    workspace_context = workspace_service.project_context()
    if payload.workspace_file:
        try:
            workspace_context += f"\n\nSelected file: {payload.workspace_file}\n{workspace_service.read_text(payload.workspace_file)}"
        except (FileNotFoundError, PermissionError, WorkspaceAccessError) as exc:
            raise HTTPException(status_code=400, detail=f"Cannot load workspace file: {exc}")

    instructions, input_messages = prompt_builder.build(history, user_prompt, workspace_context,
                                                         conversation.memory_summary, conversation.summarized_message_count)
    if payload.regenerate_message_index is None:
        conversation_manager.add_message(conversation, "user", user_prompt)

    async def events():
        answer = ""
        try:
            yield f"data: {json.dumps({'type': 'start', 'conversation_id': conversation.id})}\n\n"
            async for event in ai_service.stream(instructions, input_messages, profile_id=payload.profile_id):
                if event["type"] == "delta":
                    answer += event["text"]
                yield f"data: {json.dumps(event)}\n\n"
            conversation_manager.add_message(conversation, "assistant", answer)
            await memory_service.compact_if_needed(conversation)
        except Exception as exc:
            logger.exception("Chat stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True, reload_excludes=["logs/*", "conversation/*", "workspace/*"])
