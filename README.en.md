Language: [한국어](README.md) | **English**

# Helios

Helios is a local web app that connects to Microsoft Foundry model deployments using your own API key. It stores conversations and settings locally and lets you attach selected project files when asking a model questions.

The current version is **`0.3.0-alpha`**. By default, chat leaves input capacity decisions to the provider instead of estimating a model's capacity and reducing or restricting input. Input budgets, output limits, and conversation memory are optional. Local file processing has separate resource limits.

## Current features

- Save multiple connection profiles and select a profile for responses.
- Stream replies with Markdown and syntax highlighting; save, rename, and delete conversations; edit messages and regenerate replies.
- Optionally connect or disconnect a Workspace with automatic project context; attach text, images, PDFs, and Office files with or without a Workspace.
- Let Explore project list, search, and partially read relevant files; inspect run activity, usage, and saved evidence.
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

The default configuration is a local, single-user app without login or per-user authorization. Run one server process. Multiple browsers can access the same profiles and saved conversations. Workspace connections belong to individual conversations.

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

### During response generation

From **Send**, a compact indicator shows the current phase and elapsed time. It distinguishes preparation, waiting for the model, receiving text, and finalization, and remains visible after text starts arriving until completion. Preparation can be stopped and duplicate sends are blocked. Cancelling preparation before submission restores the original question without overwriting another draft. **Stop** requests cancellation; exploration confirms its saved terminal state before showing the cancelled result.

Stopping while waiting for Workspace or attachment preparation leaves the send wait immediately while shared preparation continues independently. After submission, the UI confirms request cleanup and reloads the saved conversation before enabling input. Confirmation waits up to 10 seconds; a timeout or connection failure is disclosed while server-side duplicate-request protection remains active. A new chat stopped before its start event recovers its saved identity through the Workspace draft and restores the question. An answer saved before cancellation takes precedence.

For an ordinary response, received text and public summaries remain available as explicitly incomplete output in the current view. This temporary output is removed when reopening the conversation or sending the next question, and is excluded from saved answers and follow-up input. Cancelled regeneration preserves the previous answer. Cancellation does not subtract reported usage or costs; calls without final usage remain unknown rather than zero. The rightmost **Send** button becomes **Stop** in the same position during generation. Stop ends the request. Work tools runs also offer **Pause after current step** to its left.

Selecting Work tools first shows ordinary response progress and public summaries. A reply that finishes without a tool call keeps only Reasoning summary, with no empty section when no public summary exists. The first actual tool call promotes the view to Work process for listing, search, and read operations. **Work process** combines public summaries, model updates, and tool steps in call order. It opens by default during generation and folds once when the final answer begins; you can reopen it afterward. Progress reflects observed app events rather than estimated completion, remaining time, or private model reasoning. Elapsed time includes waiting for this request and can differ from saved response/run duration or billable time.

### Reasoning summary

For a supported deployment, choose **On** under **Settings → Connections → profile → Context & memory → Input & output → Reasoning summary** to request public reasoning summaries. Both new and existing profiles default to **Off**. Support is not inferred from deployment names or reasoning-token counts. On requests `summary: "auto"` from the provider; unsupported requests follow existing error handling without an automatic retry that removes the option. This setting does not apply to separate conversation-memory summary calls.

When a public summary arrives, **Reasoning summary** opens by default above the answer and shows its streamed content. The status, elapsed time, and disclosure share one header. It folds once when the final answer starts. A manual collapse during generation is respected, and reopening after the final-answer transition is also preserved. When a tool is actually called, Explore project combines each call’s public summaries, visible model output, tool steps, saved-result actions, and per-call usage inside **Work process** in call order. Before tool-call classification is complete, exploration model output appears in the progress record; confirmed final output moves into the answer area without duplication. Copy content includes only the answer. Long records scroll internally, and saved records start collapsed. Ordinary replies without a summary have no empty section.

Successful assistant messages store display-only `reasoning_summary` with `reasoning_summary_schema_version: "0.1"` in the conversation. This data remains separate from answer content and is excluded from follow-up questions, regeneration, conversation-memory inputs, and input estimates. Original exploration records retain public summaries, but replay context for subsequent tool calls and questions clears their `summary` text while preserving encrypted reasoning state and original tool results. Public summaries are also available in saved model activity; raw output and encrypted state are not returned to the browser. Failed or cancelled partial ordinary replies are not adopted as assistant messages, following the existing persistence policy.

Only provider-supplied `summary_text` is handled. It is not raw internal reasoning or text reconstructed from reasoning tokens. Stream finalization and terminal snapshots reconcile without duplicating content, and a summary-only response does not count as a final answer.

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

New chats start with **no Workspace selected**. You can chat without a folder. The first message saves the folder selection with its conversation. Reopening a saved conversation restores its own Workspace connection, including after restart. Older conversations without connection metadata remain folder-free.

Use **Workspace** to select or disconnect a folder on the computer running the app. **Browse** beside the **Workspace folder** heading opens a Tk folder dialog and becomes **Stop** during selection. Stop cancels only folder selection, preserving the draft path and open modal. If the native picker is unavailable, enter an absolute path manually. **Apply** saves changes only to the current conversation; other conversations' folders are unaffected. While a chat response is running, use the composer's **Stop** and wait for cleanup before changing or disconnecting its folder.

No default Workspace folder is created automatically. A saved folder that was moved, deleted, or cannot be accessed is shown as **Unavailable**. Choose an existing folder or disconnect it in Workspace. Plain chat without project input remains available.

Connecting a folder validates its path only. Preparing a message in a connected conversation automatically includes a small overview of the project name and top-level files and folders. There is no separate structure-sharing toggle. Expand **Workspace → Preview context** to inspect the overview. It includes at most 32 direct entries and 8KiB in total; bounded listing work can also leave partial coverage. It uses the same project `.gitignore`, hidden-path, link, and app-storage exclusions as Explore.

The overview refreshes for each new message and regeneration request, then stays fixed across subsequent model calls within that request. It is included in input estimation and budget checks. One-time summary recovery reuses the failed request's overview. The overview does not recursively traverse subfolders or automatically send file contents or absolute host paths. Use explicitly selected **Explore project** or file attachments for additional structure and contents. Disconnecting a folder does not remove project information already mentioned in conversation history or memory.

**Attach → Add files** opens the native file picker on the **computer running Helios**, like Workspace Browse. A connected Workspace supplies the starting folder; selecting a Workspace is optional. Files inside and outside it follow the same attachment-copy flow. File selection in the current UI requires an available native picker.

The shared notice at the top of Attach files explains the host location and attachment copies. The center of **Selected files** shows its empty state or selected-file list; its footer groups inclusion guidance, the full supported-format list, and selection feedback. **Selection check** contains the local check's scope and results.

Use **Add files** beside the **Selected files** heading, then review and remove files in one list. The dialog grows with its content, and long selections scroll within the list area, like Profiles in Settings. **Selection check** shows guidance before checking. Use **Check files → Attach files** to review delivery and check results before applying. Cancel, the close X, backdrop clicks, and Escape ask before discarding unapplied selection changes. Add files becomes **Stop** during native selection. Use it or close Attach to cancel; dismissal waits for cancellation acknowledgement before checking unapplied changes. Cancelling or failing a new selection preserves the previous selection, and an incomplete new batch is rolled back. The check verifies local preparation, not model acceptance.

Attachment copies remain included with new messages until removed. Later edits or moves of the source file and Workspace changes do not alter copies already added. Reopening a conversation restores its latest user message's attachment references. Regeneration uses that message's original attachments unless you applied a selection in Attach or removed a composer attachment; then it uses your changed selection. Changed selections are checked again, and failed regeneration preserves the previous reply and its saved references.

Workspace path references in older conversations retain their original live-file behavior. Changing or disconnecting a folder removes only those path attachments. Restored stale references display **Unavailable**; a same-named file in another folder is never substituted. Select them again or remove them.

Copies stay on the computer running the app while conversation messages, the composer, or an Attach files selection reference them. Conversation deletion, branch replacement, selection removal/discard, and request completion check only affected copies for cleanup. Attachments used by earlier messages remain after composer removal; other conversations, drafts, running requests, and valid retry receipts retain their copies. Original source files are never deleted.

Tabs renew their selections every 15 minutes and when returning to the page; unchanged selections update only their expiry. Abandoned drafts and undelivered selection results receive a 24-hour grace period. Selection releases are persisted before physical deletion runs in the background. Cleanup reconciles the full state once at startup, then runs on ownership changes and the nearest actual expiry, without scanning files every minute. Failed deletions retry only their own paths with increasing delays; clock changes and new notifications do not shorten retry intervals. Committed references use an index updated after successful transcript writes. Unreadable conversations at startup suspend automatic deletion with one logged warning; restore the affected files and restart the app to revalidate them.

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

### Explore project

Connect a Workspace and select **Tools → Explore project** to authorize read-only exploration for the next response once. The selection clears when the request starts, the Workspace changes, or you create or open another conversation. Folder selection, basic context, and attachments do not enable exploration. Regeneration and responses after message edits use the current Tools selection without restoring a previous run's permission.

**Tools → Settings** separates run limits and targets from Explore project's read scope and exclusions. Limits and targets currently apply only to Explore project. The model-call limit includes the final answer call; active time and estimated cost are targets checked between steps, not interruptions of in-flight calls or billing caps. All fields are empty by default. Only **Apply** updates request settings, which remain across new chats and Workspace changes until the page is reloaded. **Cancel**, X, Escape, and backdrop dismissal ask before discarding unapplied edits. Applying settings does not enable tools; select them separately for the next response. Settings are read-only during response generation. Memory summaries follow separate settings and accounting. **Stop** ends further work while retaining records and costs of calls already made.

**Pause after current step** lets the current model call or individual tool finish before stopping at the next boundary. Work and costs continue during **Pause requested**; a final answer that finishes first completes normally. **Paused** closes Provider/MCP connections and releases the Workspace. **Resume** reuses the original prepared input, completed results and unstarted tools in the same run, keeping model calls, costs and active time cumulative. Paused time is excluded from the time target. **Discard run** discards the continuation.

Resume explicitly revalidates the branch, connection, memory permission, Workspace, files and attachments. A paused conversation locks the message field, attachments, Tools and model selection, leaving the rightmost primary button available as **Resume**. That button is also disabled when the original question cannot be verified or resumption is unavailable. Resumption uses the existing confirmation dialog. The server rejects new turns, message edits and regeneration until the run is resumed and completed or explicitly discarded. Composer drafts are preserved; other conversations and new chats remain available. A committed pause survives app restart for up to 24 hours without automatically starting paid work. Disconnects, Provider/MCP errors and app exits during incomplete steps terminate the attempt without automatic replay or resume. Each pause is retained for 24 hours; completion, Stop, expiry, conversation deletion or confirmed invalidation releases private resume input and additional attachment ownership. Recorded work and the usage ledger remain available.

Tool-assisted answers show the model and creation time in the same footer as ordinary answers. Their usage summary covers all model calls in that response: call count, combined tokens, elapsed run time, and estimated cost. **Details** explains the breakdown and aggregate scope. Active time includes model and tool work and excludes paused intervals. Incomplete records show only known values; missing data is not treated as `0`.

**App work result** presents attempts without a final answer in independent response rows with the same width as model answers. Its label and icon identify app guidance; it is neither stored as an assistant message nor added to conversation input. Profile, run start time, usage, and **Details** share the answer footer component. The result guidance distinguishes model-call limits, time/cost targets, missing cost records, local input checks, request cancellation, app interruption, and Provider errors. After Stop, **Stopping** remains until cleanup and the saved terminal state are confirmed. Recorded activity and incurred usage remain available. Follow-up actions open the relevant limits, connection, input budget, Workspace, or usage view. **Review limits & targets** opens current applied settings; only the relevant target value appears in outcome guidance when a limit stops the attempt.

When the original question can be verified, **Prepare new request** asks for confirmation, copies it into the composer, and selects Explore project. Review the current Workspace, model, attachments, and applied limits, then press **Send** to start a new request. Preparation neither resumes the saved attempt nor sends automatically, and warns before replacing another message draft. Unmatched older records direct you to write a new request in the composer.

For runs with actual tool activity, **Work process** appears above model answers or app work result guidance. Attempts without a final answer occupy independent response rows linked to verified original questions. A failed regeneration follows the retained successful answer. Older records without a current message match remain under **Other runs**. Expand a record to page through public summaries and model updates grouped by call, tool steps, and per-call usage. Each call's **Details** is separate from the response aggregate; unadopted attempts show their profile, run start time, and run totals in the shared footer. Outcome guidance and follow-up actions are not repeated inside collapsed activity. Expanded state and loaded activity pages survive rerendering the same conversation. **Open saved result** prioritizes the file, requested and returned ranges, and whether EOF was reached or more results remain. Read fragments with continuous byte and line/column positions in the same file revision appear as one text block. Search results distinguish match positions from snippet starts, and folder results list returned files and folders. Long folder listings, search matches, multiple read ranges, and technical details each scroll within a bounded, keyboard-accessible region. **Result details** contains revisions, exact line/column/byte positions, and omissions; **Original result (JSON)** preserves the saved output. The view does not reread current files, represent partial coverage as the whole project, or fetch additional results automatically. Private model reasoning is not returned to the browser. The response-usage display preference controls response totals, unadopted run totals, and per-call usage.

Failed, cancelled, interrupted, and replaced runs remain available. Completed runs outside the current answer branch are labelled and never automatically adopted. Reopening a conversation restores its history; older runs and steps are paged. Conversation deletion does not automatically erase original run content or the separate usage ledger, but deleted conversations' activity cannot be opened through the UI/API.

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
| `data/attachments/` | Attachment copies and the SQLite manifest, retained or cleaned according to conversation, draft, and request references. |
| `data/work_runs.sqlite3` | Exploration states, model responses, original tool results, and per-call usage references. Failed and replaced runs remain stored without automatic expiration. |
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

This MINOR/PATCH distinction is Helios's alpha policy. Alpha releases use the `-alpha` suffix. A release-preparation commit aligns the versions in `app.py` and both READMEs. Tag the verified commit with a version such as `v0.3.0-alpha`. Storage schemas change separately when their storage contract changes, not simply because an app feature was added.

### Storage and the end of alpha

The run database and optional message run references each use independent string schema `"0.1"`. New message references record `run_schema_version` and `run_id`. Existing conversations without these references remain readable without automatic rewriting. Unsupported versions are preserved without overwriting their originals.

- The current app version is **`0.3.0-alpha`**, and the settings/profile storage schema is **the string `"0.1"`**. It is not a JSON decimal: `"0.1"` and `"0.10"` are distinct identifiers.
- The attachment manifest uses string schema `"0.2"`; optional message attachment references remain `"0.1"`. Existing `"0.1"` manifests upgrade transactionally with a 24-hour cleanup grace for unused copies. Conversations are not rewritten; new references record `attachment_schema_version` and `attachment_sources`. Unsupported versions are neither overwritten nor cleaned. Back up copies, the manifest, and conversation JSON together.
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

`ProjectMCPClient` calls the bundled stdio server's `list_files`, `search_text`, and `read_file` for a selected Workspace. It pins the official Python MCP SDK to `2.3.0` and protocol `2026-07-28`. Explore project UI and the explicit API below support repeated calls. Ordinary chat with tools Off, Settings, Attach, and saved exploration queries do not start an MCP server.

Listings return direct children only; searches use case-sensitive literal UTF-8 text. Partial reads return relative paths, lines, Unicode columns, original byte ranges, and a metadata-based file revision. Long lines are fragmented and original newlines are preserved. Follow `next_cursor`, including empty progress pages. `complete` means traversal of the requested scope finished; omission counts still disclose excluded paths and unreadable files. It does not promise that the entire project was read.

Automatic reading applies ancestor and nested `.gitignore` rules. Hidden files, symbolic links, junctions, hard links, and host-selected Helios credentials, backups, and runtime stores are excluded without a model-controlled override, including through Windows 8.3 aliases. This application-level path policy is separate from explicit attachments and is not an OS sandbox. Detected changes to open files, directories, or ignore rules invalidate their continuations. A file revision is neither a content hash nor a filesystem snapshot.

Ignore patterns are case-sensitive. Global Git configuration, global ignore files, and `.git/info/exclude` are not read.

Provisional local resource limits are 64KiB per result, 256KiB per stdio message, 2KiB text fragments, and 1MiB/2,048 steps/a cooperative 5 seconds of work per page. Individual filesystem calls can take longer; the host stops the session after an 8-second call timeout. Pages allow up to 256 listing entries, 128 search matches, or 128 read fragments, with traversal depth 32 and 16 cursors per session expiring after 5 idle minutes. Each `.gitignore` must be readable UTF-8 within 64KiB or the operation fails. These are not model token limits and do not change attachment limits. Cancellation, timeouts, protocol failures, and session closure clean the process tree and discard cursors. Windows startup fails if a Job Object cannot be obtained. Arbitrary commands and external MCP servers are not accepted.

### Project exploration API

Explicitly select read-only exploration with `explore_project: true` in the development `POST /api/chat` API. For a new chat, create an independent draft with `POST /api/workspace`, then select a folder with `PUT /api/workspace/root`, passing the returned `session_id` as `workspace_session_id`. The first chat request with this identity saves the connection to its conversation. For a saved conversation, query `GET /api/workspace?conversation_id=<id>&include_entries=false` and pass `conversation_id` with its current `workspace_revision` and `workspace_session_id` to chat, folder changes, context and structure previews, file reads, and attachment inspections. A Workspace is required, and its basic project overview is automatic. Additional tool exploration remains an explicit choice. Full trees, file contents, and absolute roots are not automatically inserted into model input. The temporary Workspace used by legacy API requests without identifiers is separate from browser conversations' selections.

Send the same scope fields to `POST /api/workspace/context` to preview the automatic overview locally without model calls or MCP processes. Existing `workspace_structure` and `POST /api/workspace/structure` remain available for explicit additional structure requests in the development API. Their depth, entry-count, and absolute-path options apply only to that additional request; `workspace_structure: null` does not disable the basic overview. Disconnect the Workspace to chat without project context.

```json
{"prompt":"Explain how this project loads settings.","explore_project":true,"workspace_revision":0,"workspace_session_id":"<current session>","exploration_limits":null}
```

Only `helios_project_list_files`, `helios_project_search_text`, and `helios_project_read_file` are registered. Multiple function calls execute serially in their original order. Subsequent requests replay model messages, function calls, encrypted reasoning state, and original tool results linked by `call_id`, excluding public reasoning summaries used for display. Intermediate response text is not merged into the final answer. Saved results and source ranges also support subsequent turns and completed-run memory summaries without rereading current files as historical evidence.

Input estimates and explicit Block mode include function definitions, exploration instructions, and accumulated results. Open runs are not summarized, and input rejection or Provider failure does not trigger automatic shrinking or retries. Exploration requests use `store: false` and request `reasoning.encrypted_content`. A returned reasoning item without reusable encrypted state stops with a compatibility error. Actual Foundry Deployment support requires separate verification; not every combination has been tested. The [OpenAI function calling documentation](https://developers.openai.com/api/docs/guides/function-calling) describes the function contract and does not establish Foundry support.

All three `exploration_limits` fields default to `null`. `max_model_calls` limits exploration response calls; `elapsed_time_target_seconds` and `cost_goal_usd` set active-time and known estimated-cost goals checked between actions. Time and cost goals are not hard deadlines for in-flight API calls or billing caps. A cost-goal run with incomplete cost information stops before another call. Initial and background conversation-memory summaries retain their existing consent, policies, and separate accounting and are outside these exploration response-call goals. Tool resources and model capacity are separate.

SSE `start` and final `done` events include `run_id`; `work_progress` identifies phases and tool calls. `work_usage` reports individual calls and `run_usage` reports a separate aggregate. Final `done` events and tool-assisted answers in `GET /api/conversations/{id}` include display-only `response_metadata` derived from existing run and usage stores. These response-wide totals are neither persisted as single-call Message fields nor written back into the usage ledger. Errors also provide run references and known totals; unavailable storage remains unknown. Connections, prices, policies, and read scope are pinned at startup. Conversation edits/deletion, Workspace changes, and revocation of memory already used by the run stop further execution and adoption. Revocation before the first submission can rebuild original history; a run that already used the summary does not continue by substituting original history.

The additive `failure` field in run history and activity APIs stores and returns only allowed error codes, categories, phases, response states, HTTP status, request ID, and retry-after. Raw error messages, response bodies, and credentials are excluded. Optional diagnostic storage failure does not alter terminal handling or lease release. Older runs without this field use their retained state and reason for display.

Request cancellation or disconnect stops work, holding the Workspace lease until MCP cleanup finishes. Regeneration creates a new run; only a successful final answer and attachment references replace the old branch. Failure and cancellation preserve it. If even terminal-state storage fails, ownership is released and the saved state is reported as unavailable; restart recovers unfinished runs as interrupted. Exploration context failures do not issue one-time summary receipts that resume incomplete runs. Review input and start a new exploration. A failed final conversation JSON write can leave a completed run without an adopted reply; it is not automatically adopted or replayed.

`POST /api/work-runs/{run_id}/pause` accepts `{ "conversation_id": "..." }` to request a safe step boundary. After cleanup, the stream ends with `paused`; final answers end with `done`. `/resume` additionally requires `expected_conversation_version` and streams the same run through SSE. `/stop` persists explicit cancellation for active or paused runs without replacing completed results. A conversation request slot and atomic checkpoint claim prevent duplicate continuations. `/api/chat` and message edits in a paused conversation return `409 work_run_paused` while preserving the checkpoint. Conversation and work-history responses include `paused_run` independently of history pagination.

Private resume input is retained in a separate `work_runs.sqlite3` table without copying connection credentials. A bounded metadata manifest checks automatic read scope and ignore policies; explicit Workspace files and upload originals are verified with SHA-256. Metadata-preserving external edits and filesystem races after verification remain possible; this is not a filesystem snapshot. Old MCP cursors are rejected as `cursor_expired` after resumption and the model is instructed to restart pagination in its new tool session.

`GET /api/chat/status?conversation_id=<id>` waits up to 10 seconds for the conversation's current request cleanup and returns `conversation_id` and `in_progress`. Before a new chat's start event, query with `workspace_session_id=<id>` to recover the draft's adopted conversation. It waits on the cleanup event without repeated polling, Provider calls, MCP execution, or folder listing. A request still marked `in_progress: true` retains ownership after the timeout. This endpoint does not cancel or resume work.

Release `0.3.0-alpha` includes the Explore project UI and improvements to Workspace, attachments, response progress, and result presentation.

`GET /api/conversations/{id}/work-runs` returns the newest 20 runs and `next_before`; pass `before` for older pages. `GET /api/conversations/{id}/work-runs/{run_id}?offset=0` returns run metadata, totals, 30 ordered steps, and `next_offset`, without raw model output. Both responses include verified current transcript indices in each run's `message_link.user_index` and `assistant_index`, with `null` for unmatched positions. New runs internally capture the question index, creation time, and content checksum to prevent incorrect links after edits or branch replacement. Its `/evidence?response_id=...&call_id=...` endpoint returns one original saved tool result. All queries require the existing owning conversation, do not resolve arbitrary file paths or cross-conversation runs, and make no Provider requests or MCP calls.

### Independent attachment API

Native UI selection uses `WS /api/attachments/picker/{token}`. Browser-supplied absolute paths are not read. Only files chosen through the owned native picker enter the existing validation and storage path, which returns ID references. The current conversation or Workspace draft determines the initial folder.

The backend API shares the preparation path used by the browser Attach dialog. Send original bytes as the body of `POST /api/attachments/uploads?filename=notes.txt` to receive an app-issued UUID and `reference`. The filename is a display basename; the API does not accept server paths or directories. Receiving and validation share existing attachment resource limits, with cleanup after failed or cancelled uploads.

Pass the returned reference in `attachment_sources` to `POST /api/attachments/inspect` or `POST /api/chat`.

```json
{"attachment_sources":[{"source":"upload","attachment_id":"<app-issued UUID>"}]}
```

A Workspace reference is `{"source":"workspace","path":"notes.txt"}`; inspection also returns its Workspace revision and session ID. Both sources share file count, total byte, and format processing policies. Legacy inspection `paths` and chat `attachment_files`, `workspace_file`, and `workspace_files` remain supported and require a selected Workspace.

`GET /api/attachments/uploads` lists retained copies and their availability; `GET /api/attachments/uploads/{id}` returns metadata for an available copy. API clients retain draft selections with `PUT /api/attachments/drafts/{UUID}` and `{"attachment_ids":["<upload UUID>"]}`, renewing within 24 hours. An empty list releases that draft's references. Optional `released_attachment_ids` also releases removed copies whose initial registration never reached the server; it does not override other owners or the retained selection. New unclaimed uploads receive a 24-hour grace period; released copies are cleaned in the background when no longer used. A draft response confirms the persisted release and does not wait for physical file deletion. `DELETE /api/attachments/uploads/{id}` explicitly deletes only unreferenced copies. Conversations, drafts, running requests, and valid retry receipts return `409` while retaining a copy; deleted or missing IDs return `410`. The UI does not expose a reusable-file library.

Chat saves new references on the user message. Regeneration restores them when `attachment_sources` is omitted; `[]` clears the selection. Changed references are saved atomically with a successful replacement reply. Legacy path fields continue using the current request selection for regeneration. One-time recovery reuses the failed request's prepared payload, which is discarded if its original is deleted. Image/PDF bytes and extracted bodies are not copied into conversation JSON.

## Features under consideration

The following are not yet available in the chat UI. Their scope and implementation order will be decided separately.

- MCP-based Work tools for proposing, reviewing, and applying changes, and running commands.
- Web search, code execution through Sandbox/Docker, and execution resource management.
- Optional Microsoft Entra ID authentication, Foundry deployment/model discovery, and deployment information autofill.

Entra ID and model discovery are not prerequisites for basic chat. Automatically filling deployment information also remains separate from the user's choice to enable memory or token limits.
