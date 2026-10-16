"""Read local byte streams without allocating the entire admission budget up front."""

from io import BytesIO
from typing import BinaryIO

from services.local_work import check_local_cancelled


def read_bounded(source: BinaryIO, max_bytes: int) -> bytes:
    """Read through EOF or one byte beyond the budget so callers can reject overflow."""
    if max_bytes < 0:
        raise ValueError("The read budget must be nonnegative.")
    remaining = max_bytes + 1
    with BytesIO() as result:
        while remaining:
            check_local_cancelled()
            chunk = source.read(min(64 * 1024, remaining))
            check_local_cancelled()
            if not chunk:
                break
            result.write(chunk)
            remaining -= len(chunk)
        return result.getvalue()
