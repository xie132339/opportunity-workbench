"""Short lived cache for expensive, page independent search preparation.

The product-search admission path needs to examine the complete filtered
result set before it can apply its business rules and sort it.  A page switch
must not repeat that work.  This process-local cache deliberately has a very
short lifetime: it improves contiguous paging while keeping official source
updates visible without a worker-to-web invalidation channel.
"""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Any, Hashable


TTL_SECONDS = 10
MAX_ENTRIES = 12


@dataclass(frozen=True)
class _Entry:
    created_at: float
    value: dict[str, Any]


_entries: OrderedDict[Hashable, _Entry] = OrderedDict()
_lock = RLock()


def get(key: Hashable) -> dict[str, Any] | None:
    """Return a non-expired prepared result and mark it most recently used."""
    now = monotonic()
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        if now - entry.created_at >= TTL_SECONDS:
            del _entries[key]
            return None
        _entries.move_to_end(key)
        return entry.value


def put(key: Hashable, value: dict[str, Any]) -> None:
    """Store a fully prepared search result; the caller owns contained values."""
    with _lock:
        _entries[key] = _Entry(monotonic(), value)
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)


def clear() -> None:
    """Clear cached results, used by tests and future in-process mutations."""
    with _lock:
        _entries.clear()
