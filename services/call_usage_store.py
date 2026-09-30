"""Persist API call history independently of transcripts and aggregate it by local date."""

import json
import math
import sqlite3
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "uncached_input_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)
USAGE_FIELDS = (
    *TOKEN_FIELDS, "estimated_cost", "usage_status", "cost_status",
    "response_time_ms", "is_long_context", "provider_usage",
)


def utc_timestamp(value: datetime | str | None) -> str | None:
    """Normalize aware timestamps while leaving absent or ambiguous legacy times unknown."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _count(value: object) -> int | None:
    """Accept only nonnegative integer token counts from live or historical usage."""
    return value if type(value) is int and value >= 0 else None


class CallUsageStore:
    """Keep one durable row per request attempt in a single server process."""

    def __init__(self, path: Path | None = None) -> None:
        """Initialize the ledger, tracking boundary, and interrupted-call recovery."""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path) if path else ":memory:")
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS usage_calls ("
            "id TEXT PRIMARY KEY, conversation_id TEXT, kind TEXT NOT NULL, "
            "status TEXT NOT NULL, started_at TEXT, source TEXT NOT NULL, "
            "data TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS usage_calls_date "
            "ON usage_calls(started_at);"
            "CREATE INDEX IF NOT EXISTS usage_calls_conversation "
            "ON usage_calls(conversation_id, kind, started_at);"
            "CREATE TABLE IF NOT EXISTS usage_imports (id TEXT PRIMARY KEY);"
            "CREATE TABLE IF NOT EXISTS usage_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        now = utc_timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO usage_metadata VALUES (?, ?)",
                ("tracking_started_at", now),
            )
        self.tracking_started_at = self.connection.execute(
            "SELECT value FROM usage_metadata WHERE key = ?",
            ("tracking_started_at",),
        ).fetchone()[0]
        self.recording_errors = 0
        self.import_errors = 0
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        """Mark attempts left open by a previous process without inventing final usage."""
        rows = self.connection.execute(
            "SELECT data FROM usage_calls WHERE status = 'in_progress'"
        ).fetchall()
        with self.connection:
            for (raw,) in rows:
                record = json.loads(raw)
                record.update(
                    status="interrupted", interruption_reason="process_exit",
                    finished_at=None,
                    recovered_at=utc_timestamp(datetime.now(timezone.utc)),
                )
                self.record(record)

    def record(self, record: dict) -> None:
        """Upsert an attempt snapshot without resetting a terminal row to in-progress."""
        record = deepcopy(record)
        record["started_at"] = utc_timestamp(record.get("started_at"))
        record["recorded_at"] = utc_timestamp(datetime.now(timezone.utc))
        with self.connection:
            self.connection.execute(
                "INSERT INTO usage_calls VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                "data=excluded.data WHERE usage_calls.status='in_progress' "
                "OR excluded.status!='in_progress'",
                (record["id"], record.get("conversation_id"), record["kind"],
                 record["status"], record["started_at"], record["source"],
                 json.dumps(record, ensure_ascii=False, allow_nan=False)),
            )

    def _import(self, key: str, records: list[dict]) -> None:
        """Import one legacy snapshot transactionally and mark it only after success."""
        with self.connection:
            if self.connection.execute(
                "SELECT 1 FROM usage_imports WHERE id = ?", (key,)
            ).fetchone():
                return
            for record in records:
                record["started_at"] = utc_timestamp(record.get("started_at"))
                self.connection.execute(
                    "INSERT OR IGNORE INTO usage_calls VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (record["id"], record.get("conversation_id"), record["kind"],
                     record["status"], record["started_at"], record["source"],
                     json.dumps(record, ensure_ascii=False, allow_nan=False)),
                )
            self.connection.execute(
                "INSERT INTO usage_imports VALUES (?)", (key,)
            )

    def import_conversation(self, conversation: object) -> None:
        """Preserve saved replies once, without importing their content or repricing them."""
        records = []
        for index, message in enumerate(conversation.messages):
            if message.role != "assistant":
                continue
            data = message.to_dict()
            usage = {
                name: data.get(name) for name in USAGE_FIELDS
            } if any(name in data for name in USAGE_FIELDS) else None
            records.append({
                "id": data.get("usage_call_id") or (
                    f"legacy-reply:{conversation.id}:{index}"
                ),
                "conversation_id": conversation.id,
                "kind": "legacy_reply", "status": "completed",
                "source": "legacy_message", "usage": usage,
                "started_at": data["created_at"],
                "timestamp_basis": "message_created_at",
                "profile_id": data.get("profile_id"),
                "profile_name": data.get("profile_name"),
                "deployment": data.get("deployment"),
                "price_snapshot": None,
            })
        self._import(f"conversation:{conversation.id}", records)

    def import_summaries(self, store: object) -> None:
        """Import old summary rows using call references to avoid live-row duplication."""
        for record_id, data in store.records():
            record = {
                "id": data.get("usage_call_id") or f"legacy-summary:{record_id}",
                "conversation_id": data.get("conversation_id"),
                "kind": "summary", "status": data["status"],
                "source": "legacy_summary", "usage": data.get("usage"),
                "started_at": data.get("recorded_at"),
                "timestamp_basis": "summary_recorded_at",
                "profile_id": data.get("profile_id"),
                "deployment": data.get("deployment"),
                "response_id": data.get("response_id"),
                "incomplete_reason": data.get("incomplete_reason"),
                "price_snapshot": None,
            }
            self._import(f"summary:{record_id}", [record])

    def _selection(
        self, conversation_id: str | None, kind: str | None,
        timezone_name: str, start_date: date | None, end_date: date | None,
    ) -> tuple[str, list, object, str | None]:
        """Build a parameterized date selection using timezone-aware midnight boundaries."""
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date must not be after end_date.")
        try:
            zone = (
                timezone.utc if timezone_name == "UTC"
                else ZoneInfo(timezone_name)
            )
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown or unavailable IANA timezone.") from exc
        conditions, parameters = [], []
        for name, value in (("conversation_id", conversation_id), ("kind", kind)):
            if value is not None:
                conditions.append(f"{name} = ?")
                parameters.append(value)
        lower = None
        if start_date:
            lower = utc_timestamp(datetime.combine(start_date, time.min, zone))
            conditions.append("started_at >= ?")
            parameters.append(lower)
        if end_date:
            if end_date == date.max:
                raise ValueError("end_date must be before 9999-12-31.")
            upper = utc_timestamp(datetime.combine(
                end_date + timedelta(days=1), time.min, zone
            ))
            conditions.append("started_at < ?")
            parameters.append(upper)
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        return clause, parameters, zone, lower

    def records(
        self, *, conversation_id: str | None = None, kind: str | None = None,
        timezone_name: str = "UTC", start_date: date | None = None,
        end_date: date | None = None, limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        """Return a bounded page of call metadata without prompts or generated text."""
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("limit must be 1..200 and offset must be nonnegative.")
        clause, parameters, _, _ = self._selection(
            conversation_id, kind, timezone_name, start_date, end_date
        )
        rows = self.connection.execute(
            "SELECT data FROM usage_calls" + clause
            + " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            [*parameters, limit, offset],
        )
        return [json.loads(raw) for (raw,) in rows]

    def _totals(self, records: list[dict]) -> dict:
        """Sum only known categories while keeping unknown costs and detail counts explicit."""
        result = {
            "calls": len(records), **{name: 0 for name in TOKEN_FIELDS},
            "known_estimated_cost": 0.0, "unknown_usage_calls": 0,
            "unknown_cost_calls": 0, "partial_cost_calls": 0,
            "unknown_cost_completeness_calls": 0,
            "unknown_timestamp_calls": 0, "legacy_calls": 0,
            "statuses": {}, "kinds": {},
            **{f"unknown_{name}_calls": 0 for name in TOKEN_FIELDS},
        }
        for record in records:
            for field, target in (("status", "statuses"), ("kind", "kinds")):
                value = record[field]
                result[target][value] = result[target].get(value, 0) + 1
            result["legacy_calls"] += record["source"] != "live"
            result["unknown_timestamp_calls"] += record.get("started_at") is None
            usage = record.get("usage") or {}
            counts = [_count(usage.get(name)) for name in TOKEN_FIELDS[:3]]
            if (
                any(value is None for value in counts)
                or counts[2] != counts[0] + counts[1]
                or usage.get("usage_status") in {"partial", "unavailable"}
            ):
                result["unknown_usage_calls"] += 1
            for name in TOKEN_FIELDS:
                value = _count(usage.get(name))
                if value is None:
                    result[f"unknown_{name}_calls"] += 1
                else:
                    result[name] += value
            cost = usage.get("estimated_cost")
            if (
                not isinstance(cost, (int, float)) or isinstance(cost, bool)
                or not math.isfinite(cost) or cost < 0
            ):
                result["unknown_cost_calls"] += 1
            else:
                result["known_estimated_cost"] += cost
                if usage.get("cost_status") == "partial":
                    result["partial_cost_calls"] += 1
                elif usage.get("cost_status") != "complete":
                    result["unknown_cost_completeness_calls"] += 1
        result["known_estimated_cost"] = round(result["known_estimated_cost"], 8)
        if records and all(
            result[f"unknown_{name}_calls"] == len(records)
            for name in TOKEN_FIELDS[:3]
        ):
            result["usage_status"] = "unavailable"
        else:
            result["usage_status"] = (
                "partial" if result["unknown_usage_calls"] else "available"
            )
        if records and result["unknown_cost_calls"] == len(records):
            result["cost_status"] = "unavailable"
        elif any(result[name] for name in (
            "unknown_cost_calls", "partial_cost_calls",
            "unknown_cost_completeness_calls",
        )):
            result["cost_status"] = "partial"
        else:
            result["cost_status"] = "complete"
        return result

    def _coverage(self, result: dict, incomplete: bool) -> dict:
        """Qualify aggregate completeness separately from completeness of recorded rows."""
        result["recorded_usage_status"] = result["usage_status"]
        result["recorded_cost_status"] = result["cost_status"]
        result["coverage_incomplete"] = incomplete
        if incomplete:
            for field in ("usage_status", "cost_status"):
                if result[field] != "unavailable":
                    result[field] = "partial"
        return result

    def totals(
        self, *, conversation_id: str | None = None, kind: str | None = None,
        timezone_name: str = "UTC", start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """Return cumulative and local-date usage with explicit historical coverage limits."""
        clause, parameters, zone, lower = self._selection(
            conversation_id, kind, timezone_name, start_date, end_date
        )
        records = [json.loads(raw) for (raw,) in self.connection.execute(
            "SELECT data FROM usage_calls" + clause, parameters
        )]
        by_date = defaultdict(list)
        for record in records:
            timestamp = record.get("started_at")
            day = (
                datetime.fromisoformat(timestamp).astimezone(zone)
                .date().isoformat()
                if timestamp else "unknown"
            )
            by_date[day].append(record)
        unknown_timestamps_excluded = 0
        if start_date or end_date:
            base_clause, base_parameters, _, _ = self._selection(
                conversation_id, kind, timezone_name, None, None
            )
            null_clause = (
                " AND started_at IS NULL" if base_clause
                else " WHERE started_at IS NULL"
            )
            unknown_timestamps_excluded = self.connection.execute(
                "SELECT COUNT(*) FROM usage_calls" + base_clause + null_clause,
                base_parameters,
            ).fetchone()[0]
        untracked_history = lower is None or lower < self.tracking_started_at
        storage_errors = bool(self.recording_errors or self.import_errors)
        daily = []
        for day in sorted(by_date):
            local_start = (
                utc_timestamp(datetime.combine(
                    date.fromisoformat(day), time.min, zone
                ))
                if day != "unknown" else None
            )
            incomplete = (
                local_start is None or local_start < self.tracking_started_at
                or storage_errors or unknown_timestamps_excluded > 0
            )
            daily.append({
                "date": day if day != "unknown" else None,
                **self._coverage(self._totals(by_date[day]), incomplete),
            })
        return {
            "timezone": timezone_name,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "tracking_started_at": self.tracking_started_at,
            "historical_coverage_complete": False,
            "range_includes_untracked_history": untracked_history,
            "recording_errors": self.recording_errors,
            "import_errors": self.import_errors,
            "unknown_timestamp_calls_excluded": unknown_timestamps_excluded,
            "totals": self._coverage(self._totals(records), (
                untracked_history or storage_errors
                or unknown_timestamps_excluded > 0
            )),
            "by_date": daily,
        }

    def close(self) -> None:
        """Close the ledger database."""
        self.connection.close()
