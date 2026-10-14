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


# 2G 内存的 ECS 上，全量 prepare（数千条机会的品牌判断+排序）单次要 30-60s。
# TTL 太短（曾 10s）会让每个翻页/刷新请求都撞上重算窗口，nginx 60s 超时 → 504"访问不通"。
# TTL=300s：翻页连续访问命中缓存；采集侧新数据最多延迟 5 分钟可见（采集周期本就 15-120 分钟）。
# MAX_ENTRIES=4：每个 entry 是数千行 dict，条目过多会直接把 2G 机器内存吃满。
TTL_SECONDS = 300
MAX_ENTRIES = 4


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
        _page_entries.clear()


# 整页 HTML 缓存：首页/搜索页的最终渲染输出。worker 全量 sync 会持续占满
# 2G ECS 的 CPU，实时查询被拖到 27-58s（nginx 60s 超时 → 504"访问不通"）。
# 页面缓存让同一参数的请求在 TTL 内直接秒回，完全不碰数据库。
PAGE_TTL_SECONDS = 60
PAGE_MAX_ENTRIES = 16
_page_entries: OrderedDict[Hashable, _Entry] = OrderedDict()


def get_page(key: Hashable) -> str | None:
    """Return a non-expired rendered page HTML and mark it most recently used."""
    now = monotonic()
    with _lock:
        entry = _page_entries.get(key)
        if entry is None:
            return None
        if now - entry.created_at >= PAGE_TTL_SECONDS:
            del _page_entries[key]
            return None
        _page_entries.move_to_end(key)
        return entry.value


def put_page(key: Hashable, html: str) -> None:
    """Store a fully rendered page; the caller owns the string."""
    with _lock:
        _page_entries[key] = _Entry(monotonic(), html)
        _page_entries.move_to_end(key)
        while len(_page_entries) > PAGE_MAX_ENTRIES:
            _page_entries.popitem(last=False)
