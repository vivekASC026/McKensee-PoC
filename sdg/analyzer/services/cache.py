"""TTL cache — the replacement for @st.cache_data.

Streamlit's cache was per-session and keyed on positional args. This is
process-wide and keyed on a JSON digest of the arguments, so lists and dicts
work as keys without the tuple() gymnastics the Streamlit pages needed.

Bounded on purpose. The original store expired entries lazily on read and never
released them: an expired value stayed referenced, so the dict only ever grew.
A discovery walk caches a member frame per combination — tens of multi-megabyte
frames — and none were freed for the life of the process. Measured: 40 MB still
held after every entry had expired.

So the store now carries a byte budget and evicts, oldest-touched first, and an
expired entry is dropped rather than merely ignored. Anything too large for the
budget belongs on disk, which is what disk_cache is for.
"""

import hashlib
import json
import sys
import threading
import time
from functools import wraps

# What the process will hold in RAM for cached values. Deliberately well under
# the disk tier's 2 GB: memory is the scarce half, and a value that does not fit
# is not lost — the disk tier still has it.
MAX_MEMORY_BYTES = 512 * 1024 ** 2

# A single value bigger than this is never held in memory. It goes to disk and
# is read back per use, which costs a pickle load instead of a permanent
# resident. Sized so an ordinary member frame still lives in RAM.
MAX_ENTRY_BYTES = 384 * 1024 ** 2

_STORE: dict[str, "_Entry"] = {}
_LOCK = threading.Lock()
_bytes_held = 0

_evictions = 0
_expired_drops = 0


class _Entry:
    __slots__ = ("stored", "value", "size", "touched", "hits")

    def __init__(self, value, size: int):
        now = time.time()
        self.stored = now
        self.touched = now
        self.value = value
        self.size = size
        self.hits = 0


def _key(fn_name, args, kwargs) -> str:
    raw = json.dumps(
        {"fn": fn_name, "a": args, "k": kwargs, "data": _fingerprint()},
        sort_keys=True, default=str,
    )
    return hashlib.md5(raw.encode()).hexdigest()


def _fingerprint() -> str:
    """The same stamp of the source data the disk tier folds in.

    Both tiers need it. Fixing only the disk tier left a long-running process
    answering from memory with cluster assignments computed over a different
    number of rows -- the cache survived a reseed because nothing in the key
    said which rows it had been computed from.

    Imported lazily: the disk cache imports this module, and reaching back for
    the function at call time keeps that from being a cycle at import.
    """
    try:
        from sdg.analyzer.services.disk_cache import data_fingerprint

        return data_fingerprint()
    except Exception:
        return "unavailable"


def _sizeof(value) -> int:
    """Best-effort byte size of a cached value.

    sys.getsizeof reports the container, not the data, for a DataFrame — a
    hundred-megabyte frame measures as a few hundred bytes, which would make the
    budget meaningless. Pandas objects are asked directly, and the tuples the
    loaders return are summed member by member.
    """
    try:
        if hasattr(value, "memory_usage"):          # DataFrame / Series
            # Deep usage walks every string, which on the whole claims book is
            # two seconds spent measuring what was just loaded. A sample of
            # rows, scaled up, is close enough to budget memory with.
            rows = len(value)
            if rows > 20_000:
                sample = value.iloc[:: max(1, rows // 10_000)]
                usage = sample.memory_usage(deep=True)
                part = int(usage.sum()) if hasattr(usage, "sum") else int(usage)
                return int(part * rows / max(1, len(sample)))
            usage = value.memory_usage(deep=True)
            return int(usage.sum()) if hasattr(usage, "sum") else int(usage)
        if isinstance(value, (tuple, list)):
            return sum(_sizeof(v) for v in value)
        if isinstance(value, dict):
            return sum(_sizeof(v) for v in value.values())
        return sys.getsizeof(value)
    except Exception:
        # Never let measurement failure stop a cache write.
        return sys.getsizeof(value) if hasattr(value, "__sizeof__") else 0


def _drop_locked(key: str) -> None:
    global _bytes_held
    entry = _STORE.pop(key, None)
    if entry is not None:
        _bytes_held -= entry.size


def _evict_locked(headroom: int) -> None:
    """Make room, least-recently-touched first.

    Recency rather than insertion order: an entry read on every request is the
    one worth keeping, whatever its age.
    """
    global _bytes_held, _evictions
    if _bytes_held + headroom <= MAX_MEMORY_BYTES:
        return
    for key in sorted(_STORE, key=lambda k: _STORE[k].touched):
        _drop_locked(key)
        _evictions += 1
        if _bytes_held + headroom <= MAX_MEMORY_BYTES:
            return


def ttl_cache(seconds: int = 300):
    """Cache a function's return value for `seconds`."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            global _expired_drops, _bytes_held
            k = _key(fn.__qualname__, args, kwargs)
            now = time.time()
            with _LOCK:
                entry = _STORE.get(k)
                if entry is not None:
                    if now - entry.stored < seconds:
                        entry.touched = now
                        entry.hits += 1
                        return entry.value
                    # Expired: release it now rather than leaving it referenced.
                    _drop_locked(k)
                    _expired_drops += 1

            value = fn(*args, **kwargs)
            size = _sizeof(value)

            with _LOCK:
                # Too big to be worth a permanent resident. Returned to the
                # caller, simply not retained; the disk tier still holds it.
                if size <= MAX_ENTRY_BYTES:
                    _drop_locked(k)
                    _evict_locked(size)
                    _STORE[k] = _Entry(value, size)
                    _bytes_held += size
            return value

        def peek(*args, **kwargs):
            """(True, value) when memory holds a live entry, else (False, None)."""
            k = _key(fn.__qualname__, args, kwargs)
            with _LOCK:
                entry = _STORE.get(k)
                if entry is not None and time.time() - entry.stored < seconds:
                    entry.touched = time.time()
                    entry.hits += 1
                    return True, entry.value
            return False, None

        def keep(value, *args, **kwargs):
            """Hold a value found elsewhere -- on disk -- so the next call is free."""
            global _bytes_held
            k = _key(fn.__qualname__, args, kwargs)
            size = _sizeof(value)
            with _LOCK:
                if size <= MAX_ENTRY_BYTES:
                    _drop_locked(k)
                    _evict_locked(size)
                    _STORE[k] = _Entry(value, size)
                    _bytes_held += size

        wrapper.cache_clear = clear_all
        wrapper.peek = peek
        wrapper.keep = keep
        return wrapper
    return deco


def stats() -> dict:
    """What the memory tier is holding — the counterpart to disk_cache.stats()."""
    with _LOCK:
        return {
            "entries": len(_STORE),
            "bytes": _bytes_held,
            "megabytes": round(_bytes_held / 1024 ** 2, 1),
            "limit_megabytes": round(MAX_MEMORY_BYTES / 1024 ** 2),
            "evictions": _evictions,
            "expired_dropped": _expired_drops,
        }


def clear_all():
    global _bytes_held
    with _LOCK:
        _STORE.clear()
        _bytes_held = 0
