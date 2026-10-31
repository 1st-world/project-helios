Language: ***English*** | [한국어](api.ko-kr.md)

[README](../../README.md) · [Usage](usage.md) · [Architecture and storage](architecture.md) · ***API***

# API reference

Selected backend contracts for Helios `0.4.0-alpha`. Start the local server using the README. Request bodies and current route schemas are available at `/docs`; this document adds lifecycle and authority rules. The default server has no authentication or per-user separation.

## API support

| Item | Current implementation |
| --- | --- |
| Generation API | `<resource-or-project-base>/openai/v1/responses` for replies, regenerations, and memory summaries. |
| Authentication | Resource API key. Microsoft Entra ID authentication is not implemented yet. |
| Client | The OpenAI Python SDK configured with a Foundry endpoint. Requests use the deployment name as `model`. |
| Models and inputs | A deployment must support this API and the input formats and instructions sent to it. |
| Other APIs | Chat Completions, Anthropic Messages, and automatic API switching are not provided. |

Microsoft Foundry includes models from providers other than OpenAI. Helios does not infer a model's provider, capacity, or supported features from its name. Compatibility depends on the request contract above. Actual compatibility has not been verified across every Foundry model and input combination.

Microsoft's [v1 API and model support guide](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle#model-support) distinguishes Responses support from other v1 APIs. Check the [Microsoft Responses guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses) for your resource and deployment's API support and input requirements. Model-specific provider limits are not applied as universal app limits.

## Endpoint handling

A profile Endpoint is a resource root or Foundry project API base. [AIService](../../services/ai_service.py) trims surrounding whitespace and trailing `/` characters, retains an existing `/openai/v1` suffix, and otherwise appends `/openai/v1/`. The OpenAI SDK calls `responses` beneath that base. Existing `/api/projects/<project-name>` paths are retained rather than replaced with the host root.

For a project endpoint:

```text
Endpoint: https://your-resource.services.ai.azure.com/api/projects/your-project
SDK base: https://your-resource.services.ai.azure.com/api/projects/your-project/openai/v1/
POST:     https://your-resource.services.ai.azure.com/api/projects/your-project/openai/v1/responses
```

Microsoft's [Foundry REST reference](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/aiproject) documents the project endpoint and its `{endpoint}/openai/v1/responses` route. Project endpoints should not be classified as management-only URLs. Enter an API base rather than an individual `/responses` URL or a query-bearing operation URL; authentication, model, and input support still depend on the deployment.

## Conversation model selection API

Conversation JSON stores the next response's `profile_id` and an independent `profile_revision`. Credentials and connection settings remain in the shared profile registry. A new conversation captures the active profile at creation; later active-profile changes do not retarget it. Saving a model choice leaves the transcript `version`, memory, Workspace, messages and conversation recency unchanged.

Conversation list/detail responses and `GET /api/conversations/{id}/profile` return `profile_id`, `profile_revision` and `profile_available`. Send `{"profile_id":"<profile ID>","expected_profile_revision":0}` to `PATCH /api/conversations/{id}/profile`. A changed selection advances its revision; an identical selection retains it. Stale revisions return `409 conversation_profile_changed`, and missing profiles return `404`. Failed persistence preserves the previous choice.

For an existing conversation, omitting `profile_id` from `POST /api/chat` uses its saved choice. An explicit profile is saved for that request and future responses without changing other conversations or the global active profile. Optional `expected_profile_revision` rejects stale sends before they use an unseen model or overwrite another tab's newer choice. A new conversation saves its explicit initial profile, and retries of an adopted draft keep that choice after preparation failure. Sends rejected because the conversation is busy or paused do not change the selection.

Older conversations without selection fields initialize once, during list/detail/selection reads or requests, from the last identifiable response profile. Tool responses consult their saved run metadata. If no response identifies a profile, the current active profile becomes the stored initial choice. A recorded deleted profile retains its ID with `profile_available: false`. Missing or unavailable selections reject sends with `409 conversation_profile_unavailable` instead of automatically substituting another model.

The API can update the next-response choice during active or paused work. Already started requests and one-use recovery keep their prepared profile without overwriting a newer choice. Resume keeps the current execution segment's model by default; only an explicit model-switch request uses another profile. Composer saves the conversation's choice, while the Connections default initializes new chats. The Composer can save a model choice while paused without starting work. Resume confirmation identifies the current and target models, the target host, shared context and cumulative costs. Selection is reread before and after confirmation; changed profile settings require fresh consent. Cancel keeps both the new selection and the original paused checkpoint. Connections can still change the new-chat default.

## Project exploration API

### Workspace scope and previews

Explicitly select read-only exploration with `explore_project: true` in the development `POST /api/chat` API. For a new chat, create an independent draft with `POST /api/workspace`, then select a folder with `PUT /api/workspace/root`, passing the returned `session_id` as `workspace_session_id`. The first chat request with this identity saves the connection to its conversation. For a saved conversation, query `GET /api/workspace?conversation_id=<id>&include_entries=false` and pass `conversation_id` with its current `workspace_revision` and `workspace_session_id` to chat, folder changes, context and structure previews, file reads, and attachment inspections. A Workspace is required, and its basic project overview is automatic. Additional tool exploration remains an explicit choice. Full trees, file contents, and absolute roots are not automatically inserted into model input. The temporary Workspace used by legacy API requests without identifiers is separate from browser conversations' selections.

Send the same scope fields to `POST /api/workspace/context` to preview the automatic overview locally without model calls or MCP processes. Existing `workspace_structure` and `POST /api/workspace/structure` remain available for explicit additional structure requests in the development API. Their depth, entry-count, and absolute-path options apply only to that additional request; `workspace_structure: null` does not disable the basic overview. Disconnect the Workspace to chat without project context.

The following example assumes the folder-selection response returned revision `1`. Always use that response's actual `revision` and `session_id`; selecting a folder advances the draft's revision, so the initial disconnected revision `0` is not a fixed value for connected requests.

```json
{"prompt":"Explain how this project loads settings.","explore_project":true,"workspace_revision":1,"workspace_session_id":"<current session>","exploration_limits":null}
```

### Tool protocol and input policy

Only `helios_project_list_files`, `helios_project_search_text`, and `helios_project_read_file` are registered. Multiple function calls execute serially in their original order. Subsequent requests replay model messages, function calls, encrypted reasoning state, and original tool results linked by `call_id`, excluding public reasoning summaries used for display. Intermediate response text is not merged into the final answer. Saved results and source ranges also support subsequent turns and completed-run memory summaries without rereading current files as historical evidence.

Input estimates and explicit Block mode include function definitions, exploration instructions, and accumulated results. Open runs are not summarized, and input rejection or Provider failure does not trigger automatic shrinking or retries. Exploration requests use `store: false` and request `reasoning.encrypted_content`. A returned reasoning item without reusable encrypted state stops with a compatibility error. Actual Foundry Deployment support requires separate verification; not every combination has been tested. The [OpenAI function calling documentation](https://developers.openai.com/api/docs/guides/function-calling) describes the function contract and does not establish Foundry support.

All three `exploration_limits` fields default to `null`. `max_model_calls` limits exploration response calls; `elapsed_time_target_seconds` and `cost_goal_usd` set active-time and known estimated-cost goals checked between actions. Time and cost goals are not hard deadlines for in-flight API calls or billing caps. A cost-goal run with incomplete cost information stops before another call. Initial and background conversation-memory summaries retain their existing consent, policies, and separate accounting and are outside these exploration response-call goals. Tool resources and model capacity are separate.

### Progress and accounting

SSE `start` and final `done` events include `run_id`; `work_progress` identifies phases and tool calls. `work_usage` reports individual calls and `run_usage` reports a separate aggregate. Final `done` events and tool-assisted answers in `GET /api/conversations/{id}` include display-only `response_metadata` derived from existing run and usage stores. These response-wide totals are neither persisted as single-call Message fields nor written back into the usage ledger. Errors also provide run references and known totals; unavailable storage remains unknown. Connections, prices, policies, and read scope are pinned at startup. Conversation edits/deletion, Workspace changes, and revocation of memory already used by the run stop further execution and adoption. Revocation before the first submission can rebuild original history; a run that already used the summary does not continue by substituting original history.

The additive `failure` field in run history and activity APIs stores and returns only allowed error codes, categories, phases, response states, HTTP status, request ID, and retry-after. Raw error messages, response bodies, and credentials are excluded. Optional diagnostic storage failure does not alter terminal handling or lease release. Older runs without this field use their retained state and reason for display.

### Cancellation and saved state

Request cancellation or disconnect stops work, holding the Workspace lease until MCP cleanup finishes. Regeneration creates a new run; only a successful final answer and attachment references replace the old branch. Failure and cancellation preserve it. If even terminal-state storage fails, ownership is released and the saved state is reported as unavailable; restart recovers unfinished runs as interrupted. Exploration context failures do not issue one-time summary receipts that resume incomplete runs. Review input and start a new exploration. A failed final conversation JSON write can leave a completed run without an adopted reply; it is not automatically adopted or replayed.

### Pause and resume

`POST /api/work-runs/{run_id}/pause` accepts `{ "conversation_id": "..." }` to request a safe step boundary. After cleanup, the stream ends with `paused`; final answers end with `done`. `/resume` additionally requires `expected_conversation_version` and streams the same run through SSE. `/stop` persists explicit cancellation for active or paused runs without replacing completed results. A conversation request slot and atomic checkpoint claim prevent duplicate continuations. `/api/chat` and message edits in a paused conversation return `409 work_run_paused` while preserving the checkpoint. Conversation and work-history responses include `paused_run` independently of history pagination.

Private resume input is retained in a separate `work_runs.sqlite3` table without copying connection credentials. A bounded metadata manifest checks automatic read scope and ignore policies; explicit Workspace files and upload originals are verified with SHA-256. Metadata-preserving external edits and filesystem races after verification remain possible; this is not a filesystem snapshot. Old MCP cursors are rejected as `cursor_expired` after resumption and the model is instructed to restart pagination in its new tool session.

### Execution segments and model handoff

Work storage format `0.3` binds profiles and policies to execution segments, scopes Provider identifiers to each segment, and retains switched segments' prepared input in a separate private table. Existing `0.1` and `0.2` data migrate while preserving original snapshots, content and usage links. Checkpoints bind the segment to an increasing pause revision, and every execution claim advances its ownership revision so an older request cannot submit into or terminate a newer execution. Earlier checkpoints remain resumable. Conversation message run references retain format `0.1`.

A model-handoff projection distinguishes prepared input, completed public messages, original tool results and reference ranges, and unexecuted requests. It excludes all reasoning and normalizes Provider-specific references, including earlier work embedded in prepared input, without rewriting saved source records. Unsupported references and missing results reject preparation rather than silently dropping content. Projection creates no model calls, tool execution, automatic summaries or checkpoint claims.

To continue with another model, first save that profile as the conversation's selection, then pass `target_profile_id`, `expected_profile_revision`, `expected_checkpoint_revision`, and `expected_execution_revision` together to `/resume`. The selection API supplies the profile revision; paused-run metadata supplies the checkpoint and execution revisions. Existing `conversation_id` and `expected_conversation_version` are also required. The target's connection, memory permission, input and output policies apply, with tools, handoff history and attachments included in admission. If the saved input used memory that the target does not allow, preparation rejects the switch while preserving the source boundary. Warning mode reports warnings; only explicit Block rejects estimated input. Later next-response selection changes do not retarget an accepted execution.

The switch commits atomically only after the target's first usable output and authorized tool requests are saved. Unexecuted source tools do not run before the target decides what to do. Preparation failures and interruptions before usable target output preserve the source pause and its original expiry; attempted calls, costs and active time remain cumulative. Neither model is automatically called or retried. Explicit Stop and source authority revocation discard both the candidate and source checkpoint. Completed target work is never rolled back to an earlier boundary. Final metadata identifies the actual final model and execution segments; future questions and memory summaries replay completed mixed-model history without reasoning. Work process keeps its chronological step list across model changes. Expanding Model call shows the actual model, call start time and usage through the same footer component as ordinary responses; Details expands token facts. Totals include failed attempts. Saved-result links include the segment so overlapping Provider identifiers still open the correct evidence.

Activity `segment_id` distinguishes saved results across execution segments. If response or tool identifiers overlap, query `/evidence?response_id=...&call_id=...&segment_id=...`; ambiguous references never select a segment implicitly. Completion, expiry and deletion release every segment's private input and extra attachment ownership while retaining original records and usage.

Connection validation compares actual request settings while excluding the display name. Renaming a profile preserves valid checkpoints, and the run and its resumed calls keep the name recorded at the start. Older connection fingerprints remain verifiable using their recorded original name while retaining credential, model, pricing and permission checks.

### Request cleanup status

`GET /api/chat/status?conversation_id=<id>` waits up to 10 seconds for the conversation's current request cleanup and returns `conversation_id` and `in_progress`. Before a new chat's start event, query with `workspace_session_id=<id>` to recover the draft's adopted conversation. It waits on the cleanup event without repeated polling, Provider calls, MCP execution, or folder listing. A request still marked `in_progress: true` retains ownership after the timeout. This endpoint does not cancel or resume work.

### Saved activity and evidence

`GET /api/conversations/{id}/work-runs` returns the newest 20 runs and `next_before`; pass `before` for older pages. `GET /api/conversations/{id}/work-runs/{run_id}?offset=0` returns run metadata, totals, 30 ordered steps, and `next_offset`, without raw model output. Both responses include verified current transcript indices in each run's `message_link.user_index` and `assistant_index`, with `null` for unmatched positions. New runs internally capture the question index, creation time, and content checksum to prevent incorrect links after edits or branch replacement. Its `/evidence?response_id=...&call_id=...` endpoint returns one original saved tool result. All queries require the existing owning conversation, do not resolve arbitrary file paths or cross-conversation runs, and make no Provider requests or MCP calls.

## Independent attachment API

Native UI selection uses `WS /api/attachments/picker/{token}`. Browser-supplied absolute paths are not read. Only files chosen through the owned native picker enter the existing validation and storage path, which returns ID references. The current conversation or Workspace draft determines the initial folder.

The backend API shares the preparation path used by the browser Attach dialog. Send original bytes as the body of `POST /api/attachments/uploads?filename=notes.txt` to receive an app-issued UUID and `reference`. The filename is a display basename; the API does not accept server paths or directories. Receiving and validation share existing attachment resource limits, with cleanup after failed or cancelled uploads.

Pass the returned reference in `attachment_sources` to `POST /api/attachments/inspect` or `POST /api/chat`.

```json
{"attachment_sources":[{"source":"upload","attachment_id":"<app-issued UUID>"}]}
```

A Workspace reference is `{"source":"workspace","path":"notes.txt"}`; inspection also returns its Workspace revision and session ID. Both sources share file count, total byte, and format processing policies. Legacy inspection `paths` and chat `attachment_files`, `workspace_file`, and `workspace_files` remain supported and require a selected Workspace.

`GET /api/attachments/uploads` lists retained copies and their availability; `GET /api/attachments/uploads/{id}` returns metadata for an available copy. API clients retain draft selections with `PUT /api/attachments/drafts/{UUID}` and `{"attachment_ids":["<upload UUID>"]}`, renewing within 24 hours. An empty list releases that draft's references. Optional `released_attachment_ids` also releases removed copies whose initial registration never reached the server; it does not override other owners or the retained selection. New unclaimed uploads receive a 24-hour grace period; released copies are cleaned in the background when no longer used. A draft response confirms the persisted release and does not wait for physical file deletion. `DELETE /api/attachments/uploads/{id}` explicitly deletes only unreferenced copies. Conversations, drafts, running requests, and valid retry receipts return `409` while retaining a copy; deleted or missing IDs return `410`. The UI does not expose a reusable-file library.

Chat saves new references on the user message. Regeneration restores them when `attachment_sources` is omitted; `[]` clears the selection. Changed references are saved atomically with a successful replacement reply. Legacy path fields continue using the current request selection for regeneration. One-time recovery reuses the failed request's prepared payload, which is discarded if its original is deleted. Image/PDF bytes and extracted bodies are not copied into conversation JSON.
