"""Project saved references for read-only checks across Helios stores.

Readers accept validated SQLite snapshots and retain only the metadata needed for comparison.
"""

import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import TypeGuard

from models.attachment import ATTACHMENT_SOURCES
from models.conversation import Conversation
from services.json_storage import decode_storage_json
from services.work_run_store import ACTIVE_STATES, TERMINAL_STATES, WorkRunStore


CONVERSATIONS = "conversation"
WORK_RUNS = "data/work_runs.sqlite3"
CALL_USAGE = "logs/usage_calls.sqlite3"
SUMMARY_USAGE = "logs/summary_usage.sqlite3"
ATTACHMENTS = "data/attachments"
MANIFEST = f"{ATTACHMENTS}/manifest.sqlite3"
REPLY_CALL_KINDS = {"chat", "regeneration", "legacy_reply"}


@dataclass(frozen=True)
class Finding:
    """Expose a stable classification and a safe, root-relative location."""

    path: str
    status: str
    code: str
    message: str


@dataclass(frozen=True)
class UserReference:
    """Identify an exact user turn without retaining the user's message."""

    index: int
    created_at: str
    content_sha256: str


@dataclass
class Transcript:
    """Retain only the branch revision and referenced records of a readable conversation."""

    path: str
    version: int
    users: set[UserReference] = field(default_factory=set)
    replies: list[tuple[str | None, object]] = field(default_factory=list)

    @classmethod
    def from_conversation(cls, path: str, conversation: Conversation) -> "Transcript":
        """Use the same turn identity as the runtime resume check."""
        transcript = cls(path, conversation.version)
        for index, message in enumerate(conversation.messages):
            if message.role == "user":
                digest = sha256(message.content.encode("utf-8")).hexdigest()
                transcript.users.add(UserReference(index, message.created_at.isoformat(), digest))
            elif message.role == "assistant" and (message.run_id is not None or message.usage_call_id is not None):
                transcript.replies.append((message.run_id, message.usage_call_id))
        return transcript


@dataclass(frozen=True)
class Run:
    """Identify the owner and state of one work run."""

    owner: str
    status: str


@dataclass(frozen=True)
class PausedRun:
    """Retain the branch authority of an unexpired paused run."""

    owner: str
    version: int
    source: UserReference


@dataclass
class WorkReferences:
    """Retain run identities and references independently of their saved outputs."""

    runs: dict[str, Run | None] = field(default_factory=dict)
    calls: dict[str, str] = field(default_factory=dict)
    paused: list[PausedRun] = field(default_factory=list)
    uploads: set[str] = field(default_factory=set)
    findings: set[Finding] = field(default_factory=set)


@dataclass(frozen=True)
class Call:
    """Describe the durable ownership of a usage record."""

    owner: str | None
    kind: str
    run_id: str | None


@dataclass
class UsageReferences:
    """Index ledger rows and distinguish unreadable rows from absent ones."""

    calls: dict[str, Call | None] = field(default_factory=dict)
    findings: set[Finding] = field(default_factory=set)


@dataclass
class SummaryReferences:
    """Retain only explicit links from summary rows to ledger calls."""

    calls: set[tuple[str, str | None]] = field(default_factory=set)
    findings: set[Finding] = field(default_factory=set)


def _identifier(value: object) -> TypeGuard[str]:
    """Accept nonempty textual identities, including ids from older usage imports."""
    return isinstance(value, str) and bool(value.strip())


def _document(raw: str) -> dict:
    """Read a saved row as an object without retaining exception details."""
    value = decode_storage_json(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a saved object.")
    return value


def _tables(db: sqlite3.Connection) -> set[str]:
    """Recognize optional tables in supported earlier snapshots."""
    return {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _checkpoint_sources(context: dict) -> set[str]:
    """Validate the retained input envelope and project its immutable uploads."""
    messages = context.get("input_messages")
    if (type(context.get("version")) is not int or context["version"] != 1
            or not isinstance(context.get("instructions"), str)
            or not isinstance(messages, list) or not messages
            or not all(isinstance(message, dict) for message in messages)):
        raise ValueError("Invalid saved input envelope.")
    return _upload_ids(context.get("attachment_sources", []))


def _upload_ids(sources: object) -> set[str]:
    """Use the app's attachment reference schema without opening any workspace paths."""
    return {str(source.attachment_id) for source in ATTACHMENT_SOURCES.validate_python(sources)
            if source.source == "upload"}


def _source_reference(run: dict, snapshot: dict) -> PausedRun:
    """Read the branch revision and exact source turn stored by a paused run."""
    source = snapshot.get("source_message")
    if (not isinstance(source, dict) or type(source.get("user_index")) is not int
            or source["user_index"] < 0 or not _identifier(source.get("created_at"))
            or not isinstance(source.get("content_sha256"), str)
            or len(source["content_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in source["content_sha256"])
            or type(run.get("base_version")) is not int or run["base_version"] < 0):
        raise ValueError("Invalid paused source turn.")
    return PausedRun(run["conversation_id"], run["base_version"], UserReference(
        source["user_index"], source["created_at"], source["content_sha256"],
    ))


def _resume_input(db: sqlite3.Connection, tables: set[str], run_id: str, snapshot: dict) -> dict | None:
    """Resolve either a current stored input or a supported legacy inline input."""
    if "work_resume_contexts" in tables:
        row = db.execute("SELECT data FROM work_resume_contexts WHERE run_id=?", (run_id,)).fetchone()
        if row is not None:
            return _document(row[0])
    return snapshot.get("resume_context")


def _check_continuation(
    db: sqlite3.Connection, tables: set[str], run: dict, *, segmented: bool, now: datetime,
    result: WorkReferences,
) -> None:
    """Check checkpoint ownership while respecting expiration and earlier schemas."""
    paused = run["status"] == "paused"
    if paused:
        expires = datetime.fromisoformat(run["pause_expires_at"])
        if expires.tzinfo is None:
            raise ValueError("Paused expiration must be timezone-aware.")
        if expires <= now:
            return
    snapshot = run.get("snapshot", {})
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid work snapshot.")
    separated = snapshot.get("has_resume_context") is True
    indexed = _upload_ids(snapshot.get("resume_attachment_sources", []))
    result.uploads.update(indexed)
    if paused:
        boundary = run if segmented else {**run, "execution_revision": 0}
        if not WorkRunStore._checkpoint_matches(boundary):
            raise ValueError("Invalid paused boundary.")
        result.paused.append(_source_reference(run, snapshot))
    if not separated and "resume_context" not in snapshot:
        if paused:
            raise ValueError("Paused input is not linked.")
        return
    context = _resume_input(db, tables, run["id"], snapshot)
    if not isinstance(context, dict):
        raise ValueError("Saved input is missing.")
    uploads = _checkpoint_sources(context)
    result.uploads.update(uploads)
    if separated and indexed != uploads:
        raise ValueError("Stored and indexed upload references differ.")
    if paused and "work_segment_contexts" in tables:
        for (raw,) in db.execute(
            "SELECT c.data FROM work_execution_segments s LEFT JOIN work_segment_contexts c "
            "ON c.run_id=s.run_id AND c.segment_id=s.id "
            "WHERE s.run_id=? AND s.position>0 AND COALESCE(json_extract(s.data, '$.state'), 'committed')='committed'",
            (run["id"],),
        ):
            segment_uploads = _checkpoint_sources(_document(raw))
            result.uploads.update(segment_uploads)
            if segment_uploads != uploads:
                raise ValueError("A committed segment has different upload ownership.")


def read_work(db: sqlite3.Connection | None) -> WorkReferences:
    """Project work rows after SQLite structure and declared foreign keys were checked."""
    result = WorkReferences()
    if db is None:
        return result
    tables = _tables(db)
    segmented = "work_execution_segments" in tables
    now = datetime.now(timezone.utc)
    for identity, owner, status, raw in db.execute("SELECT id, conversation_id, status, data FROM work_runs"):
        result.runs[identity] = None
        try:
            run = _document(raw)
            if (not _identifier(identity) or not _identifier(owner) or not isinstance(status, str)
                    or status not in ACTIVE_STATES | TERMINAL_STATES | {"paused"}
                    or (run.get("id"), run.get("conversation_id"), run.get("status")) != (identity, owner, status)):
                raise ValueError("Work row identity or state is inconsistent.")
        except (TypeError, ValueError, RecursionError):
            result.findings.add(Finding(WORK_RUNS, "invalid_data", "invalid_work_record",
                                        "A work record disagrees with its stored identity, owner, or state."))
            continue
        result.runs[identity] = Run(owner, status)
        if segmented:
            segment_id = run.get("execution_segment_id")
            segment = _identifier(segment_id) and db.execute(
                "SELECT 1 FROM work_execution_segments WHERE run_id=? AND id=?", (identity, segment_id),
            ).fetchone()
            if not segment:
                result.findings.add(Finding(WORK_RUNS, "missing_reference", "work_segment_missing",
                                            "A work record refers to an execution segment it does not own."))
        if status in ACTIVE_STATES | {"paused"}:
            try:
                _check_continuation(db, tables, run, segmented=segmented, now=now, result=result)
            except (TypeError, ValueError, KeyError, RecursionError):
                result.findings.add(Finding(WORK_RUNS, "invalid_data", "invalid_work_checkpoint",
                                            "A saved continuation has invalid or missing input or boundary data."))
    for call_id, run_id in db.execute("SELECT call_id, run_id FROM model_steps"):
        if not _identifier(call_id) or run_id not in result.runs or call_id in result.calls:
            result.findings.add(Finding(WORK_RUNS, "invalid_data", "invalid_work_call_reference",
                                        "A model step has an invalid or duplicate call reference."))
            continue
        result.calls[call_id] = run_id
    segment_match = "AND m.segment_id=t.segment_id" if segmented else ""
    if db.execute(
        "SELECT 1 FROM tool_steps t WHERE NOT EXISTS (SELECT 1 FROM model_steps m "
        f"WHERE m.run_id=t.run_id AND m.response_id=t.response_id {segment_match}) LIMIT 1",
    ).fetchone() is not None:
        result.findings.add(Finding(WORK_RUNS, "missing_reference", "tool_response_missing",
                                    "A saved tool step has no model response in its execution scope."))
    return result


def read_usage(db: sqlite3.Connection | None) -> UsageReferences:
    """Read ledger identities without retaining accounting details or provider content."""
    result = UsageReferences()
    if db is None:
        return result
    linked = "usage_run_links" in _tables(db)
    query = (
        "SELECT c.id, c.conversation_id, c.kind, l.run_id, c.data FROM usage_calls c "
        "LEFT JOIN usage_run_links l ON l.call_id=c.id" if linked else
        "SELECT id, conversation_id, kind, NULL, data FROM usage_calls"
    )
    for identity, owner, kind, run_id, raw in db.execute(query):
        result.calls[identity] = None
        try:
            data = _document(raw)
            if (not _identifier(identity) or (owner is not None and not _identifier(owner))
                    or not _identifier(kind) or (run_id is not None and not _identifier(run_id))
                    or (data.get("id"), data.get("conversation_id"), data.get("kind"), data.get("run_id"))
                    != (identity, owner, kind, run_id)):
                raise ValueError("Usage row identity is inconsistent.")
        except (TypeError, ValueError, RecursionError):
            result.findings.add(Finding(CALL_USAGE, "invalid_data", "invalid_usage_record",
                                        "A usage row disagrees with its saved identity, owner, or work link."))
            continue
        result.calls[identity] = Call(owner, kind, run_id)
    return result


def read_summaries(db: sqlite3.Connection | None) -> SummaryReferences:
    """Read explicit usage links from the legacy and current summary store."""
    result = SummaryReferences()
    if db is None:
        return result
    for (raw,) in db.execute("SELECT data FROM summary_usage"):
        try:
            data = _document(raw)
            identity, owner = data.get("usage_call_id"), data.get("conversation_id")
            if ((identity is not None and not _identifier(identity))
                    or (owner is not None and not _identifier(owner))):
                raise ValueError("Invalid summary usage reference.")
        except (TypeError, ValueError, RecursionError):
            result.findings.add(Finding(SUMMARY_USAGE, "invalid_data", "invalid_summary_usage_record",
                                        "A saved summary usage row contains an invalid reference."))
            continue
        if identity is not None:
            result.calls.add((identity, owner))
    return result


REFERENCE_READERS = {WORK_RUNS: read_work, CALL_USAGE: read_usage, SUMMARY_USAGE: read_summaries}


@dataclass
class StorageReferences:
    """Compare projections from available stores without running app lifecycle code."""

    conversations: dict[str, Transcript] = field(default_factory=dict)
    conversation_files: set[str] | None = None
    work: WorkReferences | None = None
    usage: UsageReferences | None = None
    summaries: SummaryReferences | None = None
    uploads: set[str] | None = None
    manifest_initialized: bool = False

    def add_database(self, data: WorkReferences | UsageReferences | SummaryReferences) -> None:
        """Retain a database projection only after its read was stable."""
        if isinstance(data, WorkReferences):
            self.work = data
        elif isinstance(data, UsageReferences):
            self.usage = data
        else:
            self.summaries = data

    def findings(self, transcript_uploads: set[str]) -> list[Finding]:
        """Produce deterministic findings without including saved payload values."""
        findings = set(self.check_replies())
        if self.uploads is not None:
            findings.update(self.check_uploads(transcript_uploads))
        if self.work is not None:
            findings.update(self.work.findings)
            findings.update(self.check_work())
        if self.usage is not None:
            findings.update(self.usage.findings)
        if self.summaries is not None:
            findings.update(self.summaries.findings)
            if self.usage is not None:
                findings.update(self.check_summary_calls())
        return sorted(findings, key=lambda item: (item.path, item.code))

    def check_replies(self) -> Iterator[Finding]:
        """Check explicit links from retained assistant messages."""
        for owner, conversation in self.conversations.items():
            path = conversation.path
            counts = Counter(run_id for run_id, _ in conversation.replies if run_id is not None)
            if any(count > 1 for count in counts.values()):
                yield Finding(path, "invalid_data", "duplicate_reply_work_reference",
                              "Several replies refer to the same work run.")
            if self.work is not None:
                for run_id in counts:
                    run = self.work.runs.get(run_id)
                    if run_id not in self.work.runs:
                        yield Finding(path, "missing_reference", "reply_work_missing",
                                      "A saved reply refers to a missing work run.")
                    elif run is not None and (run.owner != owner or run.status != "completed"):
                        yield Finding(path, "invalid_data", "reply_work_mismatch",
                                      "A saved reply refers to work that belongs elsewhere or was not completed.")
            for run_id, call_id in conversation.replies:
                if call_id is None:
                    continue
                if not _identifier(call_id):
                    yield Finding(path, "invalid_data", "invalid_reply_usage_reference",
                                  "A saved reply contains an invalid usage call reference.")
                elif self.usage is not None:
                    finding = self.check_call(path, call_id, owner, run_id, REPLY_CALL_KINDS, "reply")
                    if finding is not None:
                        yield finding

    def check_call(
        self, path: str, identity: str, owner: str | None, run_id: str | None, kinds: set[str], source: str,
    ) -> Finding | None:
        """Treat missing usage as incomplete accounting and wrong ownership as damage."""
        calls = self.usage.calls
        if identity not in calls:
            return Finding(path, "review", f"{source}_usage_missing",
                           "A referenced call is missing from the usage ledger. Recorded totals may be incomplete.")
        call = calls[identity]
        if call is not None and (call.owner != owner or call.run_id != run_id or call.kind not in kinds):
            return Finding(path, "invalid_data", f"{source}_usage_mismatch",
                           "A referenced usage call disagrees with its expected owner, work link, or kind.")
        return None

    def check_uploads(self, transcript_uploads: set[str]) -> Iterator[Finding]:
        """Check only uploads owned by saved transcripts and work continuations."""
        if self.manifest_initialized:
            for identity in transcript_uploads - self.uploads:
                yield Finding(f"{ATTACHMENTS}/{identity}.blob", "missing_reference", "upload_reference_missing",
                              "A conversation refers to an upload without an active manifest entry.")
        elif transcript_uploads:
            yield Finding(MANIFEST, "missing_reference", "attachment_manifest_missing",
                          "Saved upload references or copies exist without an initialized manifest.")
        if self.work is not None:
            for identity in self.work.uploads - self.uploads - transcript_uploads:
                yield Finding(f"{ATTACHMENTS}/{identity}.blob", "missing_reference", "work_upload_reference_missing",
                              "A saved continuation refers to an upload without an active manifest entry.")

    def check_work(self) -> Iterator[Finding]:
        """Check dormant branches and call links without requiring historical transcripts."""
        for paused in self.work.paused:
            conversation = self.conversations.get(paused.owner)
            if conversation is not None:
                if paused.version != conversation.version or paused.source not in conversation.users:
                    yield Finding(conversation.path, "review", "paused_source_changed",
                                  "The current transcript no longer matches a paused work source.")
            elif self.conversation_files is not None and f"{paused.owner}.json" not in self.conversation_files:
                yield Finding(WORK_RUNS, "review", "paused_conversation_missing",
                              "An unexpired paused run has no saved conversation to resume in.")
        if self.usage is None:
            return
        for call_id, run_id in self.work.calls.items():
            run = self.work.runs[run_id]
            if run is not None:
                finding = self.check_call(WORK_RUNS, call_id, run.owner, run_id, REPLY_CALL_KINDS, "work")
                if finding is not None:
                    yield finding
        for call_id, call in self.usage.calls.items():
            if (call is not None and self.work.runs.get(call.run_id) is not None
                    and call_id not in self.work.calls):
                yield Finding(CALL_USAGE, "review", "usage_work_step_missing",
                              "A ledger call points to retained work without a matching model step.")

    def check_summary_calls(self) -> Iterator[Finding]:
        """Check explicitly linked summaries independently of adoption or retention."""
        for call_id, owner in self.summaries.calls:
            finding = self.check_call(SUMMARY_USAGE, call_id, owner, None, {"summary"}, "summary")
            if finding is not None:
                yield finding
