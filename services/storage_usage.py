"""Measure the logical size of known Helios files without reading or modifying them."""

import os
import re
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import Settings
from services.local_work import check_local_cancelled
from services.project_access import directory_identity, is_link
from services.storage_diagnostics import is_upload_id
from services.storage_maintenance import RECOVERY_APPLY, RECOVERY_PREVIEW, ownership_path, recovery_record_dir, result_path
from services.storage_references import CALL_USAGE, MANIFEST, SUMMARY_USAGE


STORAGE_CATEGORIES = ("conversations", "attachments", "app_data", "usage_records", "logs", "cache", "backups")
SQLITE_SIDECARS = ("-journal", "-wal", "-shm")


@dataclass
class Usage:
    """Count measured bytes, files, and entries or locations that could not be counted."""

    bytes: int = 0
    files: int = 0
    skipped: int = 0


def _measure(directory: Path, names: tuple[str, ...] | Callable[[str], bool], usage: Usage) -> None:
    """Count one directory's recognized entries and discard sizes if the directory moves."""
    check_local_cancelled()
    try:
        before = directory_identity(directory)
    except FileNotFoundError:
        return  # Optional stores may not have been initialized.
    except OSError:
        usage.skipped += 1
        return
    measured = Usage()

    def add(info: os.stat_result) -> None:
        """Count only regular files with a single directory entry."""
        if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            measured.skipped += 1
            return
        measured.bytes += info.st_size
        measured.files += 1

    try:
        if callable(names):
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_local_cancelled()
                    if names(entry.name):
                        try:
                            # DirEntry.stat may omit the link count on Windows.
                            add((directory / entry.name).lstat())
                        except OSError:
                            measured.skipped += 1
        else:
            for name in names:
                check_local_cancelled()
                try:
                    add((directory / name).lstat())
                except FileNotFoundError:
                    continue
                except OSError:
                    measured.skipped += 1
        if directory_identity(directory) != before:
            raise OSError("Storage directory changed during measurement.")
    except OSError:
        usage.skipped += 1
        return
    usage.bytes += measured.bytes
    usage.files += measured.files
    usage.skipped += measured.skipped


def _sqlite_names(*names: str) -> tuple[str, ...]:
    """Include SQLite's live sidecars in the same category as the database."""
    return tuple(name + suffix for name in names for suffix in ("", *SQLITE_SIDECARS))


def _attachment_payload(name: str) -> bool:
    """Recognize published attachment copies and uploads still being staged."""
    stem, suffix = os.path.splitext(name)
    return suffix in {".blob", ".part"} and is_upload_id(stem)


def _conversation_file(name: str) -> bool:
    """Match transcripts, including damaged JSON, and recognized in-flight writes."""
    stem, suffix = os.path.splitext(name)
    return suffix == ".json" or (suffix == ".tmp" and is_upload_id(stem))


def _recovery_record(name: str) -> bool:
    """Match the durable JSON record named for a completed recovery job."""
    stem, suffix = os.path.splitext(name)
    return suffix == ".json" and is_upload_id(stem)


def _app_log(name: str) -> bool:
    """Match the app log and its rotated copies."""
    return name == "helios.log" or re.fullmatch(r"helios\.log\.[0-9]+", name) is not None


def measure_storage_usage(settings: Settings) -> dict:
    """Measure app-owned files by role and report aggregate coverage."""
    root = settings.profiles_path.parent.absolute()
    locations = {category: Usage() for category in STORAGE_CATEGORIES}
    _measure(settings.conversations_root.absolute(), _conversation_file, locations["conversations"])
    _measure(settings.attachments_root.absolute(), _attachment_payload, locations["attachments"])
    _measure(settings.attachments_root.absolute(), _sqlite_names(Path(MANIFEST).name), locations["app_data"])
    _measure(settings.work_runs_path.parent.absolute(), _sqlite_names(settings.work_runs_path.name), locations["app_data"])
    registries = (settings.app_settings_path.name, settings.profiles_path.name)
    _measure(root, (*registries, *(name + ".lock" for name in registries)), locations["app_data"])
    owner_lock = ownership_path(root)
    _measure(owner_lock.parent, (owner_lock.name,), locations["app_data"])
    logs = settings.logs_root.absolute()
    _measure(logs, _sqlite_names(Path(CALL_USAGE).name, Path(SUMMARY_USAGE).name), locations["usage_records"])
    _measure(logs, _app_log, locations["logs"])
    _measure(recovery_record_dir(root), _recovery_record, locations["logs"])
    tokenizer = settings.tokenizer_path.absolute()
    _measure(tokenizer.parent, (tokenizer.name,), locations["cache"])
    result_names = tuple(result_path(root, scope=scope).name for scope in ("basic", "deep", RECOVERY_PREVIEW, RECOVERY_APPLY))
    _measure(result_path(root).parent, result_names, locations["cache"])
    registry_names = "|".join(re.escape(name) for name in registries)
    backup_name = re.compile(rf"(?:{registry_names})\.v[0-9]+(?:\.[0-9]+)*-[0-9a-f]{{64}}\.bak", re.ASCII)
    _measure(root, lambda name: backup_name.fullmatch(name) is not None, locations["backups"])
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "total_bytes": sum(usage.bytes for usage in locations.values()),
        "complete": not any(usage.skipped for usage in locations.values()),
        "categories": [{"id": category, **asdict(usage)} for category, usage in locations.items()],
    }
