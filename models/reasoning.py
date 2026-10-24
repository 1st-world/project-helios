"""Validate display-only reasoning summaries without retaining opaque provider state."""

SUMMARY_SCHEMA_VERSION = "0.1"
SUMMARY_STATUSES = {"in_progress", "completed", "incomplete"}
SUMMARY_FIELDS = {
    "call_id", "item_id", "output_index", "summary_index", "text", "status",
}


def validate_reasoning_summary(parts: object) -> list[dict]:
    """Copy validated public summary parts for storage and browser presentation."""
    if not isinstance(parts, list):
        raise ValueError("Invalid reasoning summary.")
    result, identities = [], set()
    for part in parts:
        if not isinstance(part, dict) or set(part) != SUMMARY_FIELDS:
            raise ValueError("Invalid reasoning summary part.")
        if any(not isinstance(part[name], str) or not part[name]
               for name in ("call_id", "item_id")):
            raise ValueError("Invalid reasoning summary identity.")
        if any(type(part[name]) is not int or part[name] < 0
               for name in ("output_index", "summary_index")):
            raise ValueError("Invalid reasoning summary position.")
        if (not isinstance(part["text"], str) or not isinstance(part["status"], str)
                or part["status"] not in SUMMARY_STATUSES):
            raise ValueError("Invalid reasoning summary content.")
        identity = (part["call_id"], part["item_id"], part["summary_index"])
        if identity in identities:
            raise ValueError("Duplicate reasoning summary part.")
        identities.add(identity)
        result.append(dict(part))
    return result


def extract_reasoning_summary(output: object, call_id: str, *,
                              status: str = "completed") -> list[dict]:
    """Select only public summary_text fields from a provider output snapshot."""
    parts = []
    for index, item in enumerate(output if isinstance(output, (list, tuple)) else ()):
        if (not isinstance(item, dict) or item.get("type") != "reasoning"
                or not isinstance(item.get("id"), str) or not item["id"]):
            continue
        summary = item.get("summary")
        if not isinstance(summary, list):
            continue
        for position, part in enumerate(summary):
            if (not isinstance(part, dict) or part.get("type") != "summary_text"
                    or not isinstance(part.get("text"), str) or not part["text"].strip()):
                continue
            parts.append({"call_id": call_id, "item_id": item["id"],
                          "output_index": index, "summary_index": position,
                          "text": part["text"], "status": status})
    return validate_reasoning_summary(parts)
