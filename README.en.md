Language: [한국어](README.md) | **English**

# Helios

Helios is a local web app that connects to Microsoft Foundry model deployments using your own API key. It stores conversations and settings locally and lets you attach selected project files when asking a model questions.

The current version is **`0.2.0`, an alpha release**. By default, chat leaves input capacity decisions to the provider instead of estimating a model's capacity and reducing or restricting input. Input budgets, output limits, and conversation memory are optional. Local file processing has separate resource limits.

## Current features

- Save multiple connection profiles and select a profile for responses.
- Stream replies with Markdown and syntax highlighting; save, rename, and delete conversations; edit messages and regenerate replies.
- Optionally connect or disconnect a Workspace and include project structure; attach text, images, PDFs, and Office files with or without a Workspace.
- Optionally configure input token budgets, output limits, conversation summaries, and one-time summary recovery.
- Record usage for replies, regenerations, and summaries; estimate costs from profile-specific prices; filter records by date and call type.
- Configure interface fonts, text size, timezone, and usage visibility.

Items marked `Planned` in `Work tools` and disabled settings are not available yet. The current UI language is English.

## Getting started

### Environment

The installation example below uses **Windows PowerShell and Python 3.12**. You need a browser and a Microsoft Foundry deployment that accepts Responses API requests with an API key. Node.js and a separate frontend build are not required.

### Install and run

Run these commands from the project root.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open [Helios in your browser](http://127.0.0.1:8000). Press `Ctrl+C` in the server terminal to stop it.

The default configuration is a local, single-user app without login or per-user authorization. Run one server process. Multiple browsers connected to it share the same profiles, conversations, and Workspace.

### Your first connection

1. Open **Settings → Connections → New profile**.
2. Enter a descriptive **Profile name**, the actual **Deployment name**, the **Foundry endpoint**, and an **API key**.
3. Select **Save changes**. The first profile becomes active automatically; use **Use** in the list to select an additional profile.
4. Send a message in chat. Basic chat does not require model capacity, pricing, or memory configuration.

Use a resource endpoint or an endpoint ending in `/openai/v1/`. This field does not accept an individual `/responses` URL or a Foundry project management URL.

```text
https://your-resource.cognitiveservices.azure.com/
https://your-resource.services.ai.azure.com/openai/v1/
```

**Deployment name means the name deployed in your resource, not the model's catalog name.** Saving a profile stores connection information locally. Actual API compatibility and permissions are checked when a request is sent.

[profiles.example.json](profiles.example.json) illustrates the storage format. Its model names, deployment names, and prices are examples; enter the information and rates for your actual deployment. You can create new connections in the UI.

## API support

| Item | Current implementation |
| --- | --- |
| Generation API | `/openai/v1/responses` for replies, regenerations, and memory summaries. |
| Authentication | Resource API key. Microsoft Entra ID authentication is not implemented yet. |
| Client | The OpenAI Python SDK configured with a Foundry endpoint. Requests use the deployment name as `model`. |
| Models and inputs | A deployment must support this API and the input formats and instructions sent to it. |
| Other APIs | Chat Completions, Anthropic Messages, and automatic API switching are not provided. |

Microsoft Foundry includes models from providers other than OpenAI. Helios does not infer a model's provider, capacity, or supported features from its name. Compatibility depends on the request contract above. Actual compatibility has not been verified across every Foundry model and input combination.

Microsoft's [v1 API and model support guide](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle#model-support) distinguishes Responses support from other v1 APIs. Check the [Microsoft Responses guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses) for your resource and deployment's API support and input requirements. Model-specific provider limits are not applied as universal app limits.

## Input, output, and memory settings

**Settings → Context & attachments** contains app defaults. **Connections → profile → Context & memory** contains profile settings. A profile can replace the app's input budget and Input check mode. For output, the smaller explicit app or profile limit applies. Saved changes apply to new requests.

### Input and output

| Setting | Fresh-install default | Meaning |
| --- | --- | --- |
| Input budget | No input limit | No local input token budget is set. |
| Input check | Warn | When an input budget applies, estimates above the selected limit produce a warning and the request continues. |
| Output limit | Provider default | `max_output_tokens` is omitted. |
| Conversation memory | Off | Original conversation history is used without automatic summaries. |
| Deployment information | Unknown | Model capacity is not inferred or looked up automatically. |

**Custom budget** sets an estimated input token allowance. **Warn** allows requests above the allowance, **Block** rejects them locally, and **Off** skips input budget checks. Explicit output limits, attachment checks, and memory settings still apply when Input check is Off.

An output limit applies to replies and summaries, including reasoning tokens. **Declared maximum output** records capacity information and is separate from a user output limit. Entering deployment information alone does not activate a chat input budget or output limit.

Policies imported from older versions may show **Review needed**. Their previous input rules remain in effect until reviewed. To use the new policy, explicitly select and save the Input budget for the relevant app or profile settings.

### Conversation memory

Memory optionally summarizes older conversation text and uses the summary in later requests in place of some original text. This makes **additional paid API calls**. Saved original messages remain intact.

1. Enter verified capacity information in the profile's **Deployment information**. You can use Declared maximum input or Declared context window.
2. Planning from a context window requires output headroom. Values are selected in this order: **Summary output headroom → effective Output limit → Declared maximum output**. Planning from maximum input alone is shown as having unverified shared input/output capacity.
3. Select **Conversation memory → On**, save, and check **Saved effective policy** for readiness. When information is missing, Helios prepares original history without automatic summaries.

App defaults for memory are:

| Setting | Default | Meaning |
| --- | --- | --- |
| Summary work budget | 262,144 tokens | Estimated input budget per summary call, also bounded by declared deployment capacity. |
| Summary trigger | Automatic | Uses 90% of the declared input allowance. `0` pauses summaries. |
| Recent history target | 65,536 tokens | Target amount of recent original text to retain. Complete turns are preserved, so this target can be exceeded. |
| Summary call limit | 8 | Maximum API calls allowed in one summary operation. |

If both a context window and maximum input are declared, memory uses the smaller input allowance. Summary output headroom is a planning allowance; it does not set an API output cap.

### Token estimation

Local estimation **does not select a model-specific tokenizer**. It uses a local `o200k_base` vocabulary file if the file passes its integrity check. Otherwise, it uses a UTF-8 byte heuristic. Request framing and image/PDF estimates are also approximate, so estimates can differ from the provider's actual token and billing usage.

To install the estimation vocabulary, run this separate setup command. It downloads and verifies a public file without calling the model API. This is not required for basic chat.

```powershell
.\.venv\Scripts\python.exe -m services.token_estimator --download
```

Normal app execution does not download the vocabulary automatically. The current estimation method appears in the profile's Saved effective policy.

### Errors and retries

Helios does not automatically reduce input, switch APIs, or retry after a provider rejection. Error notices offer ways to review the question, attachments, connection, and usage. Known usage is retained for failed, incomplete, and cancelled calls. A request with unreported usage cannot be assumed to have cost $0.

When "Summarize once & retry…" is available after a context-limit rejection, review the call scope and cost notice before explicitly starting it. The summary applies only to that retry and does not enable automatic memory. An unsuccessful regeneration does not replace the existing reply or following conversation branch.

## Workspace and attachments

The app starts and restarts with **no Workspace selected** and **project structure Off**. You can chat without a folder. No default Workspace folder is bundled or created automatically. Existing project folders and files are not connected or deleted automatically.

Use **Workspace** to select or disconnect a folder on the computer running the app. Browse opens a Tk folder dialog on that computer. If it is unavailable, enter an absolute path manually. All browsers connected to the same server process share the selected folder, and it is not retained after restart. If a task is using the folder, use **Stop** and wait for cleanup before changing or disconnecting it.

Connecting a folder validates its path only. The file list is retrieved when you open or refresh **Attach**. Subfolders render when expanded, and long lists continue with **Show more entries**. Search covers the full file list; the display page size does not limit which files you can attach. Closing Attach cancels an unfinished listing request.

Selecting a folder does not send its absolute host path, structure, or file contents to the model. Turn on structure sharing to choose a relative scope, depth, and maximum entry count, and preview what is included or omitted. The absolute path is included only when separately selected. With structure Off, explicitly attached files are still sent; paths already mentioned in saved conversation history or memory are not removed.

Use **Attach → Choose files** to pick files from the device running your browser, with or without a Workspace. Reuse retained originals under **Uploaded files**, or select project files under **Workspace files** in the same dialog. Use **Check selection → Apply selection** to review delivery and source information before applying. The check verifies local preparation, not model acceptance.

Attachments remain included with new messages until removed. Changing or disconnecting a folder removes only Workspace attachments and preserves uploads. Reopening a conversation restores its latest user message's attachments. Regeneration uses the selected message's original attachments unless you applied a selection in Attach or removed a composer attachment; then it uses your changed selection. Changed selections are checked again, and failed regeneration preserves the previous reply and its saved references.

Use **Stop upload** or close Attach to cancel an upload in progress. Completed originals stay on the device running the app; deselection, composer removal, and conversation deletion do not delete them. Use the trash button under Uploaded files for permanent deletion. Deleted or missing originals display **Unavailable** and must be selected again or removed from the selection. Explore project remains Planned.

| Format | Content sent to the model |
| --- | --- |
| UTF-8 text | The text read from the file. UTF-8 BOM is supported. |
| PNG, JPEG, WebP, static GIF | Original image input after local checks. Animated images are unsupported. |
| PDF | Original file input after local structure and page checks. Local text extraction is not required. |
| DOCX | Text from body paragraphs, tables, headers, and footers. |
| XLSX | Stored cells and formula text. Empty rectangular gaps are not counted as cells, and formulas are not executed. |
| PPTX | Text, tables, grouped shapes, and existing speaker notes from slides. |

Office attachments omit embedded images, charts, and formatting. Provide a PDF or image when visual content is needed. Encrypted documents and unsupported binary formats are rejected. The actual deployment determines image and PDF support.

### Local file limits

Adjust these in **Context & attachments → Attachment resource limits**. The provisional defaults below manage local reading and extraction; they are not model input capacities or universal provider file limits. They do not guarantee total memory use or processing time. Runtime and decoder protections may impose lower limits.

| Item | Default |
| --- | --- |
| Files per selection | 50 |
| Original bytes per file | 49,999,999 bytes |
| Combined original bytes per selection | 49,999,999 bytes |
| UTF-8 text read or extracted per file | 4,194,304 bytes (4MiB) |
| Combined expanded ZIP bytes per Office file | 134,217,728 bytes (128MiB) |
| ZIP entries per Office file | 10,000 |
| Decoded pixels per image | 50,000,000 pixels |
| Inspected pages per PDF | 1,024 |
| Stored cells per XLSX | 100,000 |

Oversized file content causes a selection error instead of being silently truncated. Extracted content, transmission serialization, and concurrent work can consume substantially more resources than the original file sizes suggest. Cancellation is cooperative; an individual parser or filesystem call may not stop immediately.

## Usage and costs

Use **Settings → Usage & costs** to inspect call records and date-based totals. Per-message and per-conversation displays are optional. Usage is based on provider-reported tokens and distinguishes missing values from `0`. Reasoning tokens are not added to output a second time.

Enter prices under **Connections → profile → Token pricing** in USD / 1M tokens. You can configure standard, cache read/write, and long-context rates, plus an input-token breakpoint. A blank standard rate is unknown; `0` means free. A blank long-context rate uses its corresponding standard rate. The default breakpoint is 272,000 tokens; adjust it to the model's actual pricing conditions.

Costs are estimates using the prices saved at call time and the reported usage. Missing required values produce partial or unavailable estimates. The app does not retrieve Azure invoices or remaining account credits. The independent usage ledger remains after a conversation is deleted.

## Local data and settings

| Location | Contents |
| --- | --- |
| `profiles.json` | Connection profiles, API keys, pricing, profile policies, and the active profile. |
| `settings.json` | App defaults for input, output, summaries, and attachments. |
| `conversation/*.json` | Saved original messages, memory summaries, and reply usage. |
| `data/attachments/` | Reusable originals and the SQLite manifest for independent uploads, retained separately from caches and rotating logs. |
| `data/work_runs.sqlite3` | Work tools foundation storage for run states, model responses, original tool results, and per-call usage references. The Explore UI remains Planned. |
| `logs/usage_calls.sqlite3` | Per-call usage, status, pricing snapshots, and the aggregation ledger. |
| `logs/summary_usage.sqlite3` | Summary usage storage, used alongside the call ledger. |
| `logs/helios.log*` | Rotating app logs. |
| `.cache/tokenizers/o200k_base.tiktoken` | Optionally installed estimation vocabulary. |
| Browser localStorage | Browser preferences such as font, text size, timezone, and usage visibility. |

Default paths are defined in [config.py](config.py). Runtime data and settings are excluded from Git. **API keys are stored as plaintext in `profiles.json`.** Saved keys are not returned to the browser in read responses. Manage profile backups with the same care as the originals.

Although app and conversation data are stored locally, replies are generated by an external API. Consider the questions, history, selected project structure, and attachment content being sent, together with the provider's data handling terms. Fonts and frontend libraries are served locally without external browser CDN requests.

Work records do not expire automatically and remain after conversation deletion. Back up conversation JSON and the run database together when they contain work context. The separate usage ledger does not import run aggregates as new API calls.

## Versions and compatibility

App versions and storage schema versions are managed independently. During alpha, they use `0.x.y` and strings in the `"0.*"` range, respectively.

### When versions change

App versions change for verified releases that group multiple commits, not for every individual commit. Git commit SHAs distinguish development revisions. A released version number is not reused for different code.

| Change unit | Version handling during alpha |
| --- | --- |
| Individual development commits; documentation, comment, and test cleanup | Keep the app version. |
| A release grouping bug fixes and small UI/UX refinements | Increment PATCH. Example: `0.2.0 → 0.2.1`. |
| A release grouping new features, significant policy changes, or incompatible alpha changes | Increment MINOR and reset PATCH to 0. Example: `0.2.x → 0.3.0`. |
| Stable release | Start at app version `1.0.0`. |

This MINOR/PATCH distinction is Helios's alpha policy. A release-preparation commit aligns the versions in `app.py` and both READMEs. Tag the verified commit with a version such as `v0.2.0`. Storage schemas change separately when their storage contract changes, not simply because an app feature was added.

### Storage and the end of alpha

The run database and optional message run references each use independent string schema `"0.1"`. New message references record `run_schema_version` and `run_id`. Existing conversations without these references remain readable without automatic rewriting. Unsupported versions are preserved without overwriting their originals.

- The current app version is **`0.2.0`**, and the settings/profile storage schema is **the string `"0.1"`**. It is not a JSON decimal: `"0.1"` and `"0.10"` are distinct identifiers.
- The independent attachment manifest and optional message attachment references each use string schema `"0.1"`. Existing conversations without attachments remain readable; new references record `attachment_schema_version` and `attachment_sources`. Unsupported attachment formats are preserved without overwriting or automatic migration.
- Existing formats without a version field are identified internally as `0`. Their original bytes are backed up before the first explicit save. The migration record's `from_schema_version: 0` and the backup name's `v0` identify that origin; new documents do not write a numeric `schema_version: 0`.
- Invalid or unsupported settings/profile versions produce an error without overwriting the originals. Preserve the affected files and backups while investigating.
- Alpha features and formats are not a permanent compatibility contract for later stages. **At the end of alpha, including a transition to beta**, support ends for alpha legacy migration code, deprecated API field aliases, and unreviewed legacy input policies. Normal execution after alpha will not automatically interpret or recover alpha legacy formats.
- If final-alpha data needs migration, define a separate procedure or tool when preparing to end alpha. Ending compatibility does not mean deleting original files or backups. Compatibility code will be removed as part of that transition; the current alpha preserves its existing compatibility and data protections.
- App/schema versions and accepted data formats for beta will be decided before the transition. Beta may precede the stable release. Stable versions start at app **`1.0.0`** and storage schema **`"1.0"`**.

## Development and structure

| Path | Responsibility |
| --- | --- |
| `app.py` | Web pages and APIs, service wiring, and request/stream lifecycle. |
| `models/` | Profile, app settings, conversation, and message models. |
| `services/` | Responses calls, input policies, summaries, attachments, Workspace, persistence, and usage. |
| `templates/index.html` | Web UI structure. |
| `static/` | Browser modules, styles, fonts, and bundled libraries. |

Follow [AGENTS.md](AGENTS.md) for development guidelines. The [frontend asset inventory](static/vendor/README.md) records bundled assets, original licenses, and update instructions.

For automatic reload during development, run:

```powershell
.\.venv\Scripts\python.exe app.py
```

### Local MCP project-reading foundation

The development `ProjectMCPClient` calls the bundled stdio server's `list_files`, `search_text`, and `read_file` for a selected Workspace. It pins the official Python MCP SDK to `2.3.0` and protocol `2026-07-28`. Chat orchestration is not connected yet; **Explore project remains Planned**. Ordinary chat, Settings, and Attach do not start an MCP server.

Listings return direct children only; searches use case-sensitive literal UTF-8 text. Partial reads return relative paths, lines, Unicode columns, original byte ranges, and a metadata-based file revision. Long lines are fragmented and original newlines are preserved. Follow `next_cursor`, including empty progress pages. `complete` means traversal of the requested scope finished; omission counts still disclose excluded paths and unreadable files. It does not promise that the entire project was read.

Automatic reading applies ancestor and nested `.gitignore` rules. Hidden files, symbolic links, junctions, hard links, and host-selected Helios credentials, backups, and runtime stores are excluded without a model-controlled override, including through Windows 8.3 aliases. This application-level path policy is separate from explicit attachments and is not an OS sandbox. Detected changes to open files, directories, or ignore rules invalidate their continuations. A file revision is neither a content hash nor a filesystem snapshot.

Ignore patterns are case-sensitive. Global Git configuration, global ignore files, and `.git/info/exclude` are not read.

Provisional local resource limits are 64KiB per result, 256KiB per stdio message, 2KiB text fragments, and 1MiB/2,048 steps/a cooperative 5 seconds of work per page. Individual filesystem calls can take longer; the host stops the session after an 8-second call timeout. Pages allow up to 256 listing entries, 128 search matches, or 128 read fragments, with traversal depth 32 and 16 cursors per session expiring after 5 idle minutes. Each `.gitignore` must be readable UTF-8 within 64KiB or the operation fails. These are not model token limits and do not change attachment limits. Cancellation, timeouts, protocol failures, and session closure clean the process tree and discard cursors. Windows startup fails if a Job Object cannot be obtained. Arbitrary commands and external MCP servers are not accepted.

### Independent attachment API

The backend API shares the preparation path used by the browser Attach dialog. Send original bytes as the body of `POST /api/attachments/uploads?filename=notes.txt` to receive an app-issued UUID and `reference`. The filename is a display basename; the API does not accept server paths or directories. Receiving and validation share existing attachment resource limits, with cleanup after failed or cancelled uploads.

Pass the returned reference in `attachment_sources` to `POST /api/attachments/inspect` or `POST /api/chat`.

```json
{"attachment_sources":[{"source":"upload","attachment_id":"<app-issued UUID>"}]}
```

A Workspace reference is `{"source":"workspace","path":"notes.txt"}`; inspection also returns its Workspace revision and session ID. Both sources share file count, total byte, and format processing policies. Legacy inspection `paths` and chat `attachment_files`, `workspace_file`, and `workspace_files` remain supported and require a selected Workspace.

`GET /api/attachments/uploads` lists retained originals and their availability; `GET /api/attachments/uploads/{id}` returns metadata for an available original. Originals do not expire automatically and remain reusable after deselection or conversation deletion. Delete them explicitly with `DELETE /api/attachments/uploads/{id}`. In-use originals return `409`; deleted or missing IDs return `410` with a reselection instruction. Back up the originals and manifest together.

Chat saves new references on the user message. Regeneration restores them when `attachment_sources` is omitted; `[]` clears the selection. Changed references are saved atomically with a successful replacement reply. Legacy path fields continue using the current request selection for regeneration. One-time recovery reuses the failed request's prepared payload, which is discarded if its original is deleted. Image/PDF bytes and extracted bodies are not copied into conversation JSON.

## Features under consideration

The following are not current features. Their scope and implementation order will be decided separately.

- MCP-based Work tools for finding relevant project files, proposing/reviewing/applying changes, and running commands.
- Web search, code execution through Sandbox/Docker, tools that read relevant file ranges, and execution resource management.
- Optional Microsoft Entra ID authentication, Foundry deployment/model discovery, and deployment information autofill.

Entra ID and model discovery are not prerequisites for basic chat. Automatically filling deployment information also remains separate from the user's choice to enable memory or token limits.
