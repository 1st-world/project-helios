Language: ***English*** | [한국어](architecture.ko-kr.md)

[README](../../README.md) · [Usage](usage.md) · ***Architecture and storage*** · [API](api.md)

# Architecture and storage

Technical reference for Helios `0.4.1-alpha`: implementation boundaries, local data, independent schema versions, and compatibility policy. These are current contracts, not a guarantee of future alpha compatibility.

## Request flow

1. The browser selects a conversation, model profile, optional Workspace and attachments.
2. `app.py` binds request ownership and the selected connection/policies, prepares context, and checks admission.
3. Ordinary replies use `AIService`; explicit Explore requests use `ProjectExplorer`, which can call the fixed local MCP reader.
4. SSE carries progress, public summaries, response text and usage to the browser.
5. Conversation storage adopts completed replies; work storage retains original activity/results and separate ledgers record call usage. Stop, pause, resume and handoff retain their own authority and cleanup checks.

## Development and structure

| Path | Responsibility |
| --- | --- |
| `app.py` | Entry points, routes, and the ASGI lifespan for application resources. |
| `services/managed_server.py` | Starts and supervises the Uvicorn worker and runs offline maintenance between workers. |
| `services/app_runtime.py` | Builds each app's services and binds requests to that app's configuration. |
| `models/` | Profile, app settings, conversation, and message models. |
| `services/` | Responses calls, input policies, summaries, attachments, Workspace, persistence, and usage. |
| `templates/index.html` | Web UI structure. |
| `static/` | Browser modules, styles, fonts, and bundled libraries. |

Follow [AGENTS.md](../../AGENTS.md) for development guidelines. The [frontend asset inventory](../../static/vendor/README.md) records bundled assets, original licenses, and update instructions.

Use `“name”` when UI prose or action text refers directly to a specific model, profile or conversation. Frontend copy uses `nameInText` from `static/ui.js`. Standalone labels, chips, list entries and values separated by a label or parentheses remain unquoted. Generic guidance, states, counts, times and prices keep their usual formatting; stored values are not changed.

`python app.py` starts the managed launcher. The worker passes its settings and maintenance session to `create_app()`. For automatic reload during development, run Uvicorn directly. `app:app` serves the default instance from the same factory and supports basic checks in Settings. The [managed launcher](#checks-from-settings) also supports deep checks from Settings.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

The factory registers routes and mounts static assets. During ASGI startup, the app acquires the storage lock before initializing services. During shutdown, it stops its background tasks and closes picker sessions, Provider clients, and databases before releasing the lock. Tests and other code that call the ASGI app directly must run its lifespan.

Provider request construction keeps app guidance in `instructions`. Workspace context, file bodies selected through legacy path fields, and applicable conversation memory become JSON source data in the first `user` input. Unsummarized original history and saved tool calls/results follow, with the current question last. New attachment labels and their text or native content immediately follow that question. Empty memory or a summary outside the current history's coverage never replaces original messages.

Input estimates include guidance, JSON framing, and replayed material in the assembled request. Historical inputs and tool results are not rewritten; existing Resume checkpoints retain their prepared inputs.

## Input and public summaries

Each attachment carries its number within the request, filename, source, and delivery mode so files with the same name remain distinguishable. Text and extracted Office content are grouped in a JSON `content` value; original image/PDF inputs immediately follow their source descriptions. Only requests with attachments add shared guidance explaining that the supplied text is actual file content and that instructions inside files should be followed only when the user asks. The guidance also asks for accurate descriptions of empty, missing, or unreadable content based on the supplied input.

Successful assistant messages store display-only `reasoning_summary` with `reasoning_summary_schema_version: "0.1"` in the conversation. This data remains separate from answer content and is excluded from follow-up questions, regeneration, conversation-memory inputs, and input estimates. Original exploration records retain public summaries, but replay context for subsequent tool calls and questions clears their `summary` text while preserving encrypted reasoning state and original tool results. Public summaries are also available in saved model activity; raw output and encrypted state are not returned to the browser. Failed or cancelled partial ordinary replies are not adopted as assistant messages, following the existing persistence policy.

Only provider-supplied `summary_text` is handled. It is not raw internal reasoning or text reconstructed from reasoning tokens. Stream finalization and terminal snapshots reconcile without duplicating content, and a summary-only response does not count as a final answer.

## Local MCP project-reading foundation

`ProjectMCPClient` calls the bundled stdio server's `list_files`, `search_text`, and `read_file` for a selected Workspace. It pins the official Python MCP SDK to `2.3.0` and protocol `2026-07-28`. Explore project UI and the [explicit exploration API](api.md) support repeated calls. Ordinary chat with tools Off, Settings, Attach, and saved exploration queries do not start an MCP server.

Listings return direct children only; searches use case-sensitive literal UTF-8 text. Partial reads return relative paths, lines, Unicode columns, original byte ranges, and a metadata-based file revision. Long lines are fragmented and original newlines are preserved. Follow `next_cursor`, including empty progress pages. `complete` means traversal of the requested scope finished; omission counts still disclose excluded paths and unreadable files. It does not promise that the entire project was read.

Automatic reading loads custom exclusions from `.heliosignore` at the Workspace root. An absent file means no custom exclusions; connecting a folder never creates it. Use UTF-8 text with `.gitignore`-style patterns. Nested `.heliosignore` files are not policies. Under **Automatic reading**, independently choose whether to exclude hidden/system paths and apply ancestor and nested `.gitignore` files within the Workspace. Both options default to off, are saved per conversation, and apply without a separate legacy or migration mode.

Explicit inclusions (`!`) in `.heliosignore` override the selected defaults, provided excluded parent folders are included first. Git metadata (`.git`), symbolic links, junctions, hard links, host-selected Helios credentials, backups, runtime stores and paths outside the Workspace remain protected without a user-rule or model-controlled override, including through Windows 8.3 aliases. This application-level path policy is separate from explicit attachments and is not an OS sandbox. Changes to open files, directories or active ignore policies invalidate continuations; Paused resumption revalidates the current policy too. A file revision is neither a content hash nor a filesystem snapshot.

Ignore patterns are case-sensitive. Global Git configuration, global ignore files, and `.git/info/exclude` are not read.

Provisional local resource limits are 64 KiB per result, 256 KiB per stdio message, and 2 KiB per text fragment. Each page allows up to 1 MiB of processing, 2,048 steps, and 5 seconds of cooperative work. Individual filesystem calls can take longer; the host stops the session after an 8-second call timeout. Pages allow up to 256 listing entries, 128 search matches, or 128 read fragments, with a traversal depth of 32 and 16 cursors per session that expire after 5 idle minutes. Active ignore files must be valid UTF-8 within 64 KiB each. Invalid patterns, control characters, unsafe file types, or read failures never become empty policies. Workspace shows root policy status; invalid root rules block Explore before its Provider call. Invalid nested Git policies fail the operation that encounters them. These are not model token limits and do not change attachment limits. Cancellation, timeouts, protocol failures, and session closure clean up the process tree and discard cursors. Windows startup fails if a Job Object cannot be obtained. Arbitrary commands and external MCP servers are not accepted.

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

Default paths are defined in [config.py](../../config.py). Runtime data and settings are excluded from Git. **API keys are stored as plaintext in `profiles.json`.** Saved keys are not returned to the browser in read responses. Manage profile backups with the same care as the originals.

Although app and conversation data are stored locally, replies are generated by an external API. Consider the questions, history, selected project structure, and attachment content being sent, together with the provider's data handling terms. Fonts and frontend libraries are served locally without external browser CDN requests.

Work records do not expire automatically and remain after conversation deletion. Back up conversation JSON and the run database together when they contain work context. The separate usage ledger does not import run aggregates as new API calls.

### Read-only storage diagnostics

#### Checks from Settings

A confirmed deep check lets active requests finish before the loopback worker exits. Once the worker confirms that its stores have closed, the launcher takes the storage lock, runs the offline check, saves the result, and starts a new worker on the same port. The new worker restores the draft Workspace selections from the maintenance handoff.

The live basic check uses the configured paths for each store. It shares the inspection logic and Settings report format in `services/storage_diagnostics.py` with the offline deep check.

Check results are separate from the stores they inspect. The latest result of each type is saved in `.cache/storage-maintenance/` as `last-basic-check.json` and `last-deep-check.json`. The deep check file retains the full findings. The launcher prints its path before starting the new worker so the result remains accessible if startup fails.

#### Standalone diagnostics

Use the standalone command when Helios cannot start or to inspect an offline copy. Close Helios before scanning its live data directory. Run from the project directory in the app's Python environment. Use `--root` for a copy with the same data layout and `--json` for a structured report.

```bash
python -m services.storage_diagnostics
python -m services.storage_diagnostics --root "./helios-backup" --json
python -m services.storage_diagnostics --root "./helios-backup" --deep --json
```

The default offline check covers settings and profile schemas, recorded migration backups, conversation structure and memory coverage, SQLite structure and declared foreign keys, and saved upload references and file sizes. Uninitialized stores and compatible older schemas are informational. If a conversation refers to a deleted profile, select another profile in the app. Reports omit saved field values, API keys, and conversation text.

Add `--deep` to verify attachment copies against their recorded SHA-256 hashes and run SQLite's full integrity check, including index consistency. This reads every size-matching active copy and can take longer. Copies without an active manifest entry are flagged for review. Both modes open SQLite read-only and defer checks when journal or WAL sidecars exist. [SQLite integrity checks](https://www.sqlite.org/pragma.html#pragma_integrity_check) · [SQLite read-only URI options](https://www.sqlite.org/uri.html#recognized_query_parameters)

Deep diagnostics also compare saved replies with their work runs, paused checkpoints with their source turns and retained uploads, and model and summary calls with the usage ledger. Work history and usage can outlive the conversation that created them. Missing usage and paused checkpoints whose source has changed are review findings. Incorrect ownership and missing required links, such as a reply's work record or a retained upload, are reported as problems. Cross-store findings are deferred if a participating file changes during the check.

Exit code `0` means the check finished without a blocking finding. Code `1` means invalid data, an unsupported schema, or a missing required reference. Code `2` means a check was deferred and no blocking finding was confirmed. The JSON `complete` flag indicates whether checks were deferred, including when there is also a confirmed problem.

### Offline attachment recovery

The [managed preview API](api.md#preview-attachment-recovery) uses the same recovery planner as the command below. It drains the worker, verifies the selected backup under the storage lock, saves the latest preview separately as `.cache/storage-maintenance/last-recovery-preview.json`, and starts a new worker. The saved preview contains attachment results and counts. Use the standalone command to preview an offline store or create a recovered copy.

Close Helios and preview recovery with `--root` pointing to the damaged store and `--backup` pointing to a saved copy. Keep each backup attachment at its original `data/attachments/<UUID>.blob` path. The preview reports missing or damaged active copies and whether each backup matches the size and SHA-256 in the source manifest. It reads both inputs without modifying them.

```bash
python -m services.storage_recovery --root "../helios-offline" --backup "../helios-backup" --json
python -m services.storage_recovery --root "../helios-offline" --backup "../helios-backup" --output-dir "../recovery"
```

Create the output directory before applying. `--output-dir` rechecks the plan and publishes a new `helios-recovered-*` folder under that directory, outside both input trees. It copies saved settings, profiles and migration backups, transcripts, SQLite stores, and attachment blobs from the source. It restores matching backups in that copy and runs deep diagnostics before publishing it. Keep the recovered folder as private as the original because it can contain API keys and conversation data.

Exit code `0` indicates a successful preview or a copy with no blocking findings. Code `1` indicates unverified repairs in the preview or problems that remain in the published copy. Code `2` means the operation was blocked. The command reports the published path and remaining findings. If no backups qualify for restoration, it creates no output folder.

### Attachment ownership and cleanup

Copies stay on the computer running the app while conversation messages, the composer, or an Attach files selection reference them. Conversation deletion, branch replacement, selection removal/discard, and request completion check only affected copies for cleanup. Attachments used by earlier messages remain after composer removal; other conversations, drafts, running requests, and valid retry receipts retain their copies. Original source files are never deleted.

Tabs renew their selections every 15 minutes and when returning to the page; unchanged selections update only their expiry. Abandoned drafts and undelivered selection results receive a 24-hour grace period. Selection releases are persisted before physical deletion runs in the background. Cleanup reconciles the full state once at startup, then runs on ownership changes and the nearest actual expiry, without scanning files every minute. Failed deletions retry only their own paths with increasing delays; clock changes and new notifications do not shorten retry intervals. Committed references use an index updated after successful transcript writes. Unreadable conversations at startup suspend automatic deletion with one logged warning; restore the affected files and restart the app to revalidate them.

## Versions and compatibility

App versions and storage schema versions are managed independently. During alpha, they use `0.x.y` and strings in the `"0.*"` range, respectively.

### Current version inventory

| Component | Version | Source |
| --- | --- | --- |
| App | `0.4.1-alpha` | [app.py](../../app.py) |
| Settings JSON | `"0.2"` | [services/settings_service.py](../../services/settings_service.py) |
| Profiles JSON | `"0.1"` | [services/json_storage.py](../../services/json_storage.py) |
| Workspace binding | `"0.1"` | [models/workspace.py](../../models/workspace.py) |
| Attachment manifest | `"0.2"` | [services/attachment_store.py](../../services/attachment_store.py) |
| Message attachment references | `"0.1"` | [models/attachment.py](../../models/attachment.py) |
| Work run database | `"0.3"` | [services/work_run_store.py](../../services/work_run_store.py) |
| Message run references | `"0.1"` | [models/message.py](../../models/message.py) |
| Public reasoning summary | `"0.1"` | [models/reasoning.py](../../models/reasoning.py) |

### Protocols and dependencies

| Item | Declaration | Source |
| --- | --- | --- |
| MCP SDK | `mcp==2.3.0` | [requirements.txt](../../requirements.txt) |
| OpenAI SDK | `>=1.99.0,<2.0.0` | [requirements.txt](../../requirements.txt) |
| MCP protocol | `2026-07-28` | [project_tools.py](../../services/project_tools.py) |
| MCP server / tool-result contract | `"0.1"` | [project_tools.py](../../services/project_tools.py) |

Dependency ranges describe accepted installations, not a claim about the installed SDK version. Frontend library versions and integrity information are maintained in the [asset inventory](../../static/vendor/README.md).

`conversation.version`, profile/Workspace revisions, and checkpoint/execution revisions are mutation counters, not storage schema labels. Portable handoff uses integer `version: 1`; the current connection fingerprint uses integer version `2` and still validates supported older fingerprints. Usage databases have no separate declared schema-version constant; do not assign them the app or work-run version.

### When versions change

App versions change for verified releases that group multiple commits, not for every individual commit. Git commit SHAs distinguish development revisions. A released version number is not reused for different code.

| Change unit | Version handling during alpha |
| --- | --- |
| Individual development commits; documentation, comment, and test cleanup | Keep the app version. |
| A release grouping bug fixes and small UI/UX refinements | Increment PATCH. Example: `0.2.0 → 0.2.1`. |
| A release grouping new features, significant policy changes, or incompatible alpha changes | Increment MINOR and reset PATCH to 0. Example: `0.2.x → 0.3.0`. |
| Stable release | Start at app version `1.0.0`. |

This MINOR/PATCH distinction is Helios's alpha policy. Alpha releases use the `-alpha` suffix. A release-preparation commit checks `app.py`, both READMEs, and current version/schema descriptions in the references together. Tag the verified commit with a version such as `v0.4.1-alpha`. Storage schemas change separately when their storage contract changes, not simply because an app feature was added.

### Storage and the end of alpha

The run database uses string schema `"0.3"`; optional message run references use the independent string schema `"0.1"`. New message references record `run_schema_version` and `run_id`. Existing conversations without run references remain readable; run support does not batch-rewrite them to backfill those references. Unsupported versions are preserved without overwriting their originals.

- The current app version is **`0.4.1-alpha`**. Settings use **string schema `"0.2"`** and profiles retain **`"0.1"`**. These are not JSON decimals: `"0.1"` and `"0.10"` are distinct identifiers.
- Settings `"0.1"` load without startup writes. The first explicit save preserves their exact bytes in `settings.json.v0.1-<sha256>.bak` before publishing `"0.2"`. The migration record identifies that source and its effective policy; any older migration provenance remains in the backed-up document. Legacy triggers of `0` retain original-history behavior with generation and saved-summary use disabled. Other existing profiles' memory permissions are unchanged.
- Profile migration records retain their original app-settings snapshot shape. Supported historical field sets are validated explicitly instead of requiring every current preference field.
- The attachment manifest uses string schema `"0.2"`; optional message attachment references remain `"0.1"`. Existing `"0.1"` manifests upgrade transactionally with a 24-hour cleanup grace for unused copies. Attachment-manifest migration does not batch-rewrite conversation JSON; new references record `attachment_schema_version` and `attachment_sources`. Unsupported versions are neither overwritten nor cleaned. Back up copies, the manifest, and conversation JSON together.
- Existing formats without a version field are identified internally as `0`. Their original bytes are backed up before the first explicit save. The migration record's `from_schema_version: 0` and the backup name's `v0` identify that origin; new documents do not write a numeric `schema_version: 0`.
- Invalid or unsupported settings/profile versions produce an error without overwriting the originals. Preserve the affected files and backups while investigating.
- Alpha features and formats are not a permanent compatibility contract for later stages. **At the end of alpha, including a transition to beta**, support ends for alpha legacy migration code, deprecated API field aliases, and unreviewed legacy input policies. Normal execution after alpha will not automatically interpret or recover alpha legacy formats.
- If final-alpha data needs migration, define a separate procedure or tool when preparing to end alpha. Ending compatibility does not mean deleting original files or backups. Compatibility code will be removed as part of that transition; the current alpha preserves its existing compatibility and data protections.
- App/schema versions and accepted data formats for beta will be decided before the transition. Beta may precede the stable release. Stable versions start at app **`1.0.0`** and storage schema **`"1.0"`**.

Legacy conversation profile selection can be initialized and saved during list/detail/selection reads. This is separate from run-reference or attachment-manifest migration; the absence of reference backfilling does not mean a conversation JSON file is never rewritten.

## Features under consideration

The following are not yet available in the chat UI. Their scope and implementation order will be decided separately.

- MCP-based Work tools for proposing, reviewing, and applying changes, and running commands.
- Web search, code execution through Sandbox/Docker, and execution resource management.
- Optional Microsoft Entra ID authentication, Foundry deployment/model discovery, and deployment information autofill.

Entra ID and model discovery are not prerequisites for basic chat. Automatically filling deployment information also remains separate from the user's choice to enable memory or token limits.

## Documentation scope

The maintained bilingual references live in `docs/reference/`. English uses the default `.md` filename; Korean translations use `.ko-kr.md`. Sanitized, reusable live Provider evidence lives separately in [SHARED-provider-records](../SHARED-provider-records/README.md). Local plans, test reports and screenshots are not part of these references; historical communication records do not establish support for every model.
