Language: ***English*** | [한국어](README.ko-kr.md)

# Helios

A local web app for using models deployed in Microsoft Foundry with your own API key. Save conversations, attach local files, or connect a Workspace to ask questions about a project.

The current version is **`0.4.0-alpha`**. The app UI is in English.

## Features

- **Per-conversation models**: Save multiple connection profiles and choose a model for each conversation.
- **Streaming chat**: Markdown, code highlighting, message editing, response regeneration, and conversation management.
- **Public summaries and work progress**: Follow supported models' public reasoning summaries and tool activity before the final answer.
- **Workspace and attachments**: Attach text, images, PDFs, and Office files with or without a connected folder.
- **Read-only project exploration**: Let Explore project list, search, and read relevant file ranges while retaining evidence.
- **Pause and resume**: Pause at a safe step boundary, then continue with the same model or hand off to another selected model.
- **Context and cost controls**: Optional input budgets, output limits, conversation memory, and call-level usage and cost estimates.

## Getting started

These commands use **Windows PowerShell and Python 3.12**. You need a Microsoft Foundry Deployment that supports the Responses API, its API key, and a browser. No Node.js or frontend build is required.

Run from the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. Press `Ctrl + C` in the terminal to stop the server.

### Your first connection

1. Open **Settings → Connections → New profile**.
2. Enter **Profile name**, **Deployment name**, **Foundry endpoint**, and **API key**.
3. Choose **Save changes**. The first profile becomes the new-chat default; use **Set as default** to change it.
4. Check **Conversation model** in the composer and send a message.

**Deployment name** is the name deployed in your resource, not the model catalog name. **Endpoint** is the resource or Foundry project API base used to call the model. A base ending in `/openai/v1/` is also accepted.

```text
https://your-resource.cognitiveservices.azure.com/
https://your-resource.services.ai.azure.com/openai/v1/
https://your-resource.services.ai.azure.com/api/projects/your-project
```

Saving a profile stores connection details locally; actual API compatibility is checked when a request is made. You can start ordinary chat without configuring model capacity, pricing, or memory.

## Basic usage

### Conversations and models

Create a chat or reopen one from the sidebar. Model choices are saved per conversation and apply to the next response. Changing the new-chat default leaves existing conversations' choices intact. Editing a message or regenerating an answer can replace the conversation branch from that point onward.

### Workspace and attachments

Use **Workspace** to select a folder on the computer running Helios. Connections are saved per conversation, and a folder is optional. A connected folder's name and top-level overview are included automatically with messages. Inspect **Preview context** or disconnect the folder to stop supplying future automatic context.

Hidden paths and `.gitignore` matches are not excluded by default. Select filters under **Automatic reading** or create a root `.heliosignore`. An absent file means no custom exclusions; the app does not create it automatically.

Use **Attach → Add files → Check files → Attach files** to select, inspect, and apply files. The picker opens on the computer running Helios; files inside and outside the Workspace follow the same flow. Attachments are copies, so later edits to the original file do not change content already added.

| Format | Content sent |
| --- | --- |
| UTF-8 text | File text |
| PNG, JPEG, WebP, static GIF, PDF | Original image or PDF input; the model must support that input type. |
| DOCX, XLSX, PPTX | Extracted text, tables, cells, and related text content; embedded images, charts, and formatting are omitted. |

File selection requires the native picker. Workspace also accepts a manually entered path. See the [detailed usage guide](docs/reference/usage.md) for format handling and local resource limits.

### Explore project and progress controls

Connect a Workspace, enable **Tools → Explore project**, and send your question. Permission applies once to the next response; the model uses registered read-only tools when needed. File edits and command execution are unsupported. Open **Work process** to inspect progress and saved results.

| Action | Behavior |
| --- | --- |
| **Stop** | Ends the current request while retaining incurred usage and saved work records. |
| **Pause after current step** | Pauses Work tools after the current model call or tool operation finishes. Work and costs can continue until that boundary. |
| **Resume** | Continues from the saved boundary. Choosing another model requires confirmation of the handoff. |
| **Discard run** | Discards a paused run's continuation point. |

Resume the paused run and wait for the request to end, or discard it, before sending another message in that conversation. Other chats remain available. Saved pauses last up to 24 hours and never restart paid work automatically.

## Optional settings and costs

Ordinary chat defaults to no app input budget or output cap; the Provider enforces its actual model limits. Configure app defaults under **Settings → Context & attachments**, and profile settings under **Connections → Context & memory**.

| Setting | Purpose |
| --- | --- |
| **Input budget / Input check** | Warn or block based on locally estimated input size. Estimates can differ from billed tokens. |
| **Output limit** | Sets the response output cap, including reasoning tokens. |
| **Conversation memory** | Summarizes older turns for later input. Originals remain stored, but extra paid calls require capacity information and explicit activation. |
| **Reasoning summary** | Requests public reasoning summaries from a supported Deployment. Off by default; it does not expose private reasoning text. |

Use **Settings → Usage & costs** to inspect usage. Costs are estimates from profile prices and Provider-reported usage, not invoices or remaining credits. Calls without reported usage are not treated as `0`.

See the [detailed usage guide](docs/reference/usage.md) for settings precedence, memory preparation, recovery, and pricing.

## Local data and precautions

- API keys are stored in **plaintext** in `profiles.json`. Protect settings and their backups together.
- Conversations are stored in `conversation/`, attachments and work records in `data/`, and usage and app logs in `logs/`. Deleting a conversation does not automatically erase original work records or the separate usage ledger.
- The app runs locally, but questions, history, selected context, and attachment content are sent to the external Provider to generate replies.
- The default setup is a **local single-user app** without login or separate user permissions. Run one server process.
- Alpha features and storage formats can change. Back up related data together as described in [storage and compatibility](docs/reference/architecture.md#versions-and-compatibility).

## Documentation and development

- [Detailed usage guide](docs/reference/usage.md): Settings, memory, Workspace, attachments, work results, and costs.
- [Architecture and storage](docs/reference/architecture.md): Module roles, input construction, MCP, versions, and compatibility.
- [API reference](docs/reference/api.md): Conversation models, exploration, Pause/Resume, model handoff, and attachments.
- [Shared Provider communication records](docs/SHARED-provider-records/README.md): Actual requests, responses, usage, and observation limits.
- [Development guidelines](AGENTS.md) · [Frontend assets and licenses](static/vendor/README.md).

Authentication currently uses resource API keys with Deployments supporting the Responses API. Chat Completions, Anthropic Messages, Entra ID authentication, web search, code execution, and `Planned` tools are not yet available.
