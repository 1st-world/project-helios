"""Collect public reasoning-summary stream events separately from answer text and replay state."""

from models.reasoning import extract_reasoning_summary


class ReasoningSummaryStream:
    """Reconcile deltas and completed snapshots without duplicating summary text."""

    def __init__(self, call_id: str) -> None:
        """Bind display parts to one locally recorded model call."""
        self.call_id = call_id
        self._parts: dict[tuple[str, int], dict] = {}
        self._sequences: set[int] = set()

    def observe(self, event: object) -> dict | None:
        """Return a safe replacement snapshot only for public summary text events."""
        kind = getattr(event, "type", "")
        if kind not in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_summary_part.done",
        }:
            return None
        sequence = getattr(event, "sequence_number", None)
        if type(sequence) is int:
            if sequence in self._sequences:
                return None
            self._sequences.add(sequence)
        item_id = getattr(event, "item_id", None)
        index = getattr(event, "output_index", None)
        position = getattr(event, "summary_index", None)
        if (not isinstance(item_id, str) or not item_id
                or type(index) is not int or index < 0
                or type(position) is not int or position < 0):
            return None
        key = item_id, position
        part = self._parts.get(key, {
            "call_id": self.call_id, "item_id": item_id,
            "output_index": index, "summary_index": position,
            "text": "", "status": "in_progress",
        })
        if kind.endswith(".delta"):
            text = getattr(event, "delta", None)
            if not isinstance(text, str) or part["status"] != "in_progress":
                return None
            part = {**part, "text": part["text"] + text}
        else:
            source = getattr(event, "part", None)
            if source is not None and getattr(source, "type", None) != "summary_text":
                return None
            text = getattr(source, "text", None) if source is not None else getattr(event, "text", None)
            if not isinstance(text, str):
                return None
            status = "incomplete" if getattr(event, "status", None) == "incomplete" else "completed"
            part = {**part, "text": text, "status": status}
        self._parts[key] = part
        return self.event()

    def finish(self, output: object, status: str) -> dict | None:
        """Use the terminal public snapshot as authoritative when one is available."""
        had_parts = bool(self._parts)
        if isinstance(output, (list, tuple)):
            parts = extract_reasoning_summary(
                output, self.call_id,
                status="completed" if status == "completed" else "incomplete",
            )
            self._parts = {(part["item_id"], part["summary_index"]): part for part in parts}
        else:
            self._parts = {key: {**part, "status": "incomplete"}
                           for key, part in self._parts.items()}
        return self.event() if had_parts or self._parts else None

    def event(self) -> dict:
        """Copy only display fields, excluding raw output and encrypted reasoning."""
        return {"type": "reasoning_summary", "call_id": self.call_id,
                "parts": [dict(part) for part in sorted(
                    self._parts.values(), key=lambda part: (part["output_index"], part["summary_index"]),
                ) if part["text"].strip()]}
