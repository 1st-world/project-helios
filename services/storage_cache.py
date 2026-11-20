"""Inspect and remove the single app-managed tokenizer cache file by revision."""

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from config import TOKENIZER_CACHE_PATH, Settings
from services.local_work import check_local_cancelled
from services.project_access import directory_identity, is_link, signature
from services.token_estimator import vocabulary_status


class TokenizerCacheUnavailable(OSError):
    """Report a cache location that cannot be inspected or removed safely."""


@dataclass(frozen=True)
class TokenizerCacheEntry:
    """Describe the file size and opaque revision of a removable vocabulary."""

    size_bytes: int
    revision: str


def _managed_path(settings: Settings) -> Path:
    """Allow cleanup only in the app-managed storage layout."""
    expected = settings.profiles_path.parent.absolute() / TOKENIZER_CACHE_PATH
    if settings.tokenizer_path.absolute().parts != expected.parts:
        raise TokenizerCacheUnavailable("The vocabulary is outside the managed cache.")
    return expected


def _inspect(path: Path) -> TokenizerCacheEntry | None:
    """Read one stable file signature without opening its contents."""
    try:
        directory = directory_identity(path.parent)
        info = path.lstat()
        after = directory_identity(path.parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TokenizerCacheUnavailable("The vocabulary cache cannot be inspected safely.") from error
    if directory != after or is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TokenizerCacheUnavailable("The vocabulary is not a stable, ordinary cache file.")
    identity = repr((directory, signature(info))).encode("ascii")
    return TokenizerCacheEntry(info.st_size, hashlib.sha256(identity).hexdigest())


def inspect_tokenizer_cache(settings: Settings) -> TokenizerCacheEntry | None:
    """Read the metadata needed to review vocabulary removal."""
    return _inspect(_managed_path(settings))


def remove_tokenizer_cache(settings: Settings, revision: str) -> bool:
    """Remove a reviewed vocabulary, rejecting a replacement and accepting an absent file."""
    path = _managed_path(settings)
    entry = _inspect(path)
    if entry is None:
        return True
    if entry.revision != revision:
        return False
    # Warm the old encoder for requests that bound this revision before their first estimate.
    vocabulary_status(path)
    entry = _inspect(path)
    if entry is None:
        return True
    if entry.revision != revision:
        return False
    check_local_cancelled()
    path.unlink(missing_ok=True)
    return True
