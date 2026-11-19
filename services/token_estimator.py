"""Estimate text offline and install the verified vocabulary on explicit request."""

import argparse
import hashlib
import os
import re
import stat
from base64 import b64decode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.request import urlopen

import tiktoken

from config import settings
from services.project_access import signature


VOCABULARY_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
)
VOCABULARY_SHA256 = (
    "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
)
FALLBACK_ESTIMATION_METHOD = "script_aware_heuristic_with_approximate_framing"
_FALLBACK_RUNS = re.compile(
    r"(?P<lowercase> ?[a-z]{2,})|(?P<digits>[0-9]{2,})|"
    r"(?P<hangul>[\uac00-\ud7a3]+)|(?P<kana>[\u3040-\u30ff]+)"
)
# The pinned vocabulary includes every space + lowercase pair and all but these
# lowercase pairs. The exclusions come from checking all 26 × 26 pairs.
_UNMERGED_LOWERCASE_PAIRS = frozenset(
    (
        "bq gk gq jv jz kq kz lq nq qf qg qj qk qv qz rj tq "
        "wq wz xg xj xk xq xv xw yq zj zq zv"
    ).split()
)
_HANGUL_START = 0xAC00
# Bit i records a two-byte token for the UTF-8 prefix of U+AC00 + 64*i.
# The pinned vocabulary contains all three UTF-8 prefixes in U+3040–U+30FF.
_HANGUL_PREFIX_MERGES = 0x67F4DD7FBF5F02DF7FF02FFE03DD97FBF45BBD7F1BBF
MAX_VOCABULARY_BYTES = 5 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
VocabularyStatus = Literal["ready", "missing", "invalid"]
VocabularyRevision = tuple | Literal["missing", "invalid"]
_vocabulary = ContextVar[tuple[Path, VocabularyRevision] | None]("helios_vocabulary", default=None)
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


class VocabularyDownloadError(RuntimeError):
    """Report an unavailable or unverified upstream vocabulary."""


@dataclass(frozen=True)
class VocabularyDownloadProgress:
    """Snapshot the transfer for Settings without exposing its path or contents."""

    phase: Literal["downloading", "verifying"] = "downloading"
    received_bytes: int = 0
    total_bytes: int | None = None


def _revision(path: Path) -> VocabularyRevision:
    """Identify the current regular file without opening it."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "invalid"
    return signature(info) if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_VOCABULARY_BYTES else "invalid"


def _verified(data: bytes | bytearray) -> bool:
    """Check the size bound and pinned digest for local or downloaded bytes."""
    return len(data) <= MAX_VOCABULARY_BYTES and hashlib.sha256(data).hexdigest() == VOCABULARY_SHA256


@lru_cache(maxsize=4)
def _load_encoding(path: Path, revision: VocabularyRevision) -> tiktoken.Encoding | None:
    """Build an encoder from the exact verified bytes of the chosen file revision."""
    if isinstance(revision, str):
        return None
    try:
        with path.open("rb") as source:
            if signature(os.fstat(source.fileno())) != revision:
                return None
            data = source.read(MAX_VOCABULARY_BYTES + 1)
        if not _verified(data):
            return None
        ranks = {}
        for line in data.splitlines():
            if line:
                token, rank = line.split()
                ranks[b64decode(token)] = int(rank)
        return tiktoken.Encoding(
            name="helios_o200k_base",
            pat_str=PATTERN,
            mergeable_ranks=ranks,
            special_tokens={},
        )
    except (OSError, ValueError):
        return None


@contextmanager
def use_tokenizer_path(path: Path) -> Iterator[None]:
    """Pin the vocabulary revision for the current request and its child work."""
    path = path.absolute()
    token = _vocabulary.set((path, _revision(path)))
    try:
        yield
    finally:
        _vocabulary.reset(token)


def _encoding(path: str) -> tiktoken.Encoding | None:
    """Read the current vocabulary revision for standalone callers."""
    source = Path(path).absolute()
    return _load_encoding(source, _revision(source))


def _local_encoding() -> tiktoken.Encoding | None:
    """Resolve the request's vocabulary or the standalone tool default."""
    vocabulary = _vocabulary.get()
    return _load_encoding(*vocabulary) if vocabulary is not None else _encoding(str(settings.tokenizer_path))


def vocabulary_status(path: Path) -> VocabularyStatus:
    """Report the current installation state independently of the caller's snapshot."""
    path = path.absolute()
    revision = _revision(path)
    if isinstance(revision, str):
        return revision
    return "ready" if _load_encoding(path, revision) is not None else "invalid"


def _download_vocabulary(report: Callable[[VocabularyDownloadProgress], None] | None) -> bytearray:
    """Read at most the allowed size, reporting upstream length when available."""
    data = bytearray()
    try:
        with urlopen(VOCABULARY_URL, timeout=30) as response:
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isascii() and length.isdecimal() and len(length) <= 8 else None
            if total is not None and not 0 < total <= MAX_VOCABULARY_BYTES:
                total = None
            while len(data) <= MAX_VOCABULARY_BYTES:
                if report is not None:
                    report(VocabularyDownloadProgress(received_bytes=len(data), total_bytes=total))
                chunk = response.read1(min(DOWNLOAD_CHUNK_BYTES, MAX_VOCABULARY_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if total is not None and len(data) > total:
                    total = None
        if total is not None and total != len(data):
            raise IncompleteRead(b"", total - len(data))
    except IncompleteRead:
        raise VocabularyDownloadError("The vocabulary download ended early. Try the download again.") from None
    except (OSError, HTTPException) as error:
        if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
            raise VocabularyDownloadError("The vocabulary download timed out. Try the download again.") from None
        raise VocabularyDownloadError("Could not download the vocabulary. Check the connection and try again.") from None
    if report is not None:
        report(VocabularyDownloadProgress("verifying", len(data), total))
    return data


def install_vocabulary(path: Path, *, report: Callable[[VocabularyDownloadProgress], None] | None = None) -> None:
    """Download the fixed asset and atomically publish it after checking its hash."""
    if vocabulary_status(path) == "ready":
        return
    data = _download_vocabulary(report)
    if not _verified(data):
        raise VocabularyDownloadError("The downloaded vocabulary failed verification. Try the download again.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".tokenizer-", delete=False) as target:
            temporary = Path(target.name)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _lowercase_merge_savings(run: str) -> int:
    """Count mergeable ASCII pairs with a spare byte between neighboring pairs."""
    savings = 0
    pair_start = 0
    # The gap stops one merge from accounting for two credited pairs, even across scripts.
    while pair_start + 2 < len(run):
        pair = run[pair_start:pair_start + 2]
        if pair in _UNMERGED_LOWERCASE_PAIRS:
            pair_start += 1
            continue
        savings += 1
        pair_start += 3
    return savings


def _fallback_text_tokens(text: str) -> int:
    """Subtract supported merge savings from the UTF-8 byte estimate."""
    tokens = len(text.encode("utf-8", errors="surrogatepass"))
    for match in _FALLBACK_RUNS.finditer(text):
        run = match.group()
        start, end = match.span()
        match match.lastgroup:
            case "lowercase":
                # A contraction can put a tokenization boundary inside this run.
                if start == 0 or text[start - 1] != "'":
                    tokens -= _lowercase_merge_savings(run)
            case "digits":
                # Every one- to three-digit ASCII group is one token in the pinned vocabulary.
                # Restrict the discount to ASCII boundaries, where grouping cannot shift.
                if (
                    (start > 0 and not text[start - 1].isascii())
                    or (end < len(text) and not text[end].isascii())
                ):
                    continue
                size = len(run)
                tokens -= size - (size + 2) // 3
            case "hangul":
                for syllable in run:
                    prefix_index = (ord(syllable) - _HANGUL_START) // 64
                    tokens -= (_HANGUL_PREFIX_MERGES >> prefix_index) & 1
            case "kana":
                tokens -= len(run)
    return tokens


def text_tokens(text: str) -> int:
    """Count verified vocabulary tokens or estimate text with the offline heuristic."""
    encoding = _local_encoding()
    if encoding:
        return len(encoding.encode_ordinary(text))
    return _fallback_text_tokens(text)


def estimation_method() -> str:
    """Name the local method without claiming exact provider request tokenization."""
    return (
        "o200k_base_with_approximate_framing"
        if _local_encoding()
        else FALLBACK_ESTIMATION_METHOD
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
    parser.parse_args()
    try:
        install_vocabulary(settings.tokenizer_path)
    except VocabularyDownloadError as error:
        parser.exit(1, f"{error}\n")
    except OSError:
        parser.exit(1, "Could not save the vocabulary. Check write access and try again.\n")
    print("Local tokenizer vocabulary installed.")


if __name__ == "__main__":
    main()
