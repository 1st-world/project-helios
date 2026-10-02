"""List and read workspace files while enforcing the configured root boundary."""

from pathlib import Path


class WorkspaceAccessError(Exception):
    """An attempted workspace operation was not safe or possible."""


class WorkspaceService:
    """Expose text files and directory listings confined to the selected workspace root."""

    def __init__(self, root: Path) -> None:
        """Resolve the initial workspace root and create it if missing."""
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def set_root(self, root_path: str) -> None:
        """Select an existing absolute directory as the workspace root."""
        candidate = Path(root_path).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceAccessError("Workspace path must be absolute.")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceAccessError(
                "Workspace folder does not exist."
            ) from exc
        if not resolved.is_dir():
            raise WorkspaceAccessError("Workspace path must be a folder.")
        self.root = resolved

    def _resolve(self, relative_path: str, *, root: Path | None = None) -> Path:
        """Resolve a relative path within the current or snapshotted workspace boundary."""
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise WorkspaceAccessError("Absolute paths are not allowed.")
        boundary = self.root if root is None else root
        resolved = (boundary / candidate).resolve()
        try:
            resolved.relative_to(boundary)
        except ValueError as exc:
            raise WorkspaceAccessError(
                "Path must remain inside the workspace."
            ) from exc
        return resolved

    def tree(self) -> list[dict]:
        """Return a directory-first file tree, skipping inaccessible folders and escaping links."""

        def walk(folder: Path) -> list[dict]:
            """Collect child entries recursively while enforcing the workspace boundary."""
            entries: list[dict] = []
            try:
                children = sorted(
                    folder.iterdir(),
                    key=lambda path: (not path.is_dir(), path.name.lower()),
                )
            except PermissionError:
                return entries
            for child in children:
                # Skip symlinks that resolve outside the workspace.
                try:
                    child.resolve().relative_to(self.root)
                except (ValueError, OSError):
                    continue
                relative = child.relative_to(self.root).as_posix()
                if child.is_dir():
                    entries.append(
                        {
                            "name": child.name,
                            "path": relative,
                            "type": "directory",
                            "children": walk(child),
                        }
                    )
                else:
                    entries.append(
                        {"name": child.name, "path": relative, "type": "file"}
                    )
            return entries

        return walk(self.root)

    def read_text(self, relative_path: str, *, max_bytes: int | None = None) -> str:
        """Read an existing UTF-8 file after validating its workspace-relative path."""
        path = self._resolve(relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(relative_path)
        try:
            if max_bytes is not None:
                with path.open("rb") as source:
                    data = source.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise WorkspaceAccessError(
                        f"Workspace text exceeds the local read limit of {max_bytes} bytes."
                    )
                return (
                    data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                )
            return path.read_text(encoding="utf-8")
        except PermissionError:
            raise
        except UnicodeDecodeError as exc:
            raise WorkspaceAccessError(
                "File is not valid UTF-8 text."
            ) from exc

    def project_context(self, max_entries: int = 300) -> str:
        """Return a bounded project manifest for every AI request."""
        paths: list[str] = []
        try:
            for path in self.root.rglob("*"):
                if len(paths) >= max_entries:
                    paths.append("... (additional paths omitted)")
                    break
                try:
                    resolved = path.resolve()
                    resolved.relative_to(self.root)
                except (OSError, ValueError):
                    continue
                paths.append(
                    path.relative_to(self.root).as_posix()
                    + ("/" if path.is_dir() else "")
                )
        except OSError:
            paths.append("... (some workspace paths could not be read)")
        listing = "\n".join(paths) if paths else "(empty workspace)"
        return f"Project workspace: {self.root}\nProject tree:\n{listing}"

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
