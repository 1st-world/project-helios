"""Import only native-picker-selected host files as validated attachment copies.

Reuse folder-picker process ownership while keeping selected host paths off the wire.
"""

import logging
import os
import stat
from copy import copy
from pathlib import Path

from services.attachment_errors import AttachmentError
from services.attachment_service import AttachmentService
from services.folder_picker_service import FolderPickerService, PickerSession
from services.local_work import run_local

logger = logging.getLogger(__name__)


class FilePickerService(FolderPickerService):
    """Prepare a bounded, atomic file selection before exposing attachment references."""

    WORKER_NAME = "file_picker_worker.py"
    RESULT_ERROR = "File picker exited unexpectedly. Try Choose files again."

    def __init__(self, attachments: AttachmentService):
        """Use the shared attachment store and current parser limits for selected files."""
        super().__init__()
        if attachments.store is None:
            raise ValueError("Native file selection requires attachment storage.")
        self.attachments = attachments

    def _valid_selection(self, result: dict) -> bool:
        """Accept absolute file paths produced by the owned native worker only."""
        paths = result.get("paths")
        return (
            isinstance(paths, list) and bool(paths)
            and all(
                isinstance(path, str) and "\0" not in path
                and Path(path).is_absolute() for path in paths
            )
        )

    async def _read_result(self, session: PickerSession) -> None:
        """Replace native paths with prepared references before any result is relayed."""
        await super()._read_result(session)
        if session.cancelling or session.result.get("status") != "selected":
            return
        paths = session.result["paths"]
        session.result = {"status": "preparing"}
        service = copy(self.attachments)
        try:
            files = await run_local(self._import_files, paths, session, service)
            if not session.cancelling:
                session.result = {"status": "selected", "files": files}
        except AttachmentError as exc:
            if not session.cancelling:
                detail = f"{exc.path}: {exc}" if exc.path else str(exc)
                session.result = {"status": "error", "detail": detail}
        except Exception:
            logger.exception("Could not prepare native attachment selection")
            if not session.cancelling:
                session.result = {
                    "status": "error",
                    "detail": "Could not prepare the selected files. Check access and try Choose files again.",
                }

    def _import_files(
        self, paths: list[str], session: PickerSession, service: AttachmentService,
    ) -> list[dict]:
        """Copy regular files with shared limits and roll back an incomplete batch."""
        selected = list(dict.fromkeys(paths))
        if len(selected) > service.MAX_FILES:
            raise AttachmentError(
                "attachment_count_exceeded",
                f"Choose up to {service.MAX_FILES} files at a time.", "", 400,
            )
        originals = []
        complete = False
        total = 0
        try:
            for value in selected:
                self._check_cancelled(session)
                path = Path(value)
                remaining = service.MAX_TOTAL_BYTES - total
                if remaining <= 0:
                    raise AttachmentError(
                        "attachment_too_large", "The selected files exceed the total attachment size limit.", "", 413,
                    )
                writer = service.store.stage(
                    path.name, min(service.MAX_FILE_BYTES, remaining),
                )
                published = False
                try:
                    writer.open()
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("Only regular files can be attached.")
                    flags = (
                        os.O_RDONLY | getattr(os, "O_BINARY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    with os.fdopen(os.open(path, flags), "rb") as source:
                        opened = os.fstat(source.fileno())
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or not os.path.samestat(info, opened)
                        ):
                            raise OSError("The selected file changed before it was opened.")
                        while chunk := source.read(65536):
                            self._check_cancelled(session)
                            writer.write(chunk)
                    writer.seal()
                    metadata = service.validate_upload(writer)
                    self._check_cancelled(session)
                    original = writer.publish()
                    originals.append({**original, "inspection": metadata})
                    published = True
                    total += original["size_bytes"]
                except OSError:
                    raise AttachmentError(
                        "attachment_read_failed", f"Could not read “{path.name}”. Select an accessible regular file and try again.",
                        path.name, 400,
                    ) from None
                finally:
                    writer.abort(discard=not published)
            self._check_cancelled(session)
            complete = True
            return originals
        finally:
            if not complete:
                for original in originals:
                    service.store.delete(original["attachment_id"])

    @staticmethod
    def _check_cancelled(session: PickerSession) -> None:
        """Stop cooperative copying before publishing a cancelled selection."""
        if session.cancelling:
            raise AttachmentError("attachment_cancelled", "File selection cancelled.", "", 499)
