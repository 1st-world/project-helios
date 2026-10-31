Language: ***English*** | [한국어](usage.ko-kr.md)

[README](../../README.md) · ***Usage*** · [Architecture and storage](architecture.md) · [API](api.md)

# Detailed usage guide

Detailed behavior for Helios `0.4.0-alpha`. Start with the root README for installation and basic workflows.

## Your first connection

1. Open **Settings → Connections → New profile**.
2. Enter a descriptive **Profile name**, the actual **Deployment name**, the **Foundry endpoint**, and an **API key**.
3. Select **Save changes**. The first profile becomes the default for new chats. Use **Set as default** in the list to change it; the **Default** badge identifies it.
4. Choose the profile for this conversation's next response in the composer's **Conversation model**, then send a message. Basic chat does not require model capacity, pricing, or memory configuration.

Model selections are saved per conversation and restored when reopened. A new chat captures the default when its draft opens; later default changes leave existing conversations and already selected drafts unchanged. Editing a profile in Settings is separate from choosing the new-chat default.

Sending while a model selection is saving waits for confirmation. Failed saves or changes from another tab reload the current choice, preserve the question draft, and do not automatically resend. If readback also fails, use **Reload selection** to verify it. Deleted selections show **Unavailable model** without automatically substituting another profile. Attachment checks also use this conversation's selected model and settings.

Enter the resource or Foundry project API base used to call the Deployment. Project endpoints containing `/api/projects/<project-name>` are supported, including bases with `/openai/v1/` already appended. Use the API base rather than an individual `/responses` operation URL, an Azure Portal page, or an Azure Resource Manager management URL.

```text
https://your-resource.cognitiveservices.azure.com/
https://your-resource.services.ai.azure.com/openai/v1/
https://your-resource.services.ai.azure.com/api/projects/your-project
```

The project path is retained. The app appends `/openai/v1/` when needed, then calls `responses` beneath that base. See [Endpoint handling](api.md#endpoint-handling) for URL construction and official references.

**Deployment name means the name deployed in your resource, not the model's catalog name.** Saving a profile stores connection information locally. Actual API compatibility and permissions are checked when a request is sent.

[profiles.example.json](../../profiles.example.json) illustrates the storage format. Its model names, deployment names, and prices are examples; enter the information and rates for your actual deployment. You can create new connections in the UI.

## During response generation

From **Send**, a compact indicator shows the current phase and elapsed time. It distinguishes preparation, waiting for the model, receiving text, and finalization, and remains visible after text starts arriving until completion. Preparation can be stopped and duplicate sends are blocked. Cancelling preparation before submission restores the original question without overwriting another draft. **Stop** requests cancellation; exploration confirms its saved terminal state before showing the cancelled result.

Stopping while waiting for Workspace or attachment preparation leaves the send wait immediately while shared preparation continues independently. After submission, the UI confirms request cleanup and reloads the saved conversation before enabling input. Confirmation waits up to 10 seconds; a timeout or connection failure is disclosed while server-side duplicate-request protection remains active. A new chat stopped before its start event recovers its saved identity through the Workspace draft and restores the question. An answer saved before cancellation takes precedence.

For an ordinary response, received text and public summaries remain available as explicitly incomplete output in the current view. This temporary output is removed when reopening the conversation or sending the next question, and is excluded from saved answers and follow-up input. Cancelled regeneration preserves the previous answer. Cancellation does not subtract reported usage or costs; calls without final usage remain unknown rather than zero. The rightmost **Send** button becomes **Stop** in the same position during generation. Stop ends the request. Work tools runs also offer **Pause after current step** to its left.

Selecting Work tools first shows ordinary response progress and public summaries. A reply that finishes without a tool call keeps only Reasoning summary, with no empty section when no public summary exists. The first actual tool call promotes the view to Work process for listing, search, and read operations. **Work process** combines public summaries, model updates, and tool steps in call order. It opens by default during generation and folds once when the final answer begins; you can reopen it afterward. Progress reflects observed app events rather than estimated completion, remaining time, or private model reasoning. Elapsed time includes waiting for this request and can differ from saved response/run duration or billable time.

## Reasoning summary

For a supported deployment, choose **On** under **Settings → Connections → profile → Context & memory → Input & output → Reasoning summary** to request public reasoning summaries. Both new and existing profiles default to **Off**. Support is not inferred from deployment names or reasoning-token counts. On requests `summary: "auto"` from the provider; unsupported requests follow existing error handling without an automatic retry that removes the option. This setting does not apply to separate conversation-memory summary calls.

When a public summary arrives, **Reasoning summary** opens by default above the answer and shows its streamed content. The status, elapsed time, and disclosure share one header. It folds once when the final answer starts. A manual collapse during generation is respected, and reopening after the final-answer transition is also preserved. When a tool is actually called, Explore project combines each call’s public summaries, visible model output, tool steps, saved-result actions, and per-call usage inside **Work process** in call order. Before tool-call classification is complete, exploration model output appears in the progress record; confirmed final output moves into the answer area without duplication. Copy content includes only the answer. Long records scroll internally, and saved records start collapsed. Ordinary replies without a summary have no empty section.

## Input, output, and memory settings

**Settings → Context & attachments** contains app defaults. **Connections → profile → Context & memory** contains profile settings. A profile can replace the app's input budget and Input check mode. For output, the smaller explicit app or profile limit applies. Saved changes apply to new requests.

Each request pins its selected connection, model, pricing and policy at the start. Changing the active selection, adding/editing/deleting another profile, renaming a profile, or saving identical values does not revoke that request's memory summary or resumed work. Changes to the pinned profile's actual request settings, or its deletion, revoke only that profile's authorization. Restoring values or turning memory back on does not revive previously revoked work. Shared app policy changes retain validation that revokes existing requests across profiles.

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

Connecting a folder checks its path and exclusion-policy status without creating files. Preparing a message in a connected conversation automatically includes a small overview of the project name and top-level files and folders. There is no separate structure-sharing toggle. Expand **Workspace → Preview context** to inspect the overview. It includes at most 32 direct entries and 8KiB in total; bounded listing work can also leave partial coverage. The **Automatic reading** options, `.heliosignore` and protected boundaries also apply to Work tools that read the Workspace.

The overview refreshes for each new message and regeneration request, then stays fixed across subsequent model calls within that request. It is included in input estimation and budget checks. One-time summary recovery reuses the failed request's overview. The overview does not recursively traverse subfolders or automatically send file contents or absolute host paths. Explicitly select supported **Work tools** or attach files for additional structure and contents. Disconnecting a folder does not remove project information already mentioned in conversation history or memory.

Versions containing this exclusion-policy change **apply the new defaults to existing conversations too**. Hidden paths and `.gitignore` matches were previously excluded automatically; both filters are now optional and default to off, so automatic reading may include more paths. Review **Workspace → Automatic reading** and the root `.heliosignore`. An absent file means no custom exclusions; there is no automatic file creation, rule import or separate transition procedure. If a Paused checkpoint from the previous policy fails current validation, discard that run and prepare a new request. Saved conversations, work results and usage records remain available.

**Attach → Add files** opens the native file picker on the **computer running Helios**, like Workspace Browse. A connected Workspace supplies the starting folder; selecting a Workspace is optional. Files inside and outside it follow the same attachment-copy flow. File selection in the current UI requires an available native picker.

The shared notice at the top of Attach files explains the host location and attachment copies. The center of **Selected files** shows its empty state or selected-file list; its footer groups inclusion guidance, the full supported-format list, and selection feedback. **Selection check** contains the local check's scope and results.

Use **Add files** beside the **Selected files** heading, then review and remove files in one list. The dialog grows with its content, and long selections scroll within the list area, like Profiles in Settings. **Selection check** shows guidance before checking. Use **Check files → Attach files** to review delivery and check results before applying. Cancel, the close X, backdrop clicks, and Escape ask before discarding unapplied selection changes. Add files becomes **Stop** during native selection. Use it or close Attach to cancel; dismissal waits for cancellation acknowledgement before checking unapplied changes. Cancelling or failing a new selection preserves the previous selection, and an incomplete new batch is rolled back. The check verifies local preparation, not model acceptance.

Attachment copies remain included with new messages until removed. Later edits or moves of the source file and Workspace changes do not alter copies already added. Reopening a conversation restores its latest user message's attachment references. Regeneration uses that message's original attachments unless you applied a selection in Attach or removed a composer attachment; then it uses your changed selection. Changed selections are checked again, and failed regeneration preserves the previous reply and its saved references.

Workspace path references in older conversations retain their original live-file behavior. Changing or disconnecting a folder removes only those path attachments. Restored stale references display **Unavailable**; a same-named file in another folder is never substituted. Select them again or remove them.

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

Resume explicitly revalidates the branch, connection, memory permission, Workspace, files and attachments. A paused conversation locks the message field, attachments, Tools and Workspace context, but **Conversation model** can select the model to continue with. The rightmost primary button becomes **Resume**, with the selected model in its accessible label and tooltip. That button is also disabled when the original question cannot be verified or resumption is unavailable. Resumption uses the existing confirmation dialog. The server rejects new turns, message edits and regeneration until the run is resumed and completed or explicitly discarded. Composer drafts are preserved; other conversations and new chats remain available. A committed pause survives app restart for up to 24 hours without automatically starting paid work. Disconnects, Provider/MCP errors and app exits during incomplete steps terminate the attempt without automatic replay or resume. Each pause is retained for 24 hours; completion, Stop, expiry, conversation deletion or confirmed invalidation releases private resume input and additional attachment ownership. Recorded work and the usage ledger remain available.

Tool-assisted answers show the model and creation time in the same footer as ordinary answers. Their usage summary covers all model calls in that response: call count, combined tokens, elapsed run time, and estimated cost. **Details** explains the breakdown and aggregate scope. Active time includes model and tool work and excludes paused intervals. Incomplete records show only known values; missing data is not treated as `0`.

**App work result** presents attempts without a final answer in independent response rows with the same width as model answers. Its label and icon identify app guidance; it is neither stored as an assistant message nor added to conversation input. Profile, run start time, usage, and **Details** share the answer footer component. The result guidance distinguishes model-call limits, time/cost targets, missing cost records, local input checks, request cancellation, app interruption, and Provider errors. After Stop, **Stopping** remains until cleanup and the saved terminal state are confirmed. Recorded activity and incurred usage remain available. Follow-up actions open the relevant limits, connection, input budget, Workspace, or usage view. **Review limits & targets** opens current applied settings; only the relevant target value appears in outcome guidance when a limit stops the attempt.

When the original question can be verified, **Prepare new request** asks for confirmation, copies it into the composer, and selects Explore project. Review the current Workspace, model, attachments, and applied limits, then press **Send** to start a new request. Preparation neither resumes the saved attempt nor sends automatically, and warns before replacing another message draft. Unmatched older records direct you to write a new request in the composer.

For runs with actual tool activity, **Work process** appears above model answers or app work result guidance. Attempts without a final answer occupy independent response rows linked to verified original questions. A failed regeneration follows the retained successful answer. Older records without a current message match remain under **Other runs**. Expand a record to page through public summaries and model updates grouped by call, tool steps, and per-call usage. Each call's **Details** is separate from the response aggregate; unadopted attempts show their profile, run start time, and run totals in the shared footer. Outcome guidance and follow-up actions are not repeated inside collapsed activity. Expanded state and loaded activity pages survive rerendering the same conversation. **Open saved result** prioritizes the file, requested and returned ranges, and whether EOF was reached or more results remain. Read fragments with continuous byte and line/column positions in the same file revision appear as one text block. Search results distinguish match positions from snippet starts, and folder results list returned files and folders. Long folder listings, search matches, multiple read ranges, and technical details each scroll within a bounded, keyboard-accessible region. **Result details** contains revisions, exact line/column/byte positions, and omissions; **Original result (JSON)** preserves the saved output. The view does not reread current files, represent partial coverage as the whole project, or fetch additional results automatically. Private model reasoning is not returned to the browser. The response-usage display preference controls response totals, unadopted run totals, and per-call usage.

Failed, cancelled, interrupted, and replaced runs remain available. Completed runs outside the current answer branch are labelled and never automatically adopted. Reopening a conversation restores its history; older runs and steps are paged. Conversation deletion does not automatically erase original run content or the separate usage ledger, but deleted conversations' activity cannot be opened through the UI/API.

## Usage and costs

Use **Settings → Usage & costs** to inspect call records and date-based totals. Per-message and per-conversation displays are optional. Usage is based on provider-reported tokens and distinguishes missing values from `0`. Reasoning tokens are not added to output a second time.

Enter prices under **Connections → profile → Token pricing** in USD / 1M tokens. You can configure standard, cache read/write, and long-context rates, plus an input-token breakpoint. A blank standard rate is unknown; `0` means free. A blank long-context rate uses its corresponding standard rate. The default breakpoint is 272,000 tokens; adjust it to the model's actual pricing conditions.

Costs are estimates using the prices saved at call time and the reported usage. Missing required values produce partial or unavailable estimates. The app does not retrieve Azure invoices or remaining account credits. The independent usage ledger remains after a conversation is deleted.
