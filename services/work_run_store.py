"""Retain work runs, ordered provider output, and original tool results.

Run content is durable application data, separate from the content-free usage ledger.
"""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from services.call_usage_store import CallUsageStore
from services.context_budget import ContextChangedError
from services.response_outcome import ResponseOutcome

WORK_RUN_SCHEMA_VERSION = "0.1"
ACTIVE_STATES = {"preparing", "model", "tools", "ready"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted", "stopped"}


class WorkContextUnavailableError(ContextChangedError):
    """A referenced run cannot supply its complete original context."""


def _now() -> str:
    """Return an aware UTC timestamp for locally persisted state changes."""
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    """Encode durable JSON without accepting non-finite numbers."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class WorkRunStore:
    """Manage synchronous run transitions without executing or resuming any work."""

    def __init__(self, path: Path | None = None) -> None:
        """Validate the independent schema and recover work left by another process."""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path) if path else ":memory:")
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
            )
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO work_metadata VALUES (?, ?)",
                    ("schema_version", WORK_RUN_SCHEMA_VERSION),
                )
                for (run_id,) in self.connection.execute(
                    "SELECT id FROM work_runs WHERE status IN "
                    "('preparing', 'model', 'tools', 'ready')"
                ).fetchall():
                    self._stop(run_id, "interrupted", "process_exit")
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        """Release the database without changing durable run states."""
        self.connection.close()

    def create(
        self, conversation_id: str, base_version: int, *,
        kind: str = "explore", user_index: int | None = None,
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
        with self.connection:
            self.connection.execute(
                "INSERT INTO work_runs VALUES (?, ?, ?, ?)",
                (run_id, conversation_id, "preparing", _json(data)),
            )
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
        return data

    def _state(self, run_id: str, status: str, **details: object) -> None:
        """Update run metadata within the caller's transaction."""
        data = self.get(run_id)
        data.update(status=status, **details)
        if status in TERMINAL_STATES:
            if details.get("reason") == "process_exit":
                data.update(finished_at=None, recovered_at=_now())
            else:
                data["finished_at"] = _now()
        self.connection.execute(
            "UPDATE work_runs SET status=?, data=? WHERE id=?",
            (status, _json(data), run_id),
        )

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

    def begin_tool(self, run_id: str, response_id: str, call_id: str) -> bool:
        """Claim a pending tool exactly once before any external action begins."""
        if self.get(run_id)["status"] != "tools":
            return False
        with self.connection:
            return bool(self.connection.execute(
                "UPDATE tool_steps SET status='in_progress' WHERE run_id=? "
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
            items.extend(outcome["output"])
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
        """Explicitly delete terminal content without deleting its separate usage rows."""
        if self.get(run_id)["status"] not in TERMINAL_STATES:
            raise ValueError("Active work runs cannot be deleted.")
        with self.connection:
            self.connection.execute("DELETE FROM work_runs WHERE id=?", (run_id,))
