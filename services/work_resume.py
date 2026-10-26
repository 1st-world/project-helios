"""Validate pinned connections and bounded Workspace inputs before explicit work resumption.

Automatic scope uses metadata, while explicitly selected files also use content hashes.
These checks do not store credentials or lock the filesystem against subsequent changes.
"""

import hashlib
import json
import os
import stat
from dataclasses import asdict, is_dataclass
from pathlib import Path

from services.context_budget import ContextChangedError
from services.local_work import check_local_cancelled
from services.project_access import (
    ProjectAccess, ProjectReadError, final_handle_path, is_link, signature,
)
from services.workspace_service import WorkspaceAccessError

WORKSPACE_MANIFEST_LIMIT = 100_000
CONNECTION_FINGERPRINT_VERSION = 2
_EXCLUDED_PATH_REASONS = {"hidden_path", "ignored_path", "protected_path"}


def _digest(value: object) -> str:
    """Hash a deterministic JSON value without retaining its encoded payload."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def connection_fingerprint(
    profile, memory_policy=None, *, context: object = None,
    version: int = CONNECTION_FINGERPRINT_VERSION, legacy_name: str | None = None,
) -> str:
    """Hash request settings, accepting older checkpoints with their recorded display name."""
    try:
        if (
            type(version) is not int
            or version not in {1, CONNECTION_FINGERPRINT_VERSION}
        ):
            raise ValueError("Unsupported connection fingerprint version.")
        profile_data = asdict(profile) if is_dataclass(profile) else profile
        if isinstance(profile_data, dict):
            if version == CONNECTION_FINGERPRINT_VERSION:
                profile_data = {
                    key: value for key, value in profile_data.items()
                    if key != "name"
                }
            elif legacy_name is not None:
                profile_data = {**profile_data, "name": legacy_name}
        policy_data = getattr(memory_policy, "policy", memory_policy)
        if isinstance(policy_data, dict):
            # Live guards check process-local memory revocation separately.
            policy_data = {
                key: value for key, value in policy_data.items()
                if key != "revision"
            }
        return _digest({
            "profile": profile_data, "memory_policy": policy_data,
            "context": context,
        })
    except (TypeError, ValueError, OverflowError):
        raise ContextChangedError(
            "The saved connection policy could not be verified. Prepare a new request."
        ) from None


def _entry_signature(info: os.stat_result) -> tuple:
    """Include entry type, hardlink count, and Windows visibility in metadata identity."""
    return (
        *signature(info), info.st_mode, info.st_nlink,
        getattr(info, "st_file_attributes", 0),
    )


def _directory_identity(info: os.stat_result) -> tuple:
    """Ignore directory timestamps affected by excluded runtime and hidden children."""
    return (
        info.st_dev, info.st_ino, info.st_mode,
        getattr(info, "st_file_attributes", 0),
    )


def workspace_fingerprint(
    workspace, protected: list[Path], *, max_entries: int = WORKSPACE_MANIFEST_LIMIT,
) -> str:
    """Hash an authorized metadata manifest or fail closed on changes and resource limits.

    Access checks read only bounded ignore-policy files; source file contents are not read.
    Hidden, ignored, and protected trees are excluded, and links are never traversed.
    """
    if type(max_entries) is not int or max_entries < 1:
        raise ValueError("Workspace manifest limit must be a positive integer.")
    try:
        if workspace.root is None:
            raise ProjectReadError("workspace_required")
        binding = (
            str(workspace.root), workspace.revision, workspace.session_id,
        )
        access = ProjectAccess(Path(workspace.root), protected)
        records = {}
        checked = []
        pending = [access.root]
        visited = 0
        while pending:
            check_local_cancelled()
            folder = pending.pop()
            relative_folder = folder.relative_to(access.root).as_posix()
            access.resolve(relative_folder)
            folder_info = folder.lstat()
            directory_before = _entry_signature(folder_info)
            policy_before = access.policy_stamp(folder)
            records[("directory", relative_folder)] = _directory_identity(folder_info)
            checked.append((folder, directory_before, False))
            policy_path = folder / ".gitignore"
            try:
                policy_info = _entry_signature(policy_path.lstat())
            except FileNotFoundError:
                policy_info = None
            records[("policy", relative_folder)] = policy_info
            checked.append((policy_path, policy_info, True))
            children = []
            with os.scandir(folder) as entries:
                for entry in entries:
                    check_local_cancelled()
                    visited += 1
                    if visited > max_entries:
                        raise ProjectReadError("workspace_manifest_limit")
                    path = folder / entry.name
                    relative = path.relative_to(access.root).as_posix()
                    try:
                        access.resolve(relative)
                    except ProjectReadError as exc:
                        if exc.code in _EXCLUDED_PATH_REASONS:
                            continue
                        if exc.code != "linked_path":
                            raise
                        access.resolve(relative_folder)
                        info = path.lstat()
                        if not is_link(info) and not (
                            stat.S_ISREG(info.st_mode) and info.st_nlink > 1
                        ):
                            # Parent replacement cannot become a link placeholder.
                            raise ProjectReadError("workspace_changed")
                        current = _entry_signature(info)
                        records[("link", relative)] = current
                        checked.append((path, current, False))
                        continue
                    info = path.lstat()
                    if is_link(info):
                        raise ProjectReadError("workspace_changed")
                    current = _entry_signature(info)
                    if stat.S_ISDIR(info.st_mode):
                        children.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        records[("file", relative)] = current
                        checked.append((path, current, False))
                    else:
                        raise ProjectReadError("unsupported_project_entry")
            if (
                _entry_signature(folder.lstat()) != directory_before
                or access.policy_stamp(folder) != policy_before
            ):
                raise ProjectReadError("workspace_changed")
            pending.extend(sorted(children, reverse=True))

        for path, previous, policy in checked:
            check_local_cancelled()
            if not policy:
                parent_name = (
                    path.parent.relative_to(access.root).as_posix()
                    if path != access.root else "."
                )
                access.resolve(parent_name)
            try:
                current = _entry_signature(path.lstat())
            except FileNotFoundError:
                current = None
            if current != previous:
                raise ProjectReadError("workspace_changed")
        access.check_root()
        if binding != (str(workspace.root), workspace.revision, workspace.session_id):
            raise ProjectReadError("workspace_changed")
        return _digest({
            "version": 1, "workspace": binding,
            "entries": [[*key, value] for key, value in sorted(records.items())],
        })
    except (OSError, ProjectReadError, TypeError, ValueError):
        raise ContextChangedError(
            "The Workspace could not be verified for resumption. Prepare a new request."
        ) from None


def selected_files_fingerprint(
    workspace, paths: list[str], *, max_total_bytes: int = 50_000_000,
) -> str:
    """Hash explicitly selected files, including consented hidden and ignored paths.

    Selection follows the existing Workspace boundary policy rather than automatic tool scope.
    File identities and the opened handle are checked before and after bounded content reads.
    """
    if type(max_total_bytes) is not int or max_total_bytes < 1:
        raise ValueError("Selected file content limit must be a positive integer.")
    try:
        if (workspace.root is None or not isinstance(paths, list)
                or len(paths) > WORKSPACE_MANIFEST_LIMIT
                or any(not isinstance(path, str) for path in paths)):
            raise WorkspaceAccessError("Selected file inputs are unavailable.")
        root = Path(workspace.root)
        if root.resolve(strict=True) != root or is_link(root.lstat()):
            raise WorkspaceAccessError("Workspace identity changed.")
        root_identity = _directory_identity(root.lstat())
        binding = (str(root), workspace.revision, workspace.session_id)
        records = []
        checked = []
        selected = {}
        for name in paths:
            check_local_cancelled()
            resolved = workspace._resolve(name, root=root)
            selected.setdefault(resolved, name)
        consumed = 0
        for path, name in sorted(selected.items()):
            check_local_cancelled()
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_size > max_total_bytes - consumed):
                raise WorkspaceAccessError("Selected file content limit was exceeded.")
            before = _entry_signature(info)
            digest = hashlib.sha256()
            with path.open("rb") as source:
                opened = final_handle_path(source.fileno(), path)
                if (not opened.is_relative_to(root) or opened != path
                        or _entry_signature(os.fstat(source.fileno())) != before):
                    raise WorkspaceAccessError("Selected file identity changed.")
                while True:
                    check_local_cancelled()
                    chunk = source.read(min(64 * 1024, max_total_bytes - consumed + 1))
                    if not chunk:
                        break
                    consumed += len(chunk)
                    if consumed > max_total_bytes:
                        raise WorkspaceAccessError("Selected file content limit was exceeded.")
                    digest.update(chunk)
                if (_entry_signature(os.fstat(source.fileno())) != before
                        or final_handle_path(source.fileno(), path) != opened):
                    raise WorkspaceAccessError("Selected file changed while being read.")
            if (workspace._resolve(name, root=root) != path
                    or _entry_signature(path.stat()) != before):
                raise WorkspaceAccessError("Selected file changed while being read.")
            records.append((
                path.relative_to(root).as_posix(), before, digest.hexdigest(),
            ))
            checked.append((path, name, before))
        for path, name, before in checked:
            check_local_cancelled()
            if (workspace._resolve(name, root=root) != path
                    or _entry_signature(path.stat()) != before):
                raise WorkspaceAccessError("Selected file changed during verification.")
        if (binding != (str(workspace.root), workspace.revision, workspace.session_id)
                or _directory_identity(root.lstat()) != root_identity):
            raise WorkspaceAccessError("Workspace identity changed.")
        return _digest({"version": 1, "workspace": binding, "files": records})
    except (
        OSError, ProjectReadError, WorkspaceAccessError, TypeError, ValueError,
        RuntimeError,
    ):
        raise ContextChangedError(
            "Selected Workspace files could not be verified for resumption. Prepare a new request."
        ) from None
