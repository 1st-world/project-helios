"""List and read workspace files while enforcing the configured root boundary."""

import os
from pathlib import Path
from uuid import uuid4

from services.bounded_io import read_bounded
from services.local_work import check_local_cancelled


class WorkspaceAccessError(Exception):
    """An attempted workspace operation was not safe or possible."""


class WorkspaceService:
    """Expose text files and directory listings confined to the selected workspace root."""

    def __init__(self, root: Path | None = None) -> None:
        """Keep an unselected root or initialize an explicitly supplied workspace."""
        self.root = root.resolve() if root is not None else None
        self.revision = 0
        self.session_id = str(uuid4())
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

    def set_root(self, root_path: str | None) -> None:
        """Select an existing absolute folder or explicitly clear the current root."""
        if root_path is None:
            if self.root is not None:
                self.root = None
                self.revision += 1
            return
        if not root_path.strip():
            raise WorkspaceAccessError("Workspace path must not be empty. Use null to clear the selection.")
        if "\x00" in root_path:
            raise WorkspaceAccessError("Workspace path must not contain null characters.")
        candidate = Path(root_path).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceAccessError("Workspace path must be absolute.")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceAccessError(
                "Workspace folder does not exist or cannot be resolved."
            ) from exc
        if not resolved.is_dir():
            raise WorkspaceAccessError("Workspace path must be a folder.")
        if self.root != resolved:
            self.root = resolved
            self.revision += 1

    def _resolve(self, relative_path: str, *, root: Path | None = None) -> Path:
        """Resolve a relative path within the current or snapshotted workspace boundary."""
        check_local_cancelled()
        if "\x00" in relative_path:
            raise WorkspaceAccessError("Project path must not contain null characters.")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise WorkspaceAccessError("Absolute paths are not allowed.")
        boundary = self.root if root is None else root
        if boundary is None:
            raise WorkspaceAccessError("No workspace is selected. Select a workspace before accessing project files.")
        try:
            resolved = (boundary / candidate).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceAccessError("The project path could not be resolved.") from exc
        try:
            resolved.relative_to(boundary)
        except ValueError as exc:
            raise WorkspaceAccessError(
                "Path must remain inside the workspace."
            ) from exc
        return resolved

    def tree(self) -> list[dict]:
        """Return a directory-first file tree, skipping inaccessible folders and escaping links."""
        root = self.root
        if root is None:
            return []

        def sort_key(path: Path) -> tuple[bool, str]:
            """Check cancellation while collecting filesystem sort keys."""
            check_local_cancelled()
            return not path.is_dir(), path.name.lower()

        def walk(folder: Path, ancestors: set[Path]) -> list[dict]:
            """Collect child entries recursively while enforcing the workspace boundary."""
            entries: list[dict] = []
            check_local_cancelled()
            try:
                children = sorted(
                    folder.iterdir(),
                    key=sort_key,
                )
            except PermissionError:
                return entries
            for child in children:
                check_local_cancelled()
                # Skip symlinks that resolve outside the workspace.
                try:
                    resolved = child.resolve()
                    resolved.relative_to(root)
                except (ValueError, OSError):
                    continue
                relative = child.relative_to(root).as_posix()
                if child.is_dir():
                    # Show ancestor links without recursing back into them.
                    descendants = (
                        [] if resolved in ancestors
                        else walk(child, ancestors | {resolved})
                    )
                    entries.append(
                        {
                            "name": child.name,
                            "path": relative,
                            "type": "directory",
                            "children": descendants,
                        }
                    )
                else:
                    entries.append(
                        {"name": child.name, "path": relative, "type": "file"}
                    )
            return entries

        return walk(root, {root})

    def read_text(self, relative_path: str, *, max_bytes: int | None = None) -> str:
        """Read an existing UTF-8 file after validating its workspace-relative path."""
        path = self._resolve(relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(relative_path)
        try:
            if max_bytes is not None:
                with path.open("rb") as source:
                    data = read_bounded(source, max_bytes)
                if len(data) > max_bytes:
                    raise WorkspaceAccessError(
                        f"Workspace text exceeds the local read limit of {max_bytes} bytes."
                    )
                return (
                    data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                )
            parts: list[str] = []
            with path.open("r", encoding="utf-8") as source:
                while True:
                    check_local_cancelled()
                    text = source.read(64 * 1024)
                    check_local_cancelled()
                    if not text:
                        break
                    parts.append(text)
            return "".join(parts)
        except PermissionError:
            raise
        except UnicodeDecodeError as exc:
            raise WorkspaceAccessError(
                "File is not valid UTF-8 text."
            ) from exc

    def project_context(
        self,
        *,
        relative_path: str = "",
        max_depth: int | None = None,
        max_entries: int | None = None,
        include_absolute_path: bool = False,
    ) -> str:
        """Describe an explicitly requested structure without reading file contents.

        Args:
            relative_path: Workspace-relative directory or file to describe.
            max_depth: Traversal depth, with direct children at one, or all depths.
            max_entries: Requested maximum included entries, or no entry limit.
            include_absolute_path: Include the host root only when explicitly chosen.

        Returns:
            Structure, selected scope, actual entry count, and omission details.

        Raises:
            WorkspaceAccessError: The scope or a requested traversal limit is invalid.
        """
        root = self.root
        if root is None:
            return ""
        for name, value in (("depth", max_depth), ("entry limit", max_entries)):
            if value is not None and (type(value) is not int or value < 1):
                raise WorkspaceAccessError(f"Structure {name} must be a positive integer or null.")
        scope = self._resolve(relative_path)
        if not scope.exists():
            raise WorkspaceAccessError("The selected structure scope does not exist.")
        scope_name = scope.relative_to(root).as_posix()
        paths: list[str] = []
        omissions: list[str] = []

        def walk(folder: Path, depth: int, ancestors: set[Path]) -> bool:
            """Enumerate lazily and stop before exceeding the requested result range."""
            check_local_cancelled()
            folder_name = folder.relative_to(root).as_posix()
            try:
                with os.scandir(folder) as children:
                    for entry in children:
                        check_local_cancelled()
                        if max_entries is not None and len(paths) >= max_entries:
                            omissions.append(f"{folder_name}: additional entries omitted at the selected entry limit.")
                            return False
                        path = folder / entry.name
                        name = path.relative_to(root).as_posix()
                        try:
                            resolved = path.resolve(strict=True)
                            resolved.relative_to(root)
                            is_directory = entry.is_dir()
                        except ValueError:
                            omissions.append(f"{name}: excluded because its target is outside the workspace.")
                            continue
                        except (OSError, RuntimeError):
                            omissions.append(f"{name}: inaccessible, missing, or an unresolved link.")
                            continue
                        paths.append(name + ("/" if is_directory else ""))
                        if not is_directory:
                            continue
                        if resolved in ancestors:
                            omissions.append(f"{name}/: ancestor link not traversed.")
                        elif max_depth is not None and depth >= max_depth:
                            omissions.append(f"{name}/: subtree not traversed beyond the selected depth.")
                        elif max_entries is not None and len(paths) >= max_entries:
                            omissions.append(f"{name}/: subtree not traversed at the selected entry limit.")
                            return False
                        elif not walk(path, depth + 1, ancestors | {resolved}):
                            return False
            except OSError:
                omissions.append(f"{folder_name}: directory entries could not be read.")
            return True

        if scope.is_dir():
            if not walk(scope, 1, {root, scope}):
                omissions.append("Remaining entries within the selected scope were not fully traversed at the selected entry limit.")
        else:
            paths.append(scope_name)
        listing = "\n".join(paths) if paths else "(no entries included)"
        omitted = "\n".join(omissions) if omissions else "None within the selected scope and depth."
        workspace_name = str(root) if include_absolute_path else root.name or "Workspace"
        return (
            f"Project workspace: {workspace_name}\n"
            f"Structure scope: {scope_name}\n"
            f"Maximum depth: {max_depth if max_depth is not None else 'all'}\n"
            f"Entry limit: {max_entries if max_entries is not None else 'none'}\n"
            f"Entries included: {len(paths)}\n"
            f"Project tree:\n{listing}\n"
            f"Omissions and untraversed ranges:\n{omitted}"
        )

    def selected_context(
        self,
        relative_paths: list[str],
        *,
        exclude_paths: set[Path] | None = None,
        max_file_bytes: int | None = None,
        max_total_bytes: int | None = None,
        max_files: int | None = None,
    ) -> str:
        """Read each selected file once and reject the whole selection on a file error."""
        seen: set[Path] = set(exclude_paths or ())
        sections: list[str] = []
        total = 0
        for relative_path in relative_paths:
            check_local_cancelled()
            try:
                resolved = self._resolve(relative_path)
                if resolved in seen:
                    continue
                if max_files is not None and len(seen) >= max_files:
                    raise WorkspaceAccessError("Selection exceeds the local attachment count limit.")
                read_limit = max_file_bytes
                if max_total_bytes is not None:
                    # Limit raw reads before loading another file.
                    # This local budget does not estimate model input tokens.
                    remaining = max(0, max_total_bytes - total)
                    read_limit = (
                        remaining if read_limit is None
                        else min(read_limit, remaining)
                    )
                content = (
                    self.read_text(relative_path, max_bytes=read_limit)
                    if read_limit is not None else self.read_text(relative_path)
                )
                total += len(content.encode("utf-8"))
                if max_total_bytes is not None and total > max_total_bytes:
                    raise WorkspaceAccessError("Workspace text exceeds the local combined read limit.")
            except (OSError, WorkspaceAccessError) as exc:
                raise WorkspaceAccessError(
                    f"Cannot load workspace file {relative_path!r}: {exc}"
                ) from exc
            seen.add(resolved)
            path = resolved.relative_to(self.root).as_posix()
            sections.append(f"\n\nSelected file: {path}\n{content}")
        return "".join(sections)
