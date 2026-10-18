"""Preserve transient Responses output and validate completed function-call arguments.

These objects contain provider content and must not be written to usage records.
"""

import json
import math
from dataclasses import dataclass
from typing import Any


def _snapshot(value: Any) -> Any:
    """Copy SDK models and lightweight fixtures into independent JSON-shaped values."""
    if hasattr(value, "model_fields_set"):
        # Avoid serializer warnings that can echo content from future output variants.
        supplied = {key: getattr(value, key) for key in value.model_fields_set}
        supplied.update(getattr(value, "model_extra", None) or {})
        value = supplied
    elif hasattr(value, "__dict__"):
        value = vars(value)
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot(item) for item in value]
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    """Reject ambiguous JSON objects rather than silently replacing duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate argument key.")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    """Reject argument numbers that cannot be represented as finite JSON numbers."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite argument number.")
    return number


def _reject_constant(value: str) -> None:
    """Reject the nonstandard NaN and Infinity constants accepted by Python's decoder."""
    raise ValueError("Non-JSON argument constant.")


@dataclass(frozen=True)
class ResponseFunctionCall:
    """Hold one completed transport-validated call pending tool and scope validation."""

    output_index: int
    item_id: str | None
    call_id: str
    name: str
    arguments: str
    parsed_arguments: dict[str, Any]


@dataclass(frozen=True)
class ResponseOutcome:
    """Keep ordered output and terminal facts for one provider response."""

    response_id: str | None
    status: str
    output: tuple[dict, ...]
    text: str
    refusal: str
    function_calls: tuple[ResponseFunctionCall, ...]
    usage: dict | None
    protocol_errors: tuple[str, ...]
    error: dict | None
    incomplete_details: dict | None

    @property
    def has_text(self) -> bool:
        """Return whether the final output contains nonblank ordinary text."""
        return bool(self.text.strip())


class ResponseAccumulator:
    """Check streamed calls against the first terminal response snapshot."""

    def __init__(self) -> None:
        """Initialize request-local state without retaining it in conversation storage."""
        self._items: dict[int, dict] = {}
        self._sequences: dict[int, dict] = {}
        self._last_sequence = -1
        self._errors: list[str] = []
        self._text: list[str] = []
        self._refusal: list[str] = []
        self._response_id: str | None = None

    def _error(self, code: str) -> None:
        """Retain stable diagnostics without echoing provider content or arguments."""
        if code not in self._errors:
            self._errors.append(code)

    def observe(self, event: object) -> None:
        """Accumulate supported events while leaving unrelated future events alone."""
        event_type = getattr(event, "type", None)
        if event_type == "response.created":
            response_id = getattr(getattr(event, "response", None), "id", None)
            if self._response_id is not None and self._response_id != response_id:
                self._error("response_identity_mismatch")
            self._response_id = response_id
            return
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            target = (
                self._text if event_type == "response.output_text.delta"
                else self._refusal
            )
            target.append(event.delta)
            return
        if event_type not in {
            "response.output_item.added", "response.output_item.done",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            return
        data = _snapshot(event)
        sequence = data.get("sequence_number")
        if sequence is not None:
            if type(sequence) is not int or sequence < 0:
                self._error("invalid_sequence_number")
                return
            if sequence in self._sequences:
                if self._sequences[sequence] != data:
                    self._error("conflicting_duplicate_event")
                return
            if sequence <= self._last_sequence:
                self._error("out_of_order_event")
            self._sequences[sequence] = data
            self._last_sequence = max(sequence, self._last_sequence)
        index = data.get("output_index")
        if type(index) is not int or index < 0:
            self._error("invalid_output_index")
            return
        if event_type.startswith("response.output_item."):
            item = data.get("item")
            if not isinstance(item, dict):
                self._error("invalid_output_item")
                return
            state = self._items.get(index)
            if state is None:
                state = {"item": item, "deltas": [], "arguments_done": None,
                         "done": None, "has_deltas": False}
                self._items[index] = state
                if (event_type.endswith(".done")
                        and item.get("type") == "function_call"):
                    self._error("missing_output_item_added")
            elif event_type.endswith(".added") and state["item"] != item:
                self._error("conflicting_output_item")
            self._check_identity(state["item"], item)
            if event_type == "response.output_item.done":
                if state["done"] is not None and state["done"] != item:
                    self._error("conflicting_output_item")
                state["done"] = item
            elif state["done"] is not None:
                self._error("output_item_after_done")
            return
        state = self._items.get(index)
        if (state is None or state["item"].get("type") != "function_call"
                or not data.get("item_id")
                or data["item_id"] != state["item"].get("id")):
            self._error("function_call_identity_mismatch")
            return
        if state["done"] is not None:
            self._error("arguments_after_output_done")
        if event_type.endswith(".delta"):
            if state["arguments_done"] is not None:
                self._error("arguments_after_done")
            delta = data.get("delta")
            if not isinstance(delta, str):
                self._error("invalid_argument_delta")
                return
            state["deltas"].append(delta)
            state["has_deltas"] = True
        else:
            arguments = data.get("arguments")
            if not isinstance(arguments, str):
                self._error("invalid_arguments")
                return
            if state["arguments_done"] not in (None, arguments):
                self._error("conflicting_arguments_done")
            state["arguments_done"] = arguments

    def _check_identity(self, initial: dict, final: dict) -> None:
        """Distinguish output item IDs from tool-result call IDs."""
        fields = ("type", "id")
        if initial.get("type") == "function_call":
            fields += ("name", "call_id")
        if any(initial.get(key) != final.get(key) for key in fields):
            self._error("output_item_identity_mismatch")

    def _check_arguments(self, state: dict, final: dict) -> None:
        """Compare all supplied complete arguments with their streamed fragments."""
        arguments = final.get("arguments")
        initial = state["item"].get("arguments", "")
        if not isinstance(initial, str):
            self._error("invalid_arguments")
        elif ((state["has_deltas"] or initial)
              and initial + "".join(state["deltas"]) != arguments):
            self._error("argument_delta_mismatch")
        if (state["arguments_done"] is not None
                and state["arguments_done"] != arguments):
            self._error("arguments_done_mismatch")
        if (state["done"] is not None
                and state["done"].get("arguments") != arguments):
            self._error("output_arguments_mismatch")
        if (state["done"] is not None
                and state["done"].get("status") not in (None, "completed")):
            self._error("function_call_not_completed")

    def finish(self, response: object, terminal_status: str) -> ResponseOutcome:
        """Preserve terminal output and expose coherently completed calls."""
        raw = _snapshot(response) if response is not None else {}
        if self._response_id is not None and raw.get("id") != self._response_id:
            self._error("response_identity_mismatch")
        status = raw.get("status", terminal_status)
        if status != terminal_status:
            self._error("terminal_status_mismatch")
        output = raw.get("output", [])
        if (not isinstance(output, list)
                or any(not isinstance(item, dict) for item in output)):
            self._error("invalid_terminal_output")
            output = []
        calls = []
        item_ids = set()
        call_ids = set()
        text = []
        refusal = []
        for index, item in enumerate(output):
            item_id = item.get("id")
            if item_id is not None:
                if (not isinstance(item_id, str) or not item_id.strip()
                        or item_id in item_ids):
                    self._error("invalid_or_duplicate_item_id")
                else:
                    item_ids.add(item_id)
            state = self._items.get(index)
            if state is not None:
                self._check_identity(state["item"], item)
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text.append(part.get("text", ""))
                    elif part.get("type") == "refusal":
                        refusal.append(part.get("refusal", ""))
            if item.get("type") != "function_call":
                continue
            if (not isinstance(raw.get("id"), str)
                    or not raw["id"].strip()):
                self._error("invalid_response_id")
            if state is not None:
                self._check_arguments(state, item)
            if item.get("status") not in (None, "completed"):
                self._error("function_call_not_completed")
            call_id = item.get("call_id")
            name = item.get("name")
            if (not isinstance(call_id, str) or not call_id.strip()
                    or call_id in call_ids):
                self._error("invalid_or_duplicate_call_id")
                continue
            call_ids.add(call_id)
            if not isinstance(name, str) or not name.strip():
                self._error("invalid_function_name")
                continue
            arguments = item.get("arguments")
            try:
                if not isinstance(arguments, str):
                    raise ValueError("Missing arguments.")
                parsed = json.loads(
                    arguments, object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant, parse_float=_finite_float,
                )
                if not isinstance(parsed, dict):
                    raise ValueError("Arguments must be an object.")
            except (ValueError, RecursionError):
                self._error("invalid_arguments")
                continue
            calls.append(ResponseFunctionCall(
                index, item_id, call_id, name, arguments, parsed,
            ))
        if any(index >= len(output) for index in self._items):
            self._error("missing_terminal_output_item")
        return ResponseOutcome(
            response_id=raw.get("id"), status=status, output=tuple(output),
            text="".join(text if "output" in raw else self._text),
            refusal="".join(refusal if "output" in raw else self._refusal),
            function_calls=(
                tuple(calls) if status == "completed" and not self._errors else ()
            ),
            usage=raw.get("usage"), protocol_errors=tuple(self._errors),
            error=raw.get("error"), incomplete_details=raw.get("incomplete_details"),
        )
