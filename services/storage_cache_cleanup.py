"""Review app-managed cache files and remove eligible entries by revision."""

import hashlib
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from config import TOKENIZER_CACHE_PATH, Settings
from services.cache_staging import CacheWriteBusy, cache_write_lock, is_cache_stage
from services.local_work import check_local_cancelled
from services.project_access import directory_identity, is_link, signature
from services.storage_maintenance import (
    RECOVERY_APPLY, RECOVERY_PREVIEW, REPORT_CACHE_NAMES, CheckScope, load_check_result, result_path,
)
from services.token_estimator import vocabulary_status


CacheStagingScope = Literal["tokenizer", "maintenance"]


class CacheEntryUnavailable(OSError):
    """Report a cache entry that cannot be inspected or removed safely."""


@dataclass(frozen=True)
class _CacheFile:
    """Describe the size and revision of a stable cache file."""

    size_bytes: int
    revision: str


def _managed_path(settings: Settings) -> Path:
    """Allow cleanup only in the app-managed storage layout."""
    expected = settings.profiles_path.parent.absolute() / TOKENIZER_CACHE_PATH
    if settings.tokenizer_path.absolute().parts != expected.parts:
        raise CacheEntryUnavailable("The vocabulary is outside the managed cache.")
    return expected


def _inspect(path: Path) -> _CacheFile | None:
    """Read one stable file signature without opening its contents."""
    try:
        directory = directory_identity(path.parent)
        info = path.lstat()
        after = directory_identity(path.parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CacheEntryUnavailable("The cache file cannot be inspected safely.") from error
    if directory != after or is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CacheEntryUnavailable("The cache entry is not a stable, ordinary file.")
    identity = repr((directory, signature(info))).encode("ascii")
    return _CacheFile(info.st_size, hashlib.sha256(identity).hexdigest())


def review_tokenizer_cache(settings: Settings, *, busy: bool = False) -> dict:
    """Return the size and revision used by both vocabulary review endpoints."""
    if busy:
        return {"state": "busy"}
    try:
        entry = _inspect(_managed_path(settings))
    except CacheEntryUnavailable:
        return {"state": "unavailable"}
    return {"state": "empty"} if entry is None else {"state": "available", **asdict(entry)}


def _review_check_cache(root: Path, scope: CheckScope, *, busy: bool) -> dict:
    """Expose a saved check's ID only when its file is safe and stable."""
    if busy:
        return {"state": "busy"}
    path = result_path(root, scope=scope)
    try:
        entry = _inspect(path)
        if entry is None:
            return {"state": "empty"}
        result = load_check_result(path, scope=scope, strict_io=True)
        if _inspect(path) != entry:
            return {"state": "unavailable"}
    except OSError:
        return {"state": "unavailable"}
    if result is None:
        return {"state": "damaged", **asdict(entry)}
    return {
        "state": "available", "size_bytes": entry.size_bytes, "result_id": result["id"],
        "finished_at": result["finished_at"],
    }


def _review_recovery_cache(path: Path) -> dict:
    """Report protected recovery state from file metadata without opening the file."""
    try:
        entry = _inspect(path)
    except CacheEntryUnavailable:
        return {"state": "unavailable"}
    return {"state": "empty"} if entry is None else {"state": "protected", "size_bytes": entry.size_bytes}


def review_storage_cache(settings: Settings, *, tokenizer_busy: bool, checks_busy: bool) -> dict:
    """List user-removable cache entries alongside protected recovery state."""
    root = settings.profiles_path.parent.absolute()
    items = [{"id": "tokenizer", **review_tokenizer_cache(settings, busy=tokenizer_busy)}]
    for scope in ("basic", "deep"):
        check_local_cancelled()
        items.append({"id": scope, **_review_check_cache(root, scope, busy=checks_busy)})
    for identity, operation in (("recovery-preview", RECOVERY_PREVIEW), ("recovery-copy", RECOVERY_APPLY)):
        check_local_cancelled()
        items.append({"id": identity, **_review_recovery_cache(result_path(root, scope=operation))})
    for scope, busy in (("tokenizer", tokenizer_busy), ("maintenance", checks_busy)):
        check_local_cancelled()
        items.append({"id": f"{scope}-staging", **_review_staging(settings, scope, busy=busy)})
    return {"items": items}


def _staging_location(settings: Settings, scope: CacheStagingScope) -> tuple[Path, tuple[str, ...]]:
    """Select the app-owned directory and recognized publications for one staging group."""
    if scope == "tokenizer":
        path = _managed_path(settings)
        return path.parent, (path.name,)
    return result_path(settings.profiles_path.parent.absolute()).parent, tuple(REPORT_CACHE_NAMES.values())


def _stages(directory: Path, targets: tuple[str, ...]) -> list[tuple[Path, _CacheFile]]:
    """Read a stable list of regular, recognized staging files under the write lock."""
    identity = directory_identity(directory)
    entries = []
    for path in sorted(directory.iterdir()):
        check_local_cancelled()
        if is_cache_stage(path.name, targets) and (entry := _inspect(path)) is not None:
            entries.append((path, entry))
    if directory_identity(directory) != identity:
        raise CacheEntryUnavailable("The staging directory changed.")
    return entries


def _staging_revision(entries: list[tuple[Path, _CacheFile]]) -> str:
    """Bind confirmation to the exact set of reviewed temporary files."""
    identity = repr([(path.name, entry.revision) for path, entry in entries]).encode("ascii")
    return hashlib.sha256(identity).hexdigest()


def _review_staging(settings: Settings, scope: CacheStagingScope, *, busy: bool) -> dict:
    """Describe abandoned stages only when no cooperating publisher owns the lock."""
    if busy:
        return {"state": "busy"}
    try:
        directory, targets = _staging_location(settings, scope)
        with cache_write_lock(directory, create=False) as locked:
            entries = _stages(directory, targets)
            if entries and not locked:
                return {"state": "unavailable"}
    except FileNotFoundError:
        return {"state": "empty"}
    except CacheWriteBusy:
        return {"state": "busy"}
    except OSError:
        return {"state": "unavailable"}
    if not entries:
        return {"state": "empty"}
    return {"state": "available", "files": len(entries),
            "size_bytes": sum(entry.size_bytes for _, entry in entries), "revision": _staging_revision(entries)}


def remove_cache_staging(settings: Settings, scope: CacheStagingScope, revision: str) -> bool:
    """Clear the reviewed stages without racing a writer or accepting a changed set."""
    directory, targets = _staging_location(settings, scope)
    try:
        with cache_write_lock(directory, create=False) as locked:
            entries = _stages(directory, targets)
            if not entries:
                return True
            if not locked:
                raise CacheEntryUnavailable("The staging lock is missing.")
            if _staging_revision(entries) != revision:
                return False
            for path, entry in entries:
                if _inspect(path) != entry:
                    return False
                check_local_cancelled()
                path.unlink(missing_ok=True)
    except FileNotFoundError:
        return True
    return True


def remove_damaged_check_cache(root: Path, scope: CheckScope, revision: str) -> bool:
    """Remove a reviewed damaged report, preserving valid reports and replacements."""
    path = result_path(root.absolute(), scope=scope)
    if _inspect(path) is None:
        return True
    try:
        with cache_write_lock(path.parent):
            entry = _inspect(path)
            if entry is None:
                return True
            if entry.revision != revision or load_check_result(path, scope=scope, strict_io=True) is not None:
                return False
            if _inspect(path) != entry:
                return False
            check_local_cancelled()
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        return True
    return True


def remove_tokenizer_cache(settings: Settings, revision: str) -> bool:
    """Remove a reviewed vocabulary, rejecting a replacement and accepting an absent file."""
    path = _managed_path(settings)
    if _inspect(path) is None:
        return True
    with cache_write_lock(path.parent):
        entry = _inspect(path)
        if entry is None:
            return True
        if entry.revision != revision:
            return False
        # Warm the old encoder for requests that bound this revision before their first estimate.
        vocabulary_status(path)
        entry = _inspect(path)
        if entry is None:
            return True
        if entry.revision != revision:
            return False
        check_local_cancelled()
        path.unlink(missing_ok=True)
    return True
