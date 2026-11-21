"""Publish recognizable cache staging files under a shared directory lock."""

import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from services.project_access import directory_identity, is_link

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class CacheWriteBusy(OSError):
    """Report that another process is publishing or removing cache files."""


def _stage_prefix(target_name: str) -> str:
    """Reserve a distinct namespace for staged writes to the named cache file."""
    return f".helios-cache-{target_name}-"


def is_cache_stage(name: str, targets: tuple[str, ...]) -> bool:
    """Match only stages of the fixed targets known to the caller."""
    return any(
        re.fullmatch(rf"{re.escape(_stage_prefix(target))}[0-9a-f]{{32}}\.tmp", name)
        for target in targets
    )


def _ordinary_lock(info: os.stat_result) -> None:
    """Reject redirected, linked, and non-regular cache locks."""
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("The cache write lock is unsafe.")


@contextmanager
def cache_write_lock(directory: Path, *, create: bool = True):
    """Take the nonblocking lock shared by writers and removers of one cache directory."""
    identity = directory_identity(directory)
    path = directory.parent / f".helios-cache-{directory.name}.lock"
    try:
        _ordinary_lock(path.lstat())
    except FileNotFoundError:
        if not create:
            yield False
            return
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if create:
        flags |= os.O_CREAT
    with os.fdopen(os.open(path, flags, 0o600), "r+b", buffering=0) as handle:
        opened = os.fstat(handle.fileno())
        current = path.lstat()
        _ordinary_lock(opened)
        _ordinary_lock(current)
        if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or directory_identity(directory) != identity):
            raise OSError("The cache directory or write lock changed.")
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise CacheWriteBusy("Another cache operation is in progress.") from error
        yield True


@contextmanager
def staged_cache_write(path: Path, *, encoding: str | None = None):
    """Flush and atomically publish one cache write, with an identifiable stage on interruption."""
    with cache_write_lock(path.parent):
        temporary = path.with_name(f"{_stage_prefix(path.name)}{uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            mode = "wb" if encoding is None else "w"
            with os.fdopen(descriptor, mode, encoding=encoding) as target:
                yield target
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
