"""Validate workspace attachments and prepare bounded text or native visual inputs.

Keep binary payloads out of stored transcripts and expose metadata for future clients.
"""

import base64
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from math import ceil
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.table import Table
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._reader import WorkSheetParser
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader

from services.workspace_service import WorkspaceAccessError, WorkspaceService


class AttachmentError(ValueError):
    """Describe a file-specific validation failure without exposing its contents."""

    def __init__(
        self, code: str, message: str, path: str, status_code: int = 400
    ) -> None:
        """Keep stable client error codes alongside a readable explanation."""
        super().__init__(message)
        self.code = code
        self.path = path
        self.status_code = status_code

    def detail(self) -> dict:
        """Return safe structured error information for an HTTP response."""
        return {"code": self.code, "message": str(self), "path": self.path}


@dataclass
class PreparedAttachments:
    """Carry transient provider parts and local estimates independently of metadata."""

    content: list[dict] = field(default_factory=list)
    metadata: list[dict] = field(default_factory=list)
    visual_tokens: int = 0


class AttachmentService:
    """Read supported local files once, enforcing root, size, and parser limits."""

    # Defaults below are provisional desktop resource policies.
    # Their safety has not been benchmarked across workloads or hardware.
    # Model token capacity does not determine these parser-work limits.
    # Match Azure's image count initially, but count every local file here.
    MAX_FILES = 50
    # Begin below Azure's decimal 50 MB native-input file ceiling.
    # The common total is a provisional budget for reading mixed formats.
    # Base64 and JSON duplicate payloads; this is not a peak-memory bound.
    MAX_FILE_BYTES = 49_999_999
    MAX_TOTAL_BYTES = 49_999_999
    # Bound text retained for tokenization and prompts, not model input tokens.
    # The initial 4 MiB allocation policy has not been benchmarked.
    MAX_TEXT_BYTES = 4 * 1024 * 1024
    # Bound Office ZIP expansion independently of compressed source size.
    # Byte and entry budgets are provisional parser-work limits.
    MAX_ZIP_BYTES = 128 * 1024 * 1024
    MAX_ZIP_MEMBERS = 10_000
    # 50 MP RGB/RGBA pixels alone need about 150-200 MB before decoder copies.
    # This provisional pixel guard is not a measured peak-memory guarantee.
    MAX_IMAGE_PIXELS = 50_000_000
    # A generous initial page count bounds local page-object traversal.
    # This is not an Azure page limit or benchmarked latency threshold.
    MAX_PDF_PAGES = 1024
    # Retain the existing initial work budget while counting stored cells.
    # Empty rectangular gaps do not count toward the provisional cell limit.
    MAX_SHEET_CELLS = 100_000
    # Azure Responses requirements were checked on 2026-10-02.
    # Images are limited to 50; each file/category total must be under 50 MB.
    # User resource policies cannot raise these provider limits.
    # https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses
    PROVIDER_FILE_BYTES = 50_000_000
    PROVIDER_IMAGE_COUNT = 50
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    IMAGE_MIMES = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }
    OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
    UNSUPPORTED_EXTENSIONS = {
        ".doc",
        ".xls",
        ".ppt",
        ".docm",
        ".xlsm",
        ".pptm",
        ".zip",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".bin",
        ".mp3",
        ".wav",
        ".mp4",
        ".mov",
        ".avi",
        ".heic",
        ".tiff",
    }

    def __init__(
        self, workspace: WorkspaceService, *, limits: dict | None = None
    ) -> None:
        """Use workspace boundaries and configurable local parser resource limits."""
        self.workspace = workspace
        for name, value in (limits or {}).items():
            if not name.startswith("MAX_") or not hasattr(self, name):
                raise ValueError("Unknown attachment resource limit.")
            if type(value) is not int or value < 1:
                raise ValueError("Attachment resource limits must be positive integers.")
            setattr(self, name, value)
        if Image.MAX_IMAGE_PIXELS is not None:
            self.MAX_IMAGE_PIXELS = min(
                self.MAX_IMAGE_PIXELS, Image.MAX_IMAGE_PIXELS
            )

    def prepare(self, paths: list[str]) -> PreparedAttachments:
        """Validate an entire selection before returning any provider content."""
        result = PreparedAttachments()
        root = self.workspace.root
        seen: set[Path] = set()
        total = 0
        native_bytes = {"image": 0, "pdf": 0}
        image_count = 0
        for supplied_path in paths:
            try:
                path = self.workspace._resolve(supplied_path, root=root)
                if path in seen:
                    continue
                if len(seen) >= self.MAX_FILES:
                    raise AttachmentError(
                        "attachment_limit",
                        f"Selection exceeds the local limit of {self.MAX_FILES} distinct attachments.",
                        supplied_path, 413,
                    )
                if not path.is_file():
                    raise AttachmentError(
                        "attachment_not_found",
                        "Attachment file not found.",
                        supplied_path,
                        404,
                    )
                size = path.stat().st_size
                native = path.suffix.lower() in self.IMAGE_EXTENSIONS | {".pdf"}
                if native and size >= self.PROVIDER_FILE_BYTES:
                    raise AttachmentError(
                        "attachment_provider_limit",
                        "Azure native image and PDF inputs must be smaller than 50 MB.",
                        supplied_path, 413,
                    )
                if size > self.MAX_FILE_BYTES:
                    raise AttachmentError(
                        "attachment_too_large",
                        f"Attachment exceeds the local read limit of {self.MAX_FILE_BYTES} bytes.",
                        supplied_path,
                        413,
                    )
                # Bound reads even if a file grows after stat().
                with path.open("rb") as source:
                    data = source.read(
                        min(self.MAX_FILE_BYTES, self.MAX_TOTAL_BYTES - total) + 1
                    )
                total += len(data)
                if (
                    len(data) > self.MAX_FILE_BYTES
                    or total > self.MAX_TOTAL_BYTES
                ):
                    raise AttachmentError(
                        "attachment_too_large",
                        f"Attachments exceed the local combined read limit of {self.MAX_TOTAL_BYTES} bytes.",
                        supplied_path,
                        413,
                    )
                canonical = path.relative_to(root).as_posix()
                parts, metadata, estimate = self._prepare_file(canonical, data)
                if native:
                    kind = metadata["kind"]
                    native_bytes[kind] += len(data)
                    image_count += metadata["kind"] == "image"
                    if (
                        native_bytes[kind] >= self.PROVIDER_FILE_BYTES
                        or image_count > self.PROVIDER_IMAGE_COUNT
                    ):
                        raise AttachmentError(
                            "attachment_provider_limit",
                            "Native inputs exceed the Azure limit of 50 images or 50 MB within one input category.",
                            supplied_path, 413,
                        )
            except AttachmentError:
                raise
            except PermissionError as exc:
                raise AttachmentError(
                    "attachment_permission_denied",
                    "Cannot read this attachment.",
                    supplied_path,
                    403,
                ) from exc
            except WorkspaceAccessError as exc:
                raise AttachmentError(
                    "attachment_path_invalid", str(exc), supplied_path
                ) from exc
            except OSError as exc:
                raise AttachmentError(
                    "attachment_unavailable",
                    "Attachment could not be read. Select it again.",
                    supplied_path,
                ) from exc
            seen.add(path)
            result.content.extend(parts)
            result.metadata.append(metadata)
            result.visual_tokens += estimate
        if self.workspace.root != root:
            raise AttachmentError(
                "attachment_workspace_changed",
                "Workspace changed while reading attachments. Select the files again.",
                "",
                409,
            )
        return result

    def _prepare_file(
        self, path: str, data: bytes
    ) -> tuple[list[dict], dict, int]:
        """Dispatch validated bytes without trusting the filename as proof of format."""
        extension = Path(path).suffix.lower()
        metadata = {"path": path, "size_bytes": len(data), "warnings": []}
        try:
            if extension in self.IMAGE_EXTENSIONS:
                part, dimensions = self._image(path, data)
                metadata.update(kind="image", delivery="native", **dimensions)
                return (
                    self._label(path, part),
                    metadata,
                    self._visual_estimate(
                        dimensions["width"], dimensions["height"]
                    ),
                )
            if extension == ".pdf":
                part, pages, estimate = self._pdf(path, data)
                metadata.update(kind="pdf", delivery="native", pages=pages)
                metadata["warnings"] = [
                    "The local PDF estimate covers page visuals only; provider-extracted text and rendering can change token usage."
                ]
                return (
                    self._label(path, part),
                    metadata,
                    estimate,
                )
            if extension in self.UNSUPPORTED_EXTENSIONS:
                raise AttachmentError(
                    "attachment_type_unsupported",
                    "This file format is not supported.",
                    path,
                    415,
                )
            if extension in self.OFFICE_EXTENSIONS:
                self._validate_zip(path, data)
                if extension == ".docx":
                    text = self._word(data)
                elif extension == ".xlsx":
                    text = self._spreadsheet(path, data)
                else:
                    text = self._presentation(data)
                metadata.update(kind=extension[1:], delivery="extracted_text")
                metadata["warnings"] = [
                    "Only document text, tables, and cell formulas are "
                    "included; embedded images, charts, and formatting "
                    "are omitted."
                ]
                if not text.strip():
                    raise AttachmentError(
                        "attachment_empty",
                        "No readable document text was found. Export visual "
                        "content as PDF or an image.",
                        path,
                    )
            else:
                text = data.decode("utf-8-sig")
                if any(
                    ord(character) < 32 and character not in "\t\n\r"
                    for character in text
                ):
                    raise AttachmentError(
                        "attachment_type_unsupported",
                        "File is binary rather than UTF-8 text.",
                        path,
                        415,
                    )
                metadata.update(kind="text", delivery="text")
            self._check_text(path, text)
            return (
                [
                    {
                        "type": "input_text",
                        "text": f"Attached file: {path}\n{text}",
                    }
                ],
                metadata,
                0,
            )
        except AttachmentError:
            raise
        except UnicodeDecodeError as exc:
            raise AttachmentError(
                "attachment_type_unsupported",
                "Use UTF-8 text, PDF, DOCX, XLSX, PPTX, or a supported image.",
                path,
                415,
            ) from exc
        except (
            Image.DecompressionBombWarning, Image.DecompressionBombError
        ) as exc:
            raise AttachmentError(
                "attachment_too_large",
                "Image exceeds the local decoder pixel limit.",
                path,
                413,
            ) from exc
        except Exception as exc:
            raise AttachmentError(
                "attachment_invalid",
                "File is damaged, encrypted, or does not match its format.",
                path,
            ) from exc

    @staticmethod
    def _label(path: str, part: dict) -> list[dict]:
        """Keep a filename label adjacent to its native visual content."""
        return [{"type": "input_text", "text": f"Attached file: {path}"}, part]

    def _check_text(self, path: str, text: str) -> None:
        """Reject oversized extracted content without silently truncating documents."""
        if len(text.encode("utf-8")) > self.MAX_TEXT_BYTES:
            raise AttachmentError(
                "attachment_text_too_large",
                f"Extracted text exceeds the local limit of {self.MAX_TEXT_BYTES} bytes.",
                path,
                413,
            )

    def _image(self, path: str, data: bytes) -> tuple[dict, dict]:
        """Verify supported image data and reject animation or excessive dimensions."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                mime = self.IMAGE_MIMES.get(image.format)
                if not mime:
                    raise AttachmentError(
                        "attachment_type_unsupported",
                        "Unsupported image encoding.",
                        path,
                        415,
                    )
                width, height = image.size
                if width * height > self.MAX_IMAGE_PIXELS:
                    raise AttachmentError(
                        "attachment_too_large",
                        f"Image exceeds the local decode limit of {self.MAX_IMAGE_PIXELS} pixels.",
                        path,
                        413,
                    )
                if getattr(image, "is_animated", False):
                    raise AttachmentError(
                        "attachment_type_unsupported",
                        "Animated images are not supported. Select a still image.",
                        path,
                        415,
                    )
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
        return (
            {
                "type": "input_image",
                "image_url": self._data_url(mime, data),
            },
            {"mime_type": mime, "width": width, "height": height},
        )

    def _pdf(self, path: str, data: bytes) -> tuple[dict, int, int]:
        """Inspect PDF structure without making local text extraction a native-input requirement."""
        if b"%PDF-" not in data[:1024]:
            raise AttachmentError(
                "attachment_invalid", "File is not a valid PDF.", path
            )
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise AttachmentError(
                "attachment_encrypted",
                "Password-protected PDFs are not supported. Provide an unlocked copy.",
                path,
            )
        pages = len(reader.pages)
        if not 1 <= pages <= self.MAX_PDF_PAGES:
            raise AttachmentError(
                "attachment_too_large",
                f"PDF exceeds the local page inspection limit of {self.MAX_PDF_PAGES}, or has no pages.",
                path,
                413,
            )
        estimate = sum(
            self._visual_estimate(
                float(page.mediabox.width) * 2,
                float(page.mediabox.height) * 2,
            )
            for page in reader.pages
        )
        return (
            {
                "type": "input_file",
                "filename": Path(path).name,
                "file_data": self._data_url("application/pdf", data),
            },
            pages,
            estimate,
        )

    @staticmethod
    def _visual_estimate(width: float, height: float) -> int:
        """Use dimension-based patches as an uncertain proxy, never exact model tokenization."""
        return max(1, ceil(abs(width) / 32) * ceil(abs(height) / 32))

    @staticmethod
    def _data_url(mime: str, data: bytes) -> str:
        """Encode validated bytes inline without uploading persistent provider files."""
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    def _validate_zip(self, path: str, data: bytes) -> None:
        """Reject encrypted or oversized Office containers before invoking their parsers."""
        if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise AttachmentError(
                "attachment_encrypted",
                "Encrypted or legacy Office containers are not supported. "
                "Export an unlocked DOCX, XLSX, or PPTX copy.",
                path,
            )
        try:
            with ZipFile(BytesIO(data)) as archive:
                members = archive.infolist()
                if (
                    len(members) > self.MAX_ZIP_MEMBERS
                    or sum(member.file_size for member in members)
                    > self.MAX_ZIP_BYTES
                ):
                    raise AttachmentError(
                        "attachment_too_large",
                        "Expanded document exceeds the parser limit.",
                        path,
                        413,
                    )
                if any(member.flag_bits & 1 for member in members):
                    raise AttachmentError(
                        "attachment_encrypted",
                        "Encrypted documents are not supported.",
                        path,
                    )
        except BadZipFile as exc:
            raise AttachmentError(
                "attachment_invalid",
                "File is not a valid Office document.",
                path,
            ) from exc

    @staticmethod
    def _word(data: bytes) -> str:
        """Extract ordered Word body paragraphs, tables, headers, and footers."""
        document = Document(BytesIO(data))
        sections = []
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                sections.extend(
                    "\t".join(cell.text for cell in row.cells)
                    for row in block.rows
                )
            else:
                sections.append(block.text)
        seen = set()
        for section in document.sections:
            for region in (section.header, section.footer):
                if region.part.partname in seen:
                    continue
                seen.add(region.part.partname)
                sections.extend(
                    paragraph.text for paragraph in region.paragraphs
                )
                for table in region.tables:
                    sections.extend(
                        "\t".join(cell.text for cell in row.cells)
                        for row in table.rows
                    )
        return "\n".join(sections)

    def _spreadsheet(self, path: str, data: bytes) -> str:
        """Extract labelled spreadsheet cells and formulas without running workbook code."""
        workbook = load_workbook(
            BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
        sections = []
        cells = 0
        try:
            for sheet in workbook.worksheets:
                sections.append(f"Sheet: {sheet.title}")
                # The public row iterator pads gaps; inspect stored XML cells.
                with sheet._get_source() as source:
                    parser = WorkSheetParser(
                        source, sheet._shared_strings, data_only=False,
                        epoch=workbook.epoch,
                        date_formats=workbook._date_formats,
                        timedelta_formats=workbook._timedelta_formats,
                    )
                    for _, row in parser.parse():
                        cells += len(row)
                        if cells > self.MAX_SHEET_CELLS:
                            raise AttachmentError(
                                "attachment_too_large",
                                f"Spreadsheet exceeds the local limit of {self.MAX_SHEET_CELLS} stored cells.",
                                path, 413,
                            )
                        values = [
                            f"{get_column_letter(cell['column'])}{cell['row']}={cell['value']}"
                            for cell in row if cell["value"] is not None
                        ]
                        if values:
                            sections.append("\t".join(values))
        finally:
            workbook.close()
        return "\n".join(sections)

    @staticmethod
    def _presentation(data: bytes) -> str:
        """Extract slide text, tables, grouped shapes, and existing speaker notes."""
        presentation = Presentation(BytesIO(data))

        def shape_text(shapes) -> list[str]:
            """Visit nested groups while preserving the slide's shape order."""
            text = []
            for shape in shapes:
                if hasattr(shape, "shapes"):
                    text.extend(shape_text(shape.shapes))
                if shape.has_text_frame:
                    text.append(shape.text_frame.text)
                if shape.has_table:
                    text.extend(
                        "\t".join(cell.text for cell in row.cells)
                        for row in shape.table.rows
                    )
            return text

        sections = []
        for number, slide in enumerate(presentation.slides, 1):
            sections.extend([f"Slide: {number}", *shape_text(slide.shapes)])
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                sections.append(
                    "Notes: " + slide.notes_slide.notes_text_frame.text
                )
        return "\n".join(sections)
