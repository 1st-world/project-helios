"""Estimate text with optional local tokenizer assets without runtime downloads.

An explicit setup command may download the public vocabulary.
"""

import argparse
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

import tiktoken
from tiktoken.load import load_tiktoken_bpe

from config import settings


VOCABULARY_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
)
VOCABULARY_SHA256 = (
    "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
)
_tokenizer_path = ContextVar[Path | None]("helios_tokenizer_path", default=None)
# Use tiktoken 0.12's o200k_base pattern with locally verified vocabulary bytes.
PATTERN = "|".join(
    (
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*"
        r"[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+"
        r"[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    )
)


@lru_cache(maxsize=4)
def _encoding(path: str) -> tiktoken.Encoding | None:
    """Load a verified local asset or return no tokenizer without network access."""
    try:
        with Path(path).open("rb") as source:
            data = source.read(5 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != VOCABULARY_SHA256:
            return None
        return tiktoken.Encoding(
            name="helios_o200k_base",
            pat_str=PATTERN,
            mergeable_ranks=load_tiktoken_bpe(path, VOCABULARY_SHA256),
            special_tokens={},
        )
    except (OSError, ValueError):
        return None


@contextmanager
def use_tokenizer_path(path: Path) -> Iterator[None]:
    """Use the app's configured vocabulary for the current request."""
    token = _tokenizer_path.set(path)
    try:
        yield
    finally:
        _tokenizer_path.reset(token)


def _local_encoding() -> tiktoken.Encoding | None:
    """Resolve the request's vocabulary or the standalone tool default."""
    return _encoding(str((_tokenizer_path.get() or settings.tokenizer_path).resolve()))


def text_tokens(text: str) -> int:
    """Count the local encoding or use a UTF-8 byte heuristic without a model guarantee."""
    encoding = _local_encoding()
    if encoding:
        return len(encoding.encode_ordinary(text))
    return len(text.encode("utf-8"))


def estimation_method() -> str:
    """Name the local method without claiming exact provider request tokenization."""
    return (
        "o200k_base_with_approximate_framing"
        if _local_encoding()
        else "utf8_byte_heuristic_with_approximate_framing"
    )


def estimation_metadata(*, enabled: bool = True) -> dict:
    """Describe local planning estimates without claiming deployment tokenizer compatibility."""
    return {
        "estimation_method": estimation_method() if enabled else "disabled",
        "estimate_is_exact": False,
        "estimate_is_model_specific": False if enabled else None,
    }


def main() -> None:
    """Download vocabulary only when explicitly invoked as a setup operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", required=True)
    args = parser.parse_args()
    if args.download:
        with urlopen(VOCABULARY_URL, timeout=30) as response:
            data = response.read(5 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != VOCABULARY_SHA256:
            raise ValueError("Tokenizer vocabulary integrity check failed.")
        settings.tokenizer_path.parent.mkdir(parents=True, exist_ok=True)
        settings.tokenizer_path.write_bytes(data)
        print("Local tokenizer vocabulary installed.")


if __name__ == "__main__":
    main()
