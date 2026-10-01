"""Translate explicit provider attachment rejections into safe, stable client errors."""

import re


class ProviderAttachmentError(RuntimeError):
    """A provider rejected native image or document input in the current request."""

    def __init__(self, code: str, message: str) -> None:
        """Expose a stable code without forwarding raw provider payloads."""
        super().__init__(message)
        self.code = code

    @classmethod
    def from_provider(
        cls, error: object, inputs: list[dict]
    ) -> "ProviderAttachmentError | None":
        """Recognize attachment errors without misclassifying authentication or outages."""
        has_native = any(
            isinstance(item.get("content"), list)
            and any(
                part.get("type") in {"input_image", "input_file"}
                for part in item["content"]
            )
            for item in inputs
        )
        if not has_native:
            return None
        status = getattr(error, "status_code", None)
        if status is not None and status not in {400, 422}:
            return None
        body = getattr(error, "body", None)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            body = body["error"]
        body = body if isinstance(body, dict) else {}
        code = str(
            body.get("code") or getattr(error, "code", None) or ""
        ).lower()
        message = str(
            body.get("message") or getattr(error, "message", None) or ""
        ).lower()
        param = str(
            body.get("param") or getattr(error, "param", None) or ""
        ).lower()
        mentions_attachment = any(
            re.search(
                r"(?<![a-z])(?:images?|vision|pdf|files?|input_file|"
                r"input_image)(?![a-z])",
                value,
            )
            for value in (message, code, param)
        )
        unsupported = any(
            phrase in message
            for phrase in (
                "does not support",
                "not supported",
                "unsupported",
                "only supports",
                "only support",
                "only accepts text",
                "text-only",
                "text only",
                "doesn't support",
            )
        ) or code in {
            "unsupported_image",
            "unsupported_file",
            "unsupported_content_type",
        }
        if mentions_attachment and unsupported:
            return cls(
                "attachment_model_unsupported",
                "The selected model or deployment does not support this attachment input. "
                "Choose a compatible model or remove the image/PDF. "
                "No automatic retry was made.",
            )
        if mentions_attachment and (
            code.startswith("invalid_")
            or code
            in {"image_parse_error", "file_parse_error", "invalid_value"}
            or param.startswith("input")
        ):
            return cls(
                "attachment_provider_rejected",
                "The provider could not process an image or PDF. "
                "Check the file and the deployment's supported formats, then try again. "
                "No automatic retry was made.",
            )
        return None
