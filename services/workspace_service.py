"""Safe, workspace-scoped filesystem access."""

from pathlib import Path


class WorkspaceAccessError(Exception):
    """An attempted workspace operation was not safe or possible."""


class WorkspaceService:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def set_root(self, root_path: str) -> None:
        candidate = Path(root_path).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceAccessError("Workspace path must be absolute.")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceAccessError("Workspace folder does not exist.") from exc
        if not resolved.is_dir():
            raise WorkspaceAccessError("Workspace path must be a folder.")
        self.root = resolved

    def _resolve(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise WorkspaceAccessError("Absolute paths are not allowed.")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceAccessError("Path must remain inside the workspace.") from exc
        return resolved

    def tree(self) -> list[dict]:
        def walk(folder: Path) -> list[dict]:
            entries: list[dict] = []
            try:
                children = sorted(folder.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
            except PermissionError:
                return entries
            for child in children:
                # A symlink can appear beneath the workspace while resolving outside it.
                # Do not expose or descend into it.
                try:
                    child.resolve().relative_to(self.root)
                except (ValueError, OSError):
                    continue
                relative = child.relative_to(self.root).as_posix()
                if child.is_dir():
                    entries.append({"name": child.name, "path": relative, "type": "directory", "children": walk(child)})
                else:
                    entries.append({"name": child.name, "path": relative, "type": "file"})
            return entries
        return walk(self.root)

    def read_text(self, relative_path: str) -> str:
        path = self._resolve(relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(relative_path)
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            raise
        except UnicodeDecodeError as exc:
            raise WorkspaceAccessError("File is not valid UTF-8 text.") from exc

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
                paths.append(path.relative_to(self.root).as_posix() + ("/" if path.is_dir() else ""))
        except OSError:
            paths.append("... (some workspace paths could not be read)")
        listing = "\n".join(paths) if paths else "(empty workspace)"
        return f"Project workspace: {self.root}\nProject tree:\n{listing}"
