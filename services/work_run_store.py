"""Retain work runs, ordered provider output, and original tool results.

Run content is durable application data, separate from the content-free usage ledger.
"""

import json
import math
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from models.message import Message
from models.reasoning import extract_reasoning_summary
from services.call_usage_store import CallUsageStore
from services.context_budget import ContextChangedError
from services.response_outcome import ResponseOutcome

WORK_RUN_SCHEMA_VERSION = "0.1"
ACTIVE_STATES = {"preparing", "model", "tools", "ready"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted", "stopped"}
PAUSE_RETENTION = timedelta(hours=24)


class WorkContextUnavailableError(ContextChangedError):
    """A referenced run cannot supply its complete original context."""


def _now() -> str:
    """Return an aware UTC timestamp for locally persisted state changes."""
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    """Encode durable JSON without accepting non-finite numbers."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class WorkRunStore:
    """Persist run transitions and resumable boundaries without executing external work."""

    def __init__(self, path: Path | None = None) -> None:
        """Validate the independent schema and recover work left by another process."""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path) if path else ":memory:")
        self.on_attachments_released: Callable[[set[str]], None] | None = None
        self.on_pause_changed: Callable[[], None] | None = None
        self._attachment_lock = RLock()
        self._attachment_owners: dict[str, set[str]] = {}
        self._active_segments: dict[str, float] = {}
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            existing = {name for (name,) in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if existing:
                required = {"work_metadata", "work_runs", "model_steps", "tool_steps"}
                if not required <= existing:
                    raise ValueError("Unsupported work run schema.")
                row = self.connection.execute(
                    "SELECT value FROM work_metadata WHERE key='schema_version'"
                ).fetchone()
                if row is None or row[0] != WORK_RUN_SCHEMA_VERSION:
                    raise ValueError("Unsupported work run schema.")
            self.connection.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE TABLE IF NOT EXISTS work_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS work_runs ("
                "id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
                "status TEXT NOT NULL, data TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS model_steps ("
                "run_id TEXT NOT NULL REFERENCES work_runs(id) ON DELETE CASCADE, "
                "position INTEGER NOT NULL, call_id TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL, response_id TEXT, outcome TEXT, "
                "PRIMARY KEY(run_id, position), UNIQUE(run_id, response_id));"
                "CREATE TABLE IF NOT EXISTS tool_steps ("
                "run_id TEXT NOT NULL REFERENCES work_runs(id) ON DELETE CASCADE, "
                "response_id TEXT NOT NULL, call_id TEXT NOT NULL, "
                "position INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(run_id, response_id, call_id));"
                "CREATE TABLE IF NOT EXISTS work_resume_contexts ("
                "run_id TEXT PRIMARY KEY REFERENCES work_runs(id) ON DELETE CASCADE, "
                "data TEXT NOT NULL);"
            )
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO work_metadata VALUES (?, ?)",
                    ("schema_version", WORK_RUN_SCHEMA_VERSION),
                )
                self._migrate_resume_contexts()
                for (run_id,) in self.connection.execute(
                    "SELECT id FROM work_runs WHERE status IN "
                    "('preparing', 'model', 'tools', 'ready')"
                ).fetchall():
                    self._stop(run_id, "interrupted", "process_exit")
            for (run_id,) in self.connection.execute("SELECT id FROM work_runs").fetchall():
                self._index_attachments(self.get(run_id))
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        """Release the database without changing durable run states."""
        self.connection.close()

    def _migrate_resume_contexts(self) -> None:
        """Move earlier inline checkpoints without loading normal run metadata repeatedly."""
        rows = self.connection.execute(
            "SELECT id, data FROM work_runs "
            "WHERE json_type(data, '$.snapshot.resume_context') IS NOT NULL",
        ).fetchall()
        for run_id, raw in rows:
            run = json.loads(raw)
            snapshot = run.get("snapshot", {})
            context = snapshot.pop("resume_context", None)
            if isinstance(context, dict) and run["status"] in ACTIVE_STATES | {"paused"}:
                self.connection.execute(
                    "INSERT OR IGNORE INTO work_resume_contexts VALUES (?, ?)",
                    (run_id, _json(context)),
                )
                snapshot.update(
                    has_resume_context=True,
                    resume_attachment_sources=context.get("attachment_sources") or [],
                )
            elif run["status"] in TERMINAL_STATES:
                self._clear_checkpoint(run)
            self.connection.execute(
                "UPDATE work_runs SET data=? WHERE id=?", (_json(run), run_id),
            )

    def create(
        self, conversation_id: str, base_version: int, *,
        kind: str = "explore", user_index: int | None = None,
        snapshot: dict | None = None,
    ) -> str:
        """Start an explicit run without making a provider request or selecting tools."""
        if (not isinstance(conversation_id, str) or not conversation_id
                or type(base_version) is not int or base_version < 0
                or kind not in {"explore", "regeneration"}
                or (kind == "regeneration" and (
                    type(user_index) is not int or user_index < 0
                )) or (kind == "explore" and user_index is not None)):
            raise ValueError("Invalid work run metadata.")
        run_id = str(uuid4())
        data = {
            "id": run_id, "conversation_id": conversation_id,
            "base_version": base_version, "kind": kind,
            "user_index": user_index, "status": "preparing",
            "started_at": _now(), "finished_at": None,
        }
        data.update(active_started_at=data["started_at"], active_elapsed_seconds=0.0)
        context = None
        if snapshot is not None:
            data["snapshot"] = json.loads(_json(snapshot))
            context = data["snapshot"].pop("resume_context", None)
            if isinstance(context, dict):
                data["snapshot"].update(
                    has_resume_context=True,
                    resume_attachment_sources=context.get("attachment_sources") or [],
                )
        with self.connection:
            self.connection.execute(
                "INSERT INTO work_runs VALUES (?, ?, ?, ?)",
                (run_id, conversation_id, "preparing", _json(data)),
            )
            if isinstance(context, dict):
                self.connection.execute(
                    "INSERT INTO work_resume_contexts VALUES (?, ?)",
                    (run_id, _json(context)),
                )
        self._index_attachments(data)
        self._active_segments[run_id] = monotonic()
        return run_id

    def get(self, run_id: str, conversation_id: str | None = None) -> dict:
        """Read one run and optionally require the owning conversation."""
        row = self.connection.execute(
            "SELECT data FROM work_runs WHERE id=?", (run_id,),
        ).fetchone()
        if row is None:
            raise WorkContextUnavailableError(
                "Saved work context is missing. Restore its data or start a new conversation to explore again."
            )
        data = json.loads(row[0])
        if conversation_id is not None and data["conversation_id"] != conversation_id:
            raise WorkContextUnavailableError(
                "Work context belongs to another conversation."
            )
        if data["status"] == "paused":
            expires = data.get("pause_expires_at")
            try:
                expired = (
                    not expires or datetime.fromisoformat(expires).tzinfo is None
                    or datetime.fromisoformat(expires) <= datetime.now(timezone.utc)
                )
            except (TypeError, ValueError):
                expired = True
            if expired:
                identities = self._run_attachment_ids(data)
                data.update(status="stopped", reason="pause_expired", finished_at=_now(),
                            pause_requested=False, active_started_at=None)
                self._clear_checkpoint_metadata(data)
                with self.connection:
                    changed = self.connection.execute(
                        "UPDATE work_runs SET status='stopped', data=? "
                        "WHERE id=? AND status='paused'", (_json(data), run_id),
                    ).rowcount
                    if changed:
                        self._clear_checkpoint(data)
                        self.connection.execute(
                            "UPDATE tool_steps SET status='stopped' WHERE run_id=? "
                            "AND status IN ('pending', 'in_progress')", (run_id,),
                        )
                        self.connection.execute(
                            "UPDATE model_steps SET status='stopped' WHERE run_id=? "
                            "AND status='in_progress'", (run_id,),
                        )
                if not changed:
                    return self.get(run_id, conversation_id)
                self._index_attachments(data)
                self._notify_attachments(identities)
                self._notify_pause_changed()
        return data

    def resume_context(self, run_id: str) -> dict | None:
        """Load exact private model input only for explicit checkpoint validation or replay."""
        run = self.get(run_id)
        if not run.get("snapshot", {}).get("has_resume_context"):
            return None
        row = self.connection.execute(
            "SELECT data FROM work_resume_contexts WHERE run_id=?", (run_id,),
        ).fetchone()
        try:
            context = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            raise WorkContextUnavailableError(
                "Saved resume input is damaged. Prepare a new request."
            ) from None
        return context if isinstance(context, dict) else None

    @staticmethod
    def _run_attachment_ids(run: dict) -> set[str]:
        """Collect immutable upload owners only while a run can still use its checkpoint."""
        if run["status"] not in ACTIVE_STATES | {"paused"}:
            return set()
        snapshot = run.get("snapshot", {})
        return {
            source["attachment_id"]
            for source in snapshot.get("resume_attachment_sources") or []
            if isinstance(source, dict) and source.get("source") == "upload"
            and isinstance(source.get("attachment_id"), str)
        }

    @staticmethod
    def _clear_checkpoint_metadata(run: dict) -> None:
        """Remove lightweight continuation metadata before encoding a terminal transition."""
        run.pop("checkpoint", None)
        if isinstance(run.get("snapshot"), dict):
            run["snapshot"].pop("resume_context", None)
            run["snapshot"].pop("has_resume_context", None)
            run["snapshot"].pop("resume_attachment_sources", None)

    def _clear_checkpoint(self, run: dict) -> None:
        """Remove resumable private payloads inside the owner's terminal transaction."""
        self._clear_checkpoint_metadata(run)
        self.connection.execute(
            "DELETE FROM work_resume_contexts WHERE run_id=?", (run["id"],),
        )

    def _notify_attachments(self, identities: set[str]) -> None:
        """Schedule cleanup after ownership changes without undoing durable state."""
        if identities and self.on_attachments_released is not None:
            try:
                self.on_attachments_released(identities)
            except Exception:
                # Cleanup is recoverable; failure must not retry a committed transition.
                pass

    def _notify_pause_changed(self) -> None:
        """Wake the app's deadline scheduler after a dormant-state transition."""
        if self.on_pause_changed is not None:
            try:
                self.on_pause_changed()
            except Exception:
                # A caller can retry scheduling without retrying a durable state change.
                pass

    def _index_attachments(self, run: dict) -> None:
        """Publish committed checkpoint ownership for cleanup worker threads."""
        identities = self._run_attachment_ids(run)
        if run["status"] not in ACTIVE_STATES:
            self._active_segments.pop(run["id"], None)
        with self._attachment_lock:
            if identities:
                self._attachment_owners[run["id"]] = identities
            else:
                self._attachment_owners.pop(run["id"], None)

    def attachment_ids(self) -> set[str]:
        """Read committed upload ownership without touching SQLite from cleanup workers."""
        with self._attachment_lock:
            return set().union(*self._attachment_owners.values())

    def attachment_is_referenced(self, identity: str) -> bool:
        """Check upload ownership using the worker-safe committed reference index."""
        with self._attachment_lock:
            return any(identity in identities for identities in self._attachment_owners.values())

    def next_pause_expiry(self) -> float | None:
        """Return the earliest dormant checkpoint deadline for the main-loop scheduler."""
        values = []
        for (raw,) in self.connection.execute(
            "SELECT json_extract(data, '$.pause_expires_at') FROM work_runs WHERE status='paused'",
        ):
            try:
                value = datetime.fromisoformat(raw)
                if value.tzinfo is not None:
                    values.append(value.timestamp())
                else:
                    return datetime.now(timezone.utc).timestamp()
            except (KeyError, TypeError, ValueError):
                return datetime.now(timezone.utc).timestamp()
        return min(values) if values else None

    def prune_paused(self) -> set[str]:
        """Expire due checkpoints on the owning thread and report released upload owners."""
        released = set()
        for (run_id,) in self.connection.execute(
            "SELECT id FROM work_runs WHERE status='paused'",
        ).fetchall():
            with self._attachment_lock:
                before = self._attachment_owners.get(run_id, set()).copy()
            if self.get(run_id)["status"] != "paused":
                released.update(before)
        return released

    def _state(self, run_id: str, status: str, **details: object) -> None:
        """Update run metadata within the caller's transaction."""
        data = self.get(run_id)
        if status == "paused" or status in TERMINAL_STATES:
            if details.get("reason") == "process_exit":
                data["active_elapsed_seconds"] = data.get("active_elapsed_seconds", 0.0)
                data["active_time_incomplete"] = True
            else:
                data["active_elapsed_seconds"] = self.active_elapsed(run_id)
            data["active_started_at"] = None
            data["pause_requested"] = False
        data.update(status=status, **details)
        if status in TERMINAL_STATES:
            self._clear_checkpoint(data)
            if details.get("reason") == "process_exit":
                data.update(finished_at=None, recovered_at=_now())
            else:
                data["finished_at"] = _now()
        self.connection.execute(
            "UPDATE work_runs SET status=?, data=? WHERE id=?",
            (status, _json(data), run_id),
        )

    def active_elapsed(self, run_id: str) -> float:
        """Return accumulated execution time while excluding completed paused intervals."""
        run = self.get(run_id)
        elapsed = run.get("active_elapsed_seconds", 0.0)
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = 0.0
        if run["status"] not in ACTIVE_STATES:
            return float(elapsed)
        if run_id in self._active_segments:
            return float(elapsed + max(0.0, monotonic() - self._active_segments[run_id]))
        started = run.get("active_started_at")
        if started is None and "active_started_at" not in run:
            started = run.get("started_at") if run["status"] in ACTIVE_STATES else None
        if started:
            try:
                value = datetime.fromisoformat(started)
                if value.tzinfo is not None:
                    elapsed += max(0.0, (datetime.now(timezone.utc) - value).total_seconds())
            except (TypeError, ValueError):
                pass
        return float(elapsed)

    def request_pause(self, run_id: str) -> bool:
        """Record intent to pause without cancelling an in-flight model or tool step."""
        if not self.get(run_id).get("snapshot", {}).get("has_resume_context"):
            return False
        with self.connection:
            changed = self.connection.execute(
                "UPDATE work_runs SET data=json_set(data, '$.pause_requested', json('true')) "
                "WHERE id=? AND status IN ('preparing', 'model', 'tools')",
                (run_id,),
            ).rowcount
        return bool(changed)

    def pause_requested(self, run_id: str) -> bool:
        """Read durable pause intent at a safe step boundary."""
        return self.get(run_id).get("pause_requested") is True

    def _checkpoint_valid(self, run_id: str, phase: str) -> bool:
        """Require complete model output and no claimed tool with an uncertain result."""
        if phase not in {"preparing", "tools"}:
            return False
        steps = self.steps(run_id)
        if not steps:
            return phase == "preparing"
        if phase != "tools":
            return False
        expected = set()
        for step in steps:
            outcome = step["outcome"]
            if (step["status"] != "completed" or not outcome
                    or outcome.get("status") != "completed"
                    or outcome.get("protocol_errors")):
                return False
            for call in outcome.get("function_calls", []):
                expected.add((outcome["response_id"], call["call_id"]))
        rows = self.connection.execute(
            "SELECT response_id, call_id, status, data FROM tool_steps WHERE run_id=?",
            (run_id,),
        ).fetchall()
        if expected != {(response_id, call_id) for response_id, call_id, _, _ in rows}:
            return False
        for _, _, status, raw in rows:
            data = json.loads(raw)
            if status not in {"pending", "completed", "failed"}:
                return False
            if status == "pending":
                if data.get("started") or data.get("output") is not None:
                    return False
            elif not isinstance(data.get("output"), str):
                return False
        return True

    def pause(
        self, run_id: str, *, checkpoint: dict | None = None,
        reason: str = "user_requested",
    ) -> bool:
        """Commit a dormant boundary without marking pending tool requests as cancelled."""
        with self.connection:
            run = self.get(run_id)
            if run["status"] == "paused":
                return True
            if not self._checkpoint_valid(run_id, run["status"]):
                return False
            self._state(
                run_id, "paused", paused_at=_now(), pause_reason=reason,
                pause_expires_at=(datetime.now(timezone.utc) + PAUSE_RETENTION).isoformat(),
                checkpoint={"version": 1, "phase": run["status"],
                            "data": json.loads(_json(checkpoint or {}))},
            )
        self._notify_pause_changed()
        self._active_segments.pop(run_id, None)
        return True

    def claim_resume(self, run_id: str) -> dict:
        """Atomically claim one paused boundary before any external work is restarted."""
        with self.connection:
            run = self.get(run_id)
            checkpoint = run.get("checkpoint", {})
            phase = checkpoint.get("phase")
            if (run["status"] != "paused" or checkpoint.get("version") != 1
                    or not self._checkpoint_valid(run_id, phase)):
                raise ValueError("This work run has no resumable checkpoint.")
            resumed = {
                **run, "status": phase, "pause_requested": False,
                "resumed_at": _now(), "active_started_at": _now(),
            }
            changed = self.connection.execute(
                "UPDATE work_runs SET status=?, data=? WHERE id=? AND status='paused'",
                (phase, _json(resumed), run_id),
            ).rowcount
            if not changed:
                raise ValueError("This work run was already resumed.")
        self._active_segments[run_id] = monotonic()
        self._notify_pause_changed()
        return resumed

    def pending_tools(self, run_id: str) -> list[dict]:
        """Return only unclaimed persisted calls in their original response order."""
        self.get(run_id)
        return [{"response_id": response_id, **json.loads(raw)}
                for response_id, raw in self.connection.execute(
                    "SELECT t.response_id, t.data FROM tool_steps t "
                    "JOIN model_steps m ON m.run_id=t.run_id AND m.response_id=t.response_id "
                    "WHERE t.run_id=? AND t.status='pending' ORDER BY m.position, t.position",
                    (run_id,),
                )]

    def begin_call(
        self, run_id: str, call_id: str, conversation_id: str | None,
    ) -> None:
        """Link one request before submission, refusing open or unfinished prior steps."""
        if conversation_id is None:
            raise ValueError("A work run requires its owning conversation.")
        run = self.get(run_id, conversation_id)
        if run["status"] not in {"preparing", "tools"}:
            raise ValueError("This work run cannot start another model call.")
        if self.connection.execute(
            "SELECT 1 FROM tool_steps WHERE run_id=? "
            "AND status NOT IN ('completed', 'failed')",
            (run_id,),
        ).fetchone():
            raise ValueError("Tool results are incomplete.")
        position = self.connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM model_steps WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                "INSERT INTO model_steps VALUES (?, ?, ?, 'in_progress', NULL, NULL)",
                (run_id, position, call_id),
            )
            self._state(run_id, "model")

    def end_call(self, run_id: str, call_id: str, status: str) -> None:
        """Retain terminal request facts independently of later outcome persistence."""
        identities = self._run_attachment_ids(self.get(run_id))
        with self.connection:
            changed = self.connection.execute(
                "UPDATE model_steps SET status=? WHERE run_id=? AND call_id=? "
                "AND status='in_progress'", (status, run_id, call_id),
            ).rowcount
            if changed and status != "completed":
                self._stop(
                    run_id, status if status in TERMINAL_STATES else "failed",
                    "model_call_ended",
                )
        if changed and status != "completed":
            self._index_attachments(self.get(run_id))
            self._notify_attachments(identities)

    def record_outcome(
        self, run_id: str, call_id: str, outcome: ResponseOutcome,
    ) -> None:
        """Save ordered raw output and queue validated calls without executing them."""
        row = self.connection.execute(
            "SELECT status, outcome FROM model_steps WHERE run_id=? AND call_id=?",
            (run_id, call_id),
        ).fetchone()
        encoded = _json(asdict(outcome))
        if row is None or row[0] == "in_progress":
            raise ValueError("The model call has not reached a terminal state.")
        if row[1] is not None:
            if json.loads(row[1]) != json.loads(encoded):
                raise ValueError("Conflicting response outcome.")
            return
        identities = self._run_attachment_ids(self.get(run_id))
        with self.connection:
            self.connection.execute(
                "UPDATE model_steps SET response_id=?, outcome=? "
                "WHERE run_id=? AND call_id=?",
                (outcome.response_id, encoded, run_id, call_id),
            )
            if self.get(run_id)["status"] != "model":
                return
            if outcome.status != "completed" or outcome.protocol_errors:
                self._stop(run_id, "failed", "response_protocol_error")
            elif outcome.function_calls:
                for call in outcome.function_calls:
                    data = {**asdict(call), "output": None, "references": []}
                    self.connection.execute(
                        "INSERT INTO tool_steps VALUES (?, ?, ?, ?, 'pending', ?)",
                        (run_id, outcome.response_id, call.call_id,
                         call.output_index, _json(data)),
                    )
                self._state(run_id, "tools")
            elif outcome.has_text or outcome.refusal.strip():
                self._state(run_id, "ready")
            else:
                self._stop(run_id, "failed", "empty_response")
        if self.get(run_id)["status"] in TERMINAL_STATES:
            self._index_attachments(self.get(run_id))
            self._notify_attachments(identities)

    def begin_tool(self, run_id: str, response_id: str, call_id: str) -> bool:
        """Claim a pending tool exactly once before any external action begins."""
        if self.get(run_id)["status"] != "tools":
            return False
        with self.connection:
            return bool(self.connection.execute(
                "UPDATE tool_steps SET status='in_progress', "
                "data=json_set(data, '$.started', json('true')) WHERE run_id=? "
                "AND response_id=? AND call_id=? AND status='pending'",
                (run_id, response_id, call_id),
            ).rowcount)

    def tool_result(
        self, run_id: str, response_id: str, call_id: str, output: str, *,
        failed: bool = False, references: list[dict] | None = None,
    ) -> None:
        """Record the original tool result once, rejecting conflicting retransmissions."""
        if not isinstance(output, str):
            raise ValueError("Tool output must be a string.")
        row = self.connection.execute(
            "SELECT status, data FROM tool_steps "
            "WHERE run_id=? AND response_id=? AND call_id=?",
            (run_id, response_id, call_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown tool request.")
        data = json.loads(row[1])
        result = {**data, "output": output, "references": references or []}
        status = "failed" if failed else "completed"
        if row[0] in {"completed", "failed"}:
            if row[0] != status or _json(data) != _json(result):
                raise ValueError("Conflicting tool result.")
            return
        if (self.get(run_id)["status"] != "tools"
                or row[0] not in {"pending", "in_progress"}):
            raise ValueError("The work run no longer accepts tool results.")
        with self.connection:
            self.connection.execute(
                "UPDATE tool_steps SET status=?, data=? "
                "WHERE run_id=? AND response_id=? AND call_id=?",
                (status, _json(result), run_id, response_id, call_id),
            )

    def steps(self, run_id: str) -> list[dict]:
        """Return complete model steps in request order, without a page truncation."""
        self.get(run_id)
        return [{
            "call_id": call_id, "status": status,
            "outcome": json.loads(raw) if raw else None,
        } for call_id, status, raw in self.connection.execute(
            "SELECT call_id, status, outcome FROM model_steps "
            "WHERE run_id=? ORDER BY position", (run_id,),
        )]

    def history(self, conversation_id: str, *, before: int | None = None,
                limit: int = 20, messages: list[Message] | None = None) -> dict:
        """Page run metadata without loading any provider output or tool results."""
        if (not 1 <= limit <= 50
                or (before is not None and not 1 <= before < 2 ** 63)):
            raise ValueError("Invalid work history page.")
        rows = self.connection.execute(
            "SELECT rowid, data FROM work_runs WHERE conversation_id=? "
            "AND (? IS NULL OR rowid < ?) ORDER BY rowid DESC LIMIT ?",
            (conversation_id, before, before, limit + 1),
        ).fetchall()
        return {
            "runs": [
                self.public_metadata(json.loads(raw), messages=messages)
                for _, raw in rows[:limit]
            ],
            "next_before": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def public_metadata(
        self, run: dict, *, messages: list[Message] | None = None,
    ) -> dict:
        """Project display metadata while omitting the host root and internal policies."""
        run = self.get(run["id"])
        snapshot = run.get("snapshot", {})
        root = snapshot.get("workspace", {}).get("root")
        profile = snapshot.get("profile", {})
        workspace_name = (
            root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if root else None
        )
        return {
            **{key: run.get(key) for key in (
                "id", "conversation_id", "status", "kind", "started_at",
                "finished_at", "recovered_at", "reason", "user_index",
                "pause_requested", "paused_at", "pause_expires_at", "pause_reason",
                "active_time_incomplete",
            )},
            "active_elapsed_seconds": self.active_elapsed(run["id"]),
            "pause_available": (
                run["status"] in {"preparing", "model", "tools"}
                and snapshot.get("has_resume_context") is True
            ),
            "resume_available": (
                run["status"] == "paused" and snapshot.get("has_resume_context") is True
                and run.get("checkpoint", {}).get("version") == 1
                and run.get("checkpoint", {}).get("phase") in {"preparing", "tools"}
            ),
            "workspace_name": workspace_name,
            "has_tool_activity": self.has_tool_activity(run["id"]),
            "profile": {
                key: profile.get(key) for key in ("id", "name", "deployment")
            },
            "goals": snapshot.get("goals", {}),
            "failure": WorkRunStore.failure_details(run.get("failure")),
            "message_link": WorkRunStore.message_link(run, messages or []),
        }

    @staticmethod
    def failure_details(failure: dict | None) -> dict:
        """Allow only content-free diagnostics already intended for client feedback."""
        if not isinstance(failure, dict):
            return {}
        safe = {}
        for key in ("code", "category", "phase", "response_status"):
            value = failure.get(key)
            if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value):
                safe[key] = value
        request_id = failure.get("request_id")
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,256}", request_id):
            safe["request_id"] = request_id
        status = failure.get("http_status")
        if type(status) is int and 100 <= status <= 599:
            safe["http_status"] = status
        retry = failure.get("retry_after_seconds")
        if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0:
            safe["retry_after_seconds"] = retry
        return safe

    def record_failure(self, run_id: str, failure: dict) -> None:
        """Attach safe terminal feedback without changing finish time, steps, or accounting."""
        run = self.get(run_id)
        if run["status"] not in {"failed", "stopped"}:
            return
        run["failure"] = self.failure_details(failure)
        with self.connection:
            self.connection.execute(
                "UPDATE work_runs SET data=? WHERE id=?", (_json(run), run_id),
            )

    @staticmethod
    def message_link(run: dict, messages: list[Message]) -> dict:
        """Resolve only exact current transcript references, leaving old branches unlinked."""
        empty = {"user_index": None, "assistant_index": None}
        answers = [
            index for index, message in enumerate(messages)
            if message.role == "assistant" and message.run_id == run["id"]
        ]
        if len(answers) > 1:
            return empty
        if answers:
            index = answers[0]
            return {
                "user_index": (
                    index - 1 if index > 0 and messages[index - 1].role == "user"
                    else None
                ),
                "assistant_index": index,
            }
        source = run.get("snapshot", {}).get("source_message")
        if not isinstance(source, dict):
            return empty
        index = source.get("user_index")
        if type(index) is not int or not 0 <= index < len(messages):
            return empty
        message = messages[index]
        if (message.role != "user"
                or source.get("created_at") != message.created_at.isoformat()
                or source.get("content_sha256") != sha256(
                    message.content.encode("utf-8")
                ).hexdigest()):
            return empty
        return {"user_index": index, "assistant_index": None}

    def activity(self, run_id: str, *, offset: int = 0, limit: int = 30) -> dict:
        """Page ordered model and tool facts without returning opaque model content."""
        self.get(run_id)
        if not 1 <= limit <= 50 or not 0 <= offset < 2 ** 63:
            raise ValueError("Invalid work activity page.")
        rows = self.connection.execute(
            "SELECT kind, call_id, status, response_id FROM ("
            "SELECT 'model' AS kind, call_id, status, response_id, "
            "position AS model_position, -1 AS tool_position "
            "FROM model_steps WHERE run_id=? UNION ALL "
            "SELECT 'tool', t.call_id, t.status, t.response_id, "
            "m.position, t.position "
            "FROM tool_steps t JOIN model_steps m ON m.run_id=t.run_id "
            "AND m.response_id=t.response_id WHERE t.run_id=?"
            ") ORDER BY model_position, tool_position LIMIT ? OFFSET ?",
            (run_id, run_id, limit + 1, offset),
        ).fetchall()
        names = {
            "helios_project_list_files", "helios_project_search_text",
            "helios_project_read_file",
        }
        steps = []
        for kind, call_id, status, response_id in rows[:limit]:
            name, available = None, False
            if kind == "tool":
                name, available = self.connection.execute(
                    "SELECT json_extract(data, '$.name'), "
                    "json_type(data, '$.output')='text' FROM tool_steps "
                    "WHERE run_id=? AND response_id=? AND call_id=?",
                    (run_id, response_id, call_id),
                ).fetchone()
            steps.append({
                "kind": kind, "call_id": call_id, "status": status,
                "response_id": response_id,
                "name": name if name in names else None,
                "result_available": bool(available),
            })
            if kind == "model":
                parts = self._call_reasoning_summary(run_id, call_id, status)
                if parts:
                    steps[-1]["reasoning_summary"] = parts
                text = self._call_intermediate_text(run_id, call_id)
                if text:
                    steps[-1]["intermediate_text"] = text
        return {
            "steps": steps,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def _call_intermediate_text(self, run_id: str, call_id: str) -> str:
        """Project public progress messages without loading opaque output or final answers."""
        rows = self.connection.execute(
            "SELECT CASE json_extract(part.value, '$.type') "
            "WHEN 'output_text' THEN json_extract(part.value, '$.text') "
            "WHEN 'refusal' THEN json_extract(part.value, '$.refusal') END "
            "FROM model_steps m, json_each(m.outcome, '$.output') item, "
            "json_each(item.value, '$.content') part "
            "WHERE m.run_id=? AND m.call_id=? "
            "AND json_extract(item.value, '$.type')='message' "
            "AND (json_extract(m.outcome, '$.status')!='completed' "
            "OR json_array_length(m.outcome, '$.protocol_errors')>0 "
            "OR length(trim(json_extract(m.outcome, '$.text')))=0 "
            "OR EXISTS (SELECT 1 FROM json_each(m.outcome, '$.output') tool "
            "WHERE json_extract(tool.value, '$.type')='function_call')) "
            "ORDER BY CAST(item.key AS INTEGER), CAST(part.key AS INTEGER)",
            (run_id, call_id),
        ).fetchall()
        return "\n".join(text for (text,) in rows if isinstance(text, str) and text.strip())

    def evidence(self, run_id: str, response_id: str, call_id: str) -> dict:
        """Return one saved tool result, never resolve a model-supplied filesystem path."""
        self.get(run_id)
        row = self.connection.execute(
            "SELECT data FROM tool_steps WHERE run_id=? AND response_id=? AND call_id=?",
            (run_id, response_id, call_id),
        ).fetchone()
        data = json.loads(row[0]) if row else {}
        if data.get("output") is None:
            raise WorkContextUnavailableError("This saved tool result is unavailable.")
        return {"output": data["output"], "references": data.get("references", [])}

    def model_call_count(self, run_id: str) -> int:
        """Count submitted model steps without loading their stored response bodies."""
        self.get(run_id)
        return self.connection.execute(
            "SELECT COUNT(*) FROM model_steps WHERE run_id=?", (run_id,),
        ).fetchone()[0]

    def has_tool_activity(self, run_id: str) -> bool:
        """Detect claimed calls and legacy results without counting unstarted plans."""
        self.get(run_id)
        return bool(self.connection.execute(
            "SELECT EXISTS(SELECT 1 FROM tool_steps WHERE run_id=? AND ("
            "json_extract(data, '$.started')=1 OR status='in_progress' OR "
            "json_type(data, '$.output')='text'))", (run_id,),
        ).fetchone()[0])

    def usage(self, run_id: str, call_store: CallUsageStore) -> dict:
        """Aggregate the ledger against every durable model-call reference."""
        self.get(run_id)
        call_ids = [call_id for (call_id,) in self.connection.execute(
            "SELECT call_id FROM model_steps WHERE run_id=? ORDER BY position",
            (run_id,),
        )]
        return call_store.run_totals(
            run_id, expected_call_ids=call_ids,
        )

    def response_metadata(
        self, run_id: str, conversation_id: str, call_store: CallUsageStore,
    ) -> dict:
        """Project whole-response display facts without persisting aggregate usage twice."""
        run = self.get(run_id, conversation_id)
        usage = self.usage(run_id, call_store)
        totals = usage["totals"]
        profile = run.get("snapshot", {}).get("profile", {})
        recorded = totals["calls"]
        metadata = {
            "usage_scope": "work_run", "response_calls": usage["expected_calls"],
            "has_tool_activity": self.has_tool_activity(run_id),
            "recorded_response_calls": recorded,
            "usage_status": totals["usage_status"] if recorded else "unavailable",
            "cost_status": totals["cost_status"] if recorded else "unavailable",
            "coverage_incomplete": totals["coverage_incomplete"],
            "partial_token_fields": [],
            "estimated_cost": (
                totals["known_estimated_cost"]
                if recorded and totals["cost_status"] != "unavailable" else None
            ),
            **{key: profile.get(source) for key, source in (
                ("profile_id", "id"), ("profile_name", "name"),
                ("deployment", "deployment"),
            ) if profile.get(source) is not None},
        }
        for field in (
            "input_tokens", "output_tokens", "total_tokens",
            "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens",
        ):
            metadata[field] = (
                totals[field] if recorded > totals[f"unknown_{field}_calls"]
                else None
            )
            if metadata[field] is not None and (
                totals[f"unknown_{field}_calls"] or usage["missing_call_ids"]
            ):
                metadata["partial_token_fields"].append(field)
        metadata["response_time_ms"] = None
        if "active_elapsed_seconds" in run and run.get("finished_at"):
            metadata["response_time_ms"] = self.active_elapsed(run_id) * 1000
            metadata["active_time"] = True
        elif run.get("started_at") and run.get("finished_at"):
            try:
                start = datetime.fromisoformat(run["started_at"])
                finish = datetime.fromisoformat(run["finished_at"])
                if start.tzinfo is not None and finish.tzinfo is not None:
                    elapsed = (finish - start).total_seconds() * 1000
                    if elapsed >= 0:
                        metadata["response_time_ms"] = elapsed
            except (TypeError, ValueError):
                pass
        return metadata

    def context(self, run_id: str, conversation_id: str, *,
                completed: bool = True) -> list[dict]:
        """Replay stored outputs and results without reading the current project files."""
        run = self.get(run_id, conversation_id)
        allowed = {"completed"} if completed else {"tools", "ready", "completed"}
        if run["status"] not in allowed:
            raise WorkContextUnavailableError("Saved work context is incomplete.")
        items = []
        for step in self.steps(run_id):
            outcome = step["outcome"]
            if step["status"] != "completed" or not outcome:
                raise WorkContextUnavailableError("A saved model response is missing.")
            items.extend(
                {**item, "summary": []} if item.get("type") == "reasoning" else item
                for item in outcome["output"]
            )
            for call in outcome["function_calls"]:
                row = self.connection.execute(
                    "SELECT data FROM tool_steps WHERE run_id=? "
                    "AND response_id=? AND call_id=? "
                    "AND status IN ('completed', 'failed')",
                    (run_id, outcome["response_id"], call["call_id"]),
                ).fetchone()
                if row is None or json.loads(row[0])["output"] is None:
                    raise WorkContextUnavailableError("A saved tool result is missing.")
                items.append({
                    "type": "function_call_output", "call_id": call["call_id"],
                    "output": json.loads(row[0])["output"],
                })
        return items

    def reasoning_summary(self, run_id: str) -> list[dict]:
        """Collect public summaries for display without changing provider replay output."""
        self.get(run_id)
        return [part for call_id, status in self.connection.execute(
            "SELECT call_id, status FROM model_steps WHERE run_id=? ORDER BY position",
            (run_id,),
        ) for part in self._call_reasoning_summary(run_id, call_id, status)]

    def _call_reasoning_summary(self, run_id: str, call_id: str, status: str) -> list[dict]:
        """Project public summary fields in SQL without loading private reasoning or tool payloads."""
        result = []
        rows = self.connection.execute(
            "SELECT j.key, json_extract(j.value, '$.id'), json_extract(j.value, '$.summary') "
            "FROM model_steps m, json_each(m.outcome, '$.output') j "
            "WHERE m.run_id=? AND m.call_id=? AND json_extract(j.value, '$.type')='reasoning' "
            "AND json_type(j.value, '$.summary')='array' "
            "ORDER BY CAST(j.key AS INTEGER)", (run_id, call_id),
        )
        for index, item_id, raw in rows:
            output = [{"type": "reasoning", "id": item_id,
                       "summary": json.loads(raw) if raw else []}]
            result.extend({**part, "output_index": index} for part in extract_reasoning_summary(
                output, call_id, status="completed" if status == "completed" else "incomplete",
            ))
        return result

    def answer(self, run_id: str) -> str:
        """Read only the last response's visible text for atomic transcript adoption."""
        self.get(run_id)
        row = self.connection.execute(
            "SELECT outcome FROM model_steps WHERE run_id=? "
            "ORDER BY position DESC LIMIT 1", (run_id,),
        ).fetchone()
        outcome = json.loads(row[0]) if row and row[0] else None
        if not outcome or outcome["function_calls"] or outcome["protocol_errors"]:
            raise ValueError("No final answer is available.")
        return "".join(
            part.get("text", "") if part["type"] == "output_text"
            else part.get("refusal", "")
            for item in outcome["output"] if item.get("type") == "message"
            for part in item.get("content", [])
            if part["type"] in {"output_text", "refusal"}
        )

    def finish(self, run_id: str, status: str, *, reason: str | None = None) -> None:
        """Finalize a run once, leaving recorded calls and original results intact."""
        if status not in TERMINAL_STATES:
            raise ValueError("Invalid terminal work run state.")
        run = self.get(run_id)
        identities = self._run_attachment_ids(run)
        if run["status"] in TERMINAL_STATES:
            if run["status"] != status:
                raise ValueError("The work run is already terminal.")
            return
        if status == "completed":
            if run["status"] != "ready" or not self.answer(run_id).strip():
                raise ValueError("The work run has no complete final answer.")
            self.context(run_id, run["conversation_id"], completed=False)
        with self.connection:
            self._stop(run_id, status, reason)
        self._index_attachments(self.get(run_id))
        self._notify_attachments(identities)
        if run["status"] == "paused":
            self._notify_pause_changed()

    def _stop(self, run_id: str, status: str, reason: str | None) -> None:
        """Close pending steps in the surrounding run-state transaction."""
        self._state(run_id, status, reason=reason)
        self.connection.execute(
            "UPDATE model_steps SET status=? WHERE run_id=? AND status='in_progress'",
            (status, run_id),
        )
        self.connection.execute(
            "UPDATE tool_steps SET status=? WHERE run_id=? "
            "AND status IN ('pending', 'in_progress')",
            (status, run_id),
        )

    def delete(self, run_id: str) -> None:
        """Delete dormant or terminal content while retaining separate usage rows."""
        run = self.get(run_id)
        if run["status"] not in TERMINAL_STATES | {"paused"}:
            raise ValueError("Active work runs cannot be deleted.")
        identities = self._run_attachment_ids(run)
        with self.connection:
            self.connection.execute("DELETE FROM work_runs WHERE id=?", (run_id,))
        with self._attachment_lock:
            self._attachment_owners.pop(run_id, None)
        self._active_segments.pop(run_id, None)
        self._notify_attachments(identities)
        if run["status"] == "paused":
            self._notify_pause_changed()

    def forget_conversation(self, conversation_id: str) -> None:
        """Discard dormant checkpoints for a deleted conversation while retaining its history."""
        for (run_id,) in self.connection.execute(
            "SELECT id FROM work_runs WHERE conversation_id=? AND status='paused'",
            (conversation_id,),
        ).fetchall():
            self.finish(run_id, "cancelled", reason="conversation_deleted")

    def paused_run(self, conversation_id: str) -> dict | None:
        """Find unresolved paused work without discarding its input when the branch changes."""
        for (run_id,) in self.connection.execute(
            "SELECT id FROM work_runs WHERE conversation_id=? AND status='paused' "
            "ORDER BY rowid DESC", (conversation_id,),
        ).fetchall():
            run = self.get(run_id, conversation_id)
            if run["status"] == "paused":
                return run
        return None
