"""Store summary-call usage in SQLite, including responses whose summaries are discarded."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class SummaryUsageStore:
    """Persist summary-call records independently of whether their memory is adopted."""

    def __init__(self, path: Path | None = None) -> None:
        """Open file-backed or in-memory SQLite storage and initialize its usage table."""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path) if path else ":memory:")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS summary_usage (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self.connection.commit()
        self.recording_errors = 0

    def record(self, **data: object) -> None:
        """Append a timestamped summary-call record within a database transaction."""
        data["recorded_at"] = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT INTO summary_usage VALUES (?, ?)",
                (str(uuid4()), json.dumps(data, ensure_ascii=False)),
            )

    def totals(self, conversation_id: str | None = None) -> dict:
        """Aggregate usage and known costs while counting missing data and recording failures."""
        result = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "known_estimated_cost": 0.0,
            "unknown_usage_calls": 0,
            "unknown_cost_calls": 0,
            "statuses": {},
            "recording_errors": self.recording_errors,
        }
        for (raw,) in self.connection.execute(
            "SELECT data FROM summary_usage"
        ):
            record = json.loads(raw)
            if (
                conversation_id is not None
                and record["conversation_id"] != conversation_id
            ):
                continue
            result["calls"] += 1
            status = record["status"]
            result["statuses"][status] = result["statuses"].get(status, 0) + 1
            usage = record["usage"]
            if usage is None:
                result["unknown_usage_calls"] += 1
            else:
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    result[key] += usage[key]
            if usage is None or usage["estimated_cost"] is None:
                result["unknown_cost_calls"] += 1
            else:
                result["known_estimated_cost"] += usage["estimated_cost"]
        result["known_estimated_cost"] = round(
            result["known_estimated_cost"], 8
        )
        return result

    def close(self) -> None:
        """Close the SQLite connection."""
        self.connection.close()
