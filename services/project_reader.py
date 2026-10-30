"""Page project listings, literal searches, and UTF-8 line fragments within one root."""

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from models.workspace import WorkspaceReadOptions
from services.local_work import check_local_cancelled
from services.project_access import ProjectAccess, ProjectReadError, ProjectText, signature


@dataclass(frozen=True)
class ProjectReadLimits:
    """Bound local resources independently of any model context or output allowance."""

    result_bytes: int = 64 * 1024
    fragment_bytes: int = 2048
    work_bytes: int = 1024 * 1024
    work_items: int = 2048
    max_entries: int = 256
    max_matches: int = 128
    max_fragments: int = 128
    max_depth: int = 32
    max_cursors: int = 16
    cursor_seconds: float = 300
    call_seconds: float = 5
    ipc_bytes: int = 256 * 1024


@dataclass
class _Cursor:
    """Own a bounded iterator and the descriptors needed by its next page."""

    key: tuple
    iterator: Iterator | None = None
    checks: list[Callable[[], None]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    pending: tuple | None = None
    touched: float = field(default_factory=time.monotonic)

    def close(self) -> None:
        """Close the iterator so its nested file and directory contexts unwind."""
        if self.iterator is not None:
            self.iterator.close()


class ProjectReader:
    """Keep ephemeral continuations without precomputing or caching whole project trees."""

    def __init__(
        self, root: Path, protected: list[Path], *,
        limits: ProjectReadLimits | None = None,
        options: WorkspaceReadOptions | None = None,
    ) -> None:
        """Pin access policy and initialize an empty cursor registry."""
        self.access = ProjectAccess(root, protected, options=options)
        self.limits = limits or ProjectReadLimits()
        self._cursors: dict[str, _Cursor] = {}

    def close(self) -> None:
        """Release all open continuation resources at session shutdown."""
        for cursor in self._cursors.values():
            cursor.close()
        self._cursors.clear()

    def _folder(self, path: Path, state: _Cursor) -> Iterator:
        """Visit one directory lazily and invalidate its iterator after replacement."""
        name = path.relative_to(self.access.root).as_posix()
        version = signature(path.lstat())
        policy = self.access.policy_stamp(path)

        def check() -> None:
            """Validate a live directory's identity and local ignore revision."""
            self.access.resolve(name)
            if signature(path.lstat()) != version or self.access.policy_stamp(path) != policy:
                raise ProjectReadError("directory_changed")

        check()
        state.checks.append(check)
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    check_local_cancelled()
                    child = path / entry.name
                    relative = child.relative_to(self.access.root).as_posix()
                    try:
                        checked = self.access.resolve(relative)
                        if checked.is_dir():
                            kind = "directory"
                        elif checked.is_file():
                            kind = "file"
                        else:
                            yield "omission", "special_file"
                            continue
                        yield "entry", (relative, checked, kind)
                    except ProjectReadError as exc:
                        if exc.code in {"workspace_changed", "ignore_policy_unavailable"}:
                            raise
                        yield "omission", exc.code
                    except OSError:
                        yield "omission", "inaccessible_entry"
            check()
        finally:
            state.checks.remove(check)

    def _list(self, path: Path, state: _Cursor) -> Iterator:
        """Yield direct children in filesystem order without sorting the entire folder."""
        if not path.is_dir():
            raise ProjectReadError("directory_required")
        for kind, value in self._folder(path, state):
            if kind == "entry":
                name, _, entry_type = value
                yield "item", {"path": name, "type": entry_type}
            else:
                yield kind, value

    def _files(self, path: Path, state: _Cursor, depth: int = 0) -> Iterator:
        """Traverse directories incrementally while reporting unvisited depth ranges."""
        if path.is_file():
            yield "file", path
            return
        for kind, value in self._folder(path, state):
            if kind != "entry":
                yield kind, value
                continue
            _, child, entry_type = value
            yield "progress", 0
            if entry_type == "file":
                yield "file", child
            elif depth >= self.limits.max_depth:
                yield "omission", "depth_limit"
            else:
                yield from self._files(child, state, depth + 1)

    def _read(self, path: Path, state: _Cursor, start: int, count: int) -> Iterator:
        """Read the requested line range in bounded fragments, including seek progress."""
        source = ProjectText(self.access, path, self.limits.fragment_bytes)
        state.metadata["file_version"] = source.version
        state.metadata["requested_range"] = {"start_line": start, "line_count": count}
        state.metadata["reached_eof"] = False
        state.checks.append(source.check)
        try:
            while source.line < start + count:
                source.check()
                before = source.source.tell()
                part = source.fragment()
                yield "progress", source.source.tell() - before
                if part is None:
                    state.metadata.update(reached_eof=True, next_position=None)
                    break
                source.check()
                if part["line"] >= start:
                    yield "item", part
        finally:
            state.checks.remove(source.check)
            source.close()

    def _search(self, path: Path, state: _Cursor, query: str) -> Iterator:
        """Find literal matches across fragment boundaries without buffering whole lines."""
        for kind, value in self._files(path, state):
            if kind != "file":
                yield kind, value
                continue
            source = None
            try:
                source = ProjectText(self.access, value, self.limits.fragment_bytes)
                state.checks.append(source.check)
                tail = ""
                while True:
                    source.check()
                    before = source.source.tell()
                    part = source.fragment()
                    yield "progress", source.source.tell() - before
                    if part is None:
                        break
                    source.check()
                    text = tail + part["text"]
                    base_column = part["column"] - len(tail)
                    offset = 0
                    while (position := text.find(query, offset)) >= 0:
                        offset = position + 1
                        # Only report matches completed by the newly read fragment.
                        if position + len(query) <= len(tail):
                            continue
                        left, right = max(0, position - 80), position + len(query) + 80
                        yield "item", {
                            "path": value.relative_to(self.access.root).as_posix(),
                            "line": part["line"], "column": base_column + position,
                            "text": text[left:right].rstrip("\r\n"),
                            "snippet_column": base_column + left,
                            "snippet_is_partial": (
                                base_column + left > 0 or right < len(text)
                                or not text.endswith("\n")
                            ),
                            "file_version": source.version,
                        }
                    tail = (
                        "" if text.endswith("\n") or len(query) == 1
                        else text[-(len(query) - 1):]
                    )
            except ProjectReadError as exc:
                if exc.code in {"workspace_changed", "ignore_policy_unavailable", "file_changed"}:
                    raise
                yield "omission", exc.code
            except OSError:
                yield "omission", "inaccessible_file"
            finally:
                if source is not None:
                    state.checks.remove(source.check)
                    source.close()

    def _page(self, state: _Cursor, maximum: int) -> dict:
        """Advance a cursor within byte, item, and processing-time resource limits."""
        for check in state.checks:
            check()
        items = []
        omissions = Counter()
        worked = 0
        size = 2048
        deadline = time.monotonic() + self.limits.call_seconds
        complete = False
        for _ in range(self.limits.work_items):
            check_local_cancelled()
            if time.monotonic() >= deadline or worked >= self.limits.work_bytes:
                break
            try:
                event = state.pending or next(state.iterator)
                state.pending = None
            except StopIteration:
                complete = True
                break
            kind, value = event
            if kind == "item":
                addition = len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + 1
                if len(items) >= maximum or size + addition > self.limits.result_bytes:
                    state.pending = event
                    break
                items.append(value)
                size += addition
            elif kind == "progress":
                worked += value
            else:
                omissions[value] += 1
        for check in state.checks:
            check()
        totals = Counter(state.metadata.get("omission_totals", {}))
        totals.update(omissions)
        state.metadata["omission_totals"] = dict(totals)
        if state.key[0] == "read_file":
            state.metadata["next_position"] = None
            if items and not state.metadata["reached_eof"]:
                last = items[-1]
                ended = last["text"].endswith("\n")
                state.metadata["next_position"] = {
                    "line": last["line"] + int(ended),
                    "column": 0 if ended else last["column"] + len(last["text"]),
                    "byte_offset": last["byte_end"],
                }
        next_cursor = None
        if complete:
            state.close()
        else:
            next_cursor = uuid4().hex
            state.touched = time.monotonic()
            self._cursors[next_cursor] = state
        return {
            "contract_version": "0.1", "scope": state.key[1],
            "items": items, "next_cursor": next_cursor,
            "complete": complete, "omissions": dict(omissions),
            "bytes_processed": worked, **state.metadata,
        }

    def call(self, name: str, arguments: dict) -> dict:
        """Validate one fixed tool call and release its resources on every failure."""
        self.access.check_root()
        now = time.monotonic()
        for token, state in list(self._cursors.items()):
            if now - state.touched >= self.limits.cursor_seconds:
                self._cursors.pop(token).close()
        if not isinstance(arguments, dict):
            raise ProjectReadError("invalid_arguments")
        allowed = {
            "list_files": {"path", "page_size", "cursor"},
            "search_text": {"path", "query", "page_size", "cursor"},
            "read_file": {"path", "start_line", "line_count", "cursor"},
        }
        if name not in allowed:
            raise ProjectReadError("unknown_tool")
        if arguments.keys() - allowed[name]:
            raise ProjectReadError("invalid_arguments")
        path = self.access.normalize(arguments.get("path", "."))
        maximum = arguments.get("page_size", 100 if name == "list_files" else 20)
        cap = self.limits.max_entries if name == "list_files" else self.limits.max_matches
        if type(maximum) is not int or not 1 <= maximum <= cap:
            raise ProjectReadError("invalid_page_size")
        query = arguments.get("query", "")
        if name == "search_text" and (
            not isinstance(query, str) or not query or "\n" in query or "\r" in query
            or "\x00" in query or len(query.encode("utf-8")) > 4096
        ):
            raise ProjectReadError("invalid_query")
        start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
        if name == "read_file" and (
            type(start) is not int or start < 1 or type(count) is not int or not 1 <= count <= 1000
        ):
            raise ProjectReadError("invalid_line_range")
        key = (name, path, query, start, count)
        token = arguments.get("cursor")
        if token is not None and (not isinstance(token, str) or len(token) != 32):
            raise ProjectReadError("invalid_cursor")
        state = self._cursors.get(token) if token else None
        if token and (state is None or state.key != key):
            raise ProjectReadError("invalid_cursor")
        if state is not None:
            self._cursors.pop(token)
        else:
            if len(self._cursors) >= self.limits.max_cursors:
                raise ProjectReadError("cursor_capacity")
            state = _Cursor(key)
        try:
            checked = self.access.resolve(path)
            if state.iterator is None:
                if name == "list_files":
                    state.iterator = self._list(checked, state)
                elif name == "search_text":
                    state.iterator = self._search(checked, state, query)
                else:
                    state.iterator = self._read(checked, state, start, count)
            result = self._page(
                state, self.limits.max_fragments if name == "read_file" else maximum,
            )
            result["order"] = "filesystem" if name == "list_files" else "traversal"
            result["policy"] = self.access.describe_policy()
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.limits.result_bytes:
                raise ProjectReadError("result_limit")
            return result
        except BaseException:
            state.close()
            for cursor, active in list(self._cursors.items()):
                if active is state:
                    del self._cursors[cursor]
            raise
