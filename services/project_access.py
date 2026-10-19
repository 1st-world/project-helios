"""Enforce automatic project-reading boundaries and Git ignore rules.

This is application-level path validation, not an operating-system sandbox.
"""

import codecs
import hashlib
import json
import os
import stat
from pathlib import Path, PureWindowsPath

from pathspec import GitIgnoreSpec

from services.local_work import check_local_cancelled


class ProjectReadError(ValueError):
    """Report a stable tool error without exposing host paths or file contents."""

    def __init__(self, code: str) -> None:
        """Keep the diagnostic safe for MCP tool results and application logs."""
        self.code = code
        super().__init__(code)


def signature(info: os.stat_result) -> tuple:
    """Identify a file revision without reading its entire contents."""
    # Windows fstat/stat disagree on deprecated ctime; birthtime has stable semantics.
    created = info.st_birthtime_ns if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, created)


def is_link(info: os.stat_result) -> bool:
    """Recognize symbolic links and all Windows reparse points, including junctions."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def final_handle_path(fd: int, fallback: Path) -> Path:
    """Resolve an opened Windows handle before reading bytes through it."""
    if os.name != "nt":
        return fallback.resolve(strict=True)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ProjectReadError("file_identity_unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


class ProjectAccess:
    """Pin one existing root and deny hidden, ignored, linked, and runtime paths."""

    POLICY_BYTES = 64 * 1024

    def __init__(self, root: Path, protected: list[Path]) -> None:
        """Capture trusted host paths without creating or traversing a workspace."""
        if not root.is_absolute():
            raise ProjectReadError("workspace_required")
        try:
            self.root = root.resolve(strict=True)
            info = self.root.lstat()
        except OSError:
            raise ProjectReadError("workspace_required") from None
        if not stat.S_ISDIR(info.st_mode) or is_link(info):
            raise ProjectReadError("workspace_required")
        self.identity = (info.st_dev, info.st_ino)
        self.protected = tuple(path.resolve() for path in protected)
        self._policies: dict[Path, tuple[tuple | None, GitIgnoreSpec]] = {}
        self._empty_policy = GitIgnoreSpec.from_lines([])

    def check_root(self) -> None:
        """Reject a disconnected, replaced, or linked root before every operation."""
        check_local_cancelled()
        try:
            info = self.root.lstat()
            if is_link(info) or (info.st_dev, info.st_ino) != self.identity:
                raise ProjectReadError("workspace_changed")
        except OSError:
            raise ProjectReadError("workspace_changed") from None

    def normalize(self, value: str) -> str:
        """Accept portable relative paths without Windows aliases or traversal."""
        if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or "\\" in value or ":" in value or value.startswith("/")
                or any(char in value for char in '<>"|?*')
                or PureWindowsPath(value).drive):
            raise ProjectReadError("invalid_project_path")
        parts = value.split("/")
        if any(part == ".." or part.endswith((" ", ".")) and part != "." for part in parts):
            raise ProjectReadError("invalid_project_path")
        reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        ), *(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "¹²³")}
        if any(part.split(".")[0].rstrip(" .").upper() in reserved for part in parts):
            raise ProjectReadError("invalid_project_path")
        return "/".join(part for part in parts if part not in {"", "."}) or "."

    def _runtime(self, path: Path) -> bool:
        """Protect exact runtime locations, their descendants, and file backups."""
        for protected in self.protected:
            if path == protected or protected in path.parents:
                return True
            if path.parent == protected.parent and path.name.startswith(protected.name + "."):
                return True
        return False

    def _policy(self, folder: Path) -> tuple[tuple | None, GitIgnoreSpec]:
        """Load bounded UTF-8 ignore rules, failing closed on unsafe policy files."""
        path = folder / ".gitignore"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None, self._empty_policy
        if is_link(info) or not stat.S_ISREG(info.st_mode):
            raise ProjectReadError("ignore_policy_unavailable")
        version = signature(info)
        cached = self._policies.get(folder)
        if cached is not None and cached[0] == version:
            return cached
        try:
            with self.open_file(path, policy=True) as source:
                data = source.read(self.POLICY_BYTES + 1)
                if len(data) > self.POLICY_BYTES or signature(os.fstat(source.fileno())) != version:
                    raise ProjectReadError("ignore_policy_unavailable")
                rules = GitIgnoreSpec.from_lines(data.decode("utf-8-sig").splitlines())
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, ProjectReadError):
                raise
            raise ProjectReadError("ignore_policy_unavailable") from None
        result = version, rules
        # Cache size is independent of the number of files traversed.
        if len(self._policies) >= 64:
            self._policies.pop(next(iter(self._policies)))
        self._policies[folder] = result
        return result

    def resolve(self, value: str, *, policy: bool = False) -> Path:
        """Validate each path component and every applicable ignore policy."""
        self.check_root()
        name = self.normalize(value)
        current = self.root
        rules = []
        parts = [] if name == "." else name.split("/")
        for index, part in enumerate(parts):
            if not policy:
                _, spec = self._policy(current)
                rules.append((current, spec))
            current = current / part
            if self._runtime(current):
                raise ProjectReadError("protected_path")
            if part.startswith(".") and not (policy and index == len(parts) - 1):
                raise ProjectReadError("hidden_path")
            info = current.lstat()
            if is_link(info) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                raise ProjectReadError("linked_path")
            if (getattr(info, "st_file_attributes", 0) & 0x6
                    and not (policy and index == len(parts) - 1)):
                raise ProjectReadError("hidden_path")
            if not policy:
                ignored = False
                for base, spec in rules:
                    candidate = current.relative_to(base).as_posix()
                    if stat.S_ISDIR(info.st_mode):
                        candidate += "/"
                    match = spec.check_file(candidate)
                    if match.include is not None:
                        ignored = match.include
                if ignored:
                    raise ProjectReadError("ignored_path")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ProjectReadError("outside_workspace")
        if resolved != current:
            # Short Windows names must not bypass long-name ignore or hidden rules.
            raise ProjectReadError("aliased_path")
        return current

    def policy_stamp(self, path: Path) -> tuple:
        """Bind continuations to the ignore files covering their current scope."""
        self.check_root()
        folders = [self.root]
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_dir():
                folders.append(current)
        return tuple(self._policy(folder)[0] for folder in folders)

    def open_file(self, path: Path, *, policy: bool = False):
        """Verify the opened identity and final location before allowing file reads."""
        name = path.relative_to(self.root).as_posix()
        checked = self.resolve(name, policy=policy)
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(checked, flags)
        try:
            info = os.fstat(descriptor)
            final = final_handle_path(descriptor, checked)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or is_link(info) or not final.is_relative_to(self.root)
                    or signature(info) != signature(checked.lstat())):
                raise ProjectReadError("file_changed")
            self.resolve(final.relative_to(self.root).as_posix(), policy=policy)
            self.resolve(name, policy=policy)
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise


class ProjectText:
    """Read bounded UTF-8 line fragments while retaining exact continuation positions."""

    def __init__(self, access: ProjectAccess, path: Path, fragment_bytes: int) -> None:
        """Open one validated file and capture its metadata revision and policy."""
        self.access = access
        self.path = path
        self.source = access.open_file(path)
        try:
            self.revision = signature(os.fstat(self.source.fileno()))
            self.policy = access.policy_stamp(path.parent)
        except BaseException:
            self.source.close()
            raise
        self.version = hashlib.sha256(json.dumps(self.revision).encode()).hexdigest()
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.line = 1
        self.column = 0
        self.fragment_bytes = fragment_bytes

    def check(self) -> None:
        """Detect path replacements, metadata changes, and altered ignore rules."""
        self.access.resolve(self.path.relative_to(self.access.root).as_posix())
        if (signature(self.path.lstat()) != self.revision
                or signature(os.fstat(self.source.fileno())) != self.revision
                or self.access.policy_stamp(self.path.parent) != self.policy
                or not final_handle_path(self.source.fileno(), self.path).is_relative_to(self.access.root)):
            raise ProjectReadError("file_changed")

    def fragment(self) -> dict | None:
        """Return a bounded fragment without allocating a complete long line."""
        check_local_cancelled()
        pending = len(self.decoder.getstate()[0])
        start = self.source.tell() - pending
        data = self.source.readline(self.fragment_bytes)
        try:
            text = self.decoder.decode(data, final=not data)
        except UnicodeError:
            raise ProjectReadError("unsupported_text_encoding") from None
        if b"\x00" in data:
            raise ProjectReadError("binary_file")
        if not data and not text:
            return None
        end = self.source.tell() - len(self.decoder.getstate()[0])
        result = {
            "line": self.line, "column": self.column,
            "byte_start": start, "byte_end": end, "text": text,
        }
        if text.endswith("\n"):
            self.line += 1
            self.column = 0
        else:
            self.column += len(text)
        return result

    def close(self) -> None:
        """Release the file descriptor even after a partial or failed read."""
        self.source.close()
