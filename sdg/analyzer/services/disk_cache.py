"""Two-tier cache: memory in front, disk behind.

Why disk at all — the in-memory TTL cache alone has three problems this
workload actually hits:

  1. It dies with the process. Every uvicorn restart re-runs every query, and
     with --reload that's every time a file is saved.
  2. It is per-worker. Run uvicorn with more than one worker and each has its
     own copy, so the hit rate falls with the worker count.
  3. It is unbounded in the way that matters — a few large member frames sit
     in RAM for the whole TTL whether or not anyone asks for them again.

Disk fixes all three at the cost of serialization time, which is far cheaper
than a round trip to SQL Server plus a pandas build.

Layout — one file per entry under cache/api/, plus a sidecar .meta with the
stored time so expiry doesn't depend on filesystem mtime:

    cache/api/<hash>.pkl     pickled payload
    cache/api/<hash>.meta    {"stored": epoch, "ttl": seconds, "key": "..."}

Pickle rather than parquet deliberately: payloads here are tuples of frames
plus dicts, not single tables, and pickle needs no extra dependency. If
pyarrow is present and payloads become single frames, parquet would be
smaller and faster to read.

Unpickling runs code, so every payload is signed (HMAC-SHA256) when written
and read back only when its signature matches. A .pkl file put in the cache
folder by anything else -- or an entry from before signing -- is a miss and is
dropped, never loaded. The key is SDG_CACHE_KEY when set, else a random key
kept in cache/cache.key beside (not inside) the entries; workers share it.
"""

import hashlib
import hmac
import json
import logging
import os
import pickle
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .cache import ttl_cache  # memory tier

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "api"
KEY_FILE = CACHE_DIR.parent / "cache.key"

log = logging.getLogger(__name__)

# Namespace -> its in-memory cache_clear, so clear_namespace flushes both tiers.
_MEMORY_CLEARERS: dict = {}
DEFAULT_TTL = 3600           # an hour: long enough to survive a restart cycle
MAX_CACHE_BYTES = 2 * 1024 ** 3   # 2 GB overall, oldest evicted first

# Per-namespace ceilings. Without these, one namespace starves the others:
# generated datasets are large and cheap to rebuild, discovery results are
# small and cost a 500-combination sweep. Global oldest-first eviction let the
# former push out the latter, so discovery kept missing and recomputing.
NAMESPACE_QUOTAS = {
    "dataset": 256 * 1024 ** 2,   # generated frames — only needed preview→export
    # A discovery walk writes one entry per combination — hundreds of member
    # frames in a single run. Uncapped, that alone exceeded the global ceiling
    # and evicted everything else, including the discovery result the walk had
    # just produced. Capped here, a walk can only ever displace its own inputs.
    "members.load": 512 * 1024 ** 2,
    "claims.load": 512 * 1024 ** 2,
    # One entry per distinct state of the patterns table. Every write produces a
    # new signature and so a new entry, and the superseded ones are dead the
    # moment they are written — a walk that upserts hundreds of patterns would
    # otherwise leave a trail of them. Capped small; they are cheap to rebuild.
    "patterns.all": 64 * 1024 ** 2,
    "patterns.catalog": 64 * 1024 ** 2,
}

# Namespaces global eviction must never touch. These are small and cost minutes
# to rebuild — a discovery result is the output of a 500-combination sweep, so
# evicting it to make room for the frames that produced it is exactly backwards.
# They still expire on TTL; they are simply never chosen as eviction victims.
PROTECTED = ("discovery.",)

# Above this an entry is skipped entirely — it is neither worth the pickle nor
# the space. The old ceiling was 4 MB and had the trade backwards: a value too
# big for disk was kept in memory instead, so the largest values became
# permanent residents that did not even survive a restart, while the smallest
# ones got the durable tier. Disk is the abundant half here (35 MB of 2 GB in
# use when this was measured), so large values now spill to it and the memory
# tier keeps a bounded working set. See api/cache.py.
MAX_DISK_ENTRY_BYTES = 128 * 1024 ** 2

# How long an entry lives is scaled by what it cost to produce. A flat hour
# suits a query; it does not suit a discovery walk, which is 10 KB of result
# bought with three minutes of clustering and was being discarded hourly. TTL
# becomes cost x this, floored at the caller's own ttl and capped below.
COST_TTL_MULTIPLIER = 1000

# No entry outlives this, however expensive or however often it is read. The
# ceiling is what keeps a warm entry from becoming a permanently stale one.
MAX_TTL = 7 * 86400


def ttl_for(base_ttl: int, cost_seconds: float) -> int:
    """Lifetime for something that took `cost_seconds` to compute."""
    if cost_seconds <= 0:
        return base_ttl
    return int(min(MAX_TTL, max(base_ttl, cost_seconds * COST_TTL_MULTIPLIER)))

# Sweeping the whole directory on every put costs two stat() calls per file and
# grows with the cache. Amortize it instead.
SWEEP_EVERY = 25

# Guards the ledgers below and the sweep counter. Re-entrant: eviction holds
# it while dropping entries, and _drop takes it too.
_LOCK = threading.RLock()
_puts_since_sweep = 0

# stem → namespace, so quota checks never re-read the sidecars.
_NS_INDEX: dict[str, str] = {}

# namespace → {stem: (mtime, size)}. Quota enforcement reads this instead of
# scanning the directory. Enforcing a quota by globbing and stat-ing every file
# on every write made a 500-entry discovery walk quadratic — 500 writes each
# scanning a growing directory, all holding the lock.
_NS_FILES: dict[str, dict[str, tuple[float, int]]] = {}
_NS_SCANNED = False


def _ensure_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


_KEY: bytes | None = None


def _signing_key() -> bytes:
    """The key payloads are signed with: SDG_CACHE_KEY, else cache/cache.key.

    The file is made once, whole, before it is visible (written to a temp
    name, then linked into place), so a second worker starting at the same
    moment reads the first one's key rather than a half-written file.
    """
    global _KEY
    if _KEY:
        return _KEY
    configured = os.getenv("SDG_CACHE_KEY", "").strip()
    if configured:
        _KEY = configured.encode()
        return _KEY
    try:
        key = KEY_FILE.read_bytes()
    except FileNotFoundError:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=KEY_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
            try:
                os.link(tmp, KEY_FILE)
            except FileExistsError:
                pass  # another worker made it first: use theirs
        finally:
            Path(tmp).unlink(missing_ok=True)
        key = KEY_FILE.read_bytes()
    if len(key) < 32:
        raise RuntimeError(f"{KEY_FILE} holds no usable cache key. Delete it (and the "
                           f"cache folder beside it) and the next run makes a new one.")
    _KEY = key
    return _KEY


class _SigningWriter:
    """A file that signs what is written to it, for pickle.dump."""

    def __init__(self, fh, key: bytes) -> None:
        self._fh = fh
        self.mac = hmac.new(key, digestmod=hashlib.sha256)

    def write(self, data) -> int:
        self.mac.update(data)
        return self._fh.write(data)


def _signature_of(fh, key: bytes) -> str:
    """The HMAC of an open file's whole content, read in chunks; rewound after."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        mac.update(chunk)
    fh.seek(0)
    return mac.hexdigest()


def _write_atomic(path: Path, text: str) -> None:
    """Write a small text file under a unique temp name, then rename it in."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_FINGERPRINT: tuple[float, str] | None = None
FINGERPRINT_TTL = 60.0
FINGERPRINT_RETRY = 10.0


def data_fingerprint() -> str:
    """A cheap stamp of the data the analyzer reads.

    Every key folds this in, so changing the source invalidates the cache
    instead of silently answering from it. Without it a reseeded fixture
    produced cluster assignments computed over a different number of rows --
    which surfaced as `Boolean index has wrong length` if you were lucky, and
    as quietly wrong patterns if you were not.

    Read from catalog metadata rather than by counting rows: sys.partitions
    knows the row count without touching the table, so this costs microseconds
    and can afford to be checked often.
    """
    global _FINGERPRINT
    now = time.time()
    if _FINGERPRINT and now - _FINGERPRINT[0] < FINGERPRINT_TTL:
        return _FINGERPRINT[1]

    stamp = "unknown"
    try:
        import pyodbc

        from sdg.analyzer.config import get_connection_string

        with pyodbc.connect(get_connection_string(), timeout=5) as connection:
            cursor = connection.cursor()
            from sdg.analyzer.config import SQL_SCHEMA

            # The store's own denorm tables: how many rows each holds, and when
            # each was last created or altered. A store rebuilt in another
            # shape -- the same rows in different columns -- is a different
            # store, which row counts alone would not say.
            cursor.execute("""
                SELECT t.name, SUM(p.rows), MAX(t.modify_date)
                FROM sys.partitions AS p
                JOIN sys.tables AS t ON t.object_id = p.object_id
                WHERE p.index_id IN (0, 1) AND SCHEMA_NAME(t.schema_id) = ?
                  AND t.name LIKE '%[_]denorm'
                GROUP BY t.name ORDER BY t.name
            """, SQL_SCHEMA)
            stamp = ";".join(f"{name}:{count}:{changed:%Y%m%d%H%M%S}"
                             for name, count, changed in cursor.fetchall())
            # The last refresh, which can leave every count as it was.
            cursor.execute(
                "SELECT CASE WHEN OBJECT_ID(?, 'U') IS NULL THEN NULL "
                "ELSE 1 END", f"{SQL_SCHEMA}.extract_log")
            if cursor.fetchone()[0]:
                cursor.execute(f"SELECT MAX(finished_at) FROM {SQL_SCHEMA}.extract_log")
                last = cursor.fetchone()[0]
                if last is not None:
                    stamp += f"|extracted:{last:%Y%m%d%H%M%S%f}"
    except Exception:
        # Not a constant: entries written now must not match entries written
        # in a later outage, after the data has changed. A stamp of this
        # moment keeps them apart, and it is kept only briefly, so the real
        # fingerprint is read again soon.
        log.warning("Could not read the data fingerprint; caching is keyed to "
                    "this moment until it can be read", exc_info=True)
        stamp = f"unavailable:{time.time_ns()}"
        failed = True
    else:
        failed = False

    try:
        # Rows read by another version of the loader are other rows.
        from sdg.analyzer.entity_loader import READ_VERSION

        stamp += f"|read:{READ_VERSION}"
    except Exception:
        pass

    try:
        # Patterns found on other features are other patterns.
        from sdg.analyzer.services.discovery_features import signature

        stamp += "|features:" + signature()
    except Exception:
        pass

    # A failed read is retried after FINGERPRINT_RETRY, not FINGERPRINT_TTL.
    _FINGERPRINT = (now - FINGERPRINT_TTL + FINGERPRINT_RETRY if failed else now, stamp)
    return stamp


def make_key(namespace: str, *parts: Any) -> str:
    raw = json.dumps(
        {"ns": namespace, "parts": parts, "data": data_fingerprint()},
        sort_keys=True, default=str,
    )
    return hashlib.md5(raw.encode()).hexdigest()


def _paths(key: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"{key}.pkl", CACHE_DIR / f"{key}.meta"


def get(key: str) -> Optional[Any]:
    """Return the cached payload, or None if absent or expired.

    Reading an entry extends its life. Something asked for repeatedly is worth
    keeping regardless of when it was built, which is the whole point of a
    cache; expiring it on a fixed schedule throws away exactly the entries in
    demand. The extension is bounded twice over — never past MAX_TTL from the
    first write, and the deadline is only rewritten once an entry is past half
    its life, so a hot key costs at most one small write per half-TTL rather
    than one per read.
    """
    blob, meta = _paths(key)
    if not blob.exists() or not meta.exists():
        return None
    try:
        info = json.loads(meta.read_text())
        now = time.time()
        ttl = info.get("ttl", DEFAULT_TTL)
        if now - info["stored"] > ttl:
            _drop(key)
            return None

        with blob.open("rb") as fh:
            # Checked on the same open file that is then loaded, so what was
            # verified is what is unpickled.
            signed = hmac.compare_digest(str(info.get("mac", "")),
                                         _signature_of(fh, _signing_key()))
            value = _unpack(pickle.load(fh)) if signed else None
        if not signed:
            # Dropped once the file is closed: Windows will not delete an open one.
            log.warning("Dropped cache entry %s: it is unsigned or its signature "
                        "does not match", key)
            _drop(key)
            return None

        first = info.get("first_stored", info["stored"])
        if now - info["stored"] > ttl / 2 and now - first < MAX_TTL:
            info["stored"] = now
            info["hits"] = int(info.get("hits", 0)) + 1
            try:
                _write_atomic(meta, json.dumps(info))
            except OSError:
                pass  # Extension is an optimization, never a failure.
        return value
    except Exception:
        # A corrupt or half-written entry is not worth crashing over.
        log.warning("Dropped unreadable cache entry %s", key, exc_info=True)
        _drop(key)
        return None


# Frames are stored with their repeated text as categories. The whole claims
# book pickles to 230 MB with plain strings -- over the entry limit, so it was
# written, discarded and read from SQL Server again on every analysis -- and
# to 73 MB this way, in a fiftieth of the time. The original types come back
# on read, so nothing that uses the frame can tell it was ever packed.
_PACKED = "_sdm_packed_columns"


def _pack(value: Any) -> Any:
    import pandas as pd

    if isinstance(value, pd.DataFrame):
        packed = {}
        out = value
        for column in value.columns:
            series = value[column]
            if series.dtype == object or str(series.dtype).startswith(("str", "string")):
                try:
                    if series.nunique(dropna=False) < 0.5 * max(1, len(series)):
                        if out is value:
                            out = value.copy()
                        out[column] = series.astype("category")
                        packed[column] = str(series.dtype)
                except TypeError:
                    continue  # unhashable values: leave the column as it is
        if packed:
            out.attrs = {**value.attrs, _PACKED: packed}
        return out
    if isinstance(value, tuple):
        return tuple(_pack(v) for v in value)
    if isinstance(value, list):
        return [_pack(v) for v in value]
    if isinstance(value, dict):
        return {k: _pack(v) for k, v in value.items()}
    return value


def _unpack(value: Any) -> Any:
    import pandas as pd

    if isinstance(value, pd.DataFrame):
        packed = value.attrs.get(_PACKED)
        if packed:
            for column, dtype in packed.items():
                value[column] = value[column].astype(dtype)
            value.attrs = {k: v for k, v in value.attrs.items() if k != _PACKED}
        return value
    if isinstance(value, tuple):
        return tuple(_unpack(v) for v in value)
    if isinstance(value, list):
        return [_unpack(v) for v in value]
    if isinstance(value, dict):
        return {k: _unpack(v) for k, v in value.items()}
    return value


def put(key: str, value: Any, ttl: int = DEFAULT_TTL, label: str = "",
        namespace: str = "", cost_seconds: float = 0.0) -> None:
    _ensure_dir()
    blob, meta = _paths(key)
    resolved_ns = namespace or label.split("(")[0]
    tmp: Optional[Path] = None
    try:
        # Write to a temp name then rename, so a reader never sees a partial
        # file even if the process dies mid-write. The name is this writer's
        # own: two workers writing the same key at once each finish their own
        # file, and the last rename wins whole.
        fd, tmp_name = tempfile.mkstemp(dir=CACHE_DIR, prefix=key + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as fh:
            writer = _SigningWriter(fh, _signing_key())
            pickle.dump(_pack(value), writer, protocol=pickle.HIGHEST_PROTOCOL)

        size = tmp.stat().st_size
        if size > MAX_DISK_ENTRY_BYTES:
            # Too big to be worth persisting. The memory tier still holds it,
            # so it is reused within this run; it simply does not survive a
            # restart, which is the right trade for a frame this size.
            tmp.unlink(missing_ok=True)
            return

        os.replace(tmp, blob)
        now = time.time()
        _write_atomic(meta, json.dumps({
            "stored": now, "first_stored": now,
            "ttl": ttl_for(ttl, cost_seconds), "base_ttl": ttl,
            "cost_seconds": round(cost_seconds, 3), "hits": 0,
            "key": label, "ns": resolved_ns, "mac": writer.mac.hexdigest(),
        }))
        with _LOCK:
            _NS_INDEX[key] = resolved_ns
            _NS_FILES.setdefault(resolved_ns, {})[key] = (time.time(), size)
    except Exception:
        # The value is still returned to the caller and held in memory; only
        # the disk copy is lost. Said, so a cache that never persists is seen.
        log.warning("Could not write cache entry %s in %s", key, resolved_ns, exc_info=True)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return

    global _puts_since_sweep
    with _LOCK:
        _puts_since_sweep += 1
        sweep = _puts_since_sweep >= SWEEP_EVERY
        if sweep:
            _puts_since_sweep = 0
    if sweep:
        _evict_if_needed()
    elif resolved_ns in NAMESPACE_QUOTAS:
        # O(1) against the in-memory ledger — no directory scan.
        _enforce_quota(resolved_ns)


def _drop(key: str) -> None:
    with _LOCK:
        ns = _NS_INDEX.pop(key, None)
        if ns is not None:
            _NS_FILES.get(ns, {}).pop(key, None)
        else:
            for entries in _NS_FILES.values():
                entries.pop(key, None)
    for p in _paths(key):
        p.unlink(missing_ok=True)


def _namespace_of(key: str) -> str:
    """The namespace an entry belongs to.

    Read from an in-memory index rather than the sidecar. Reading JSON per file
    on every quota check meant a few hundred cached entries turned each write
    into a few hundred file reads while holding the cache lock — which is what
    made unrelated work appear to hang.
    """
    cached = _NS_INDEX.get(key)
    if cached is not None:
        return cached
    _, meta = _paths(key)
    try:
        ns = str(json.loads(meta.read_text()).get("ns", ""))
    except Exception:
        ns = ""
    with _LOCK:
        _NS_INDEX[key] = ns
    return ns


def _is_protected(namespace: str) -> bool:
    return any(namespace.startswith(p) for p in PROTECTED)


def _scan_once() -> None:
    """Build the size ledger from disk, one time, at first use.

    Everything after this is maintained incrementally by put() and _drop(), so
    no quota check ever touches the filesystem again.
    """
    global _NS_SCANNED
    if _NS_SCANNED or not CACHE_DIR.exists():
        _NS_SCANNED = True
        return
    for blob in CACHE_DIR.glob("*.pkl"):
        try:
            stat = blob.stat()
        except OSError:
            continue
        ns = _namespace_of(blob.stem)
        _NS_FILES.setdefault(ns, {})[blob.stem] = (stat.st_mtime, stat.st_size)
    # Temp files a process died holding: each writer's has its own name, so
    # nothing overwrites them. An hour old is long past any write in flight.
    for tmp in CACHE_DIR.glob("*.tmp"):
        try:
            if time.time() - tmp.stat().st_mtime > 3600:
                tmp.unlink()
        except OSError:
            continue
    _NS_SCANNED = True


def _enforce_quota(namespace: str) -> int:
    """Hold one namespace under its ceiling, oldest first.

    Reads the in-memory ledger rather than the directory. The previous version
    globbed and stat'd every cached file on every write, so a discovery walk —
    hundreds of writes into a growing cache — spent most of its time here,
    holding the lock the whole way.
    """
    limit = NAMESPACE_QUOTAS.get(namespace)
    if not limit:
        return 0

    with _LOCK:
        _scan_once()
        entries = _NS_FILES.get(namespace, {})
        total = sum(size for _, size in entries.values())
        if total <= limit:
            return 0

        oldest = sorted(entries.items(), key=lambda kv: kv[1][0])
        removed = 0
        for stem, (_, size) in oldest:
            if total <= limit:
                break
            total -= size
            _drop(stem)
            removed += 1
        return removed


def _evict_if_needed() -> None:
    """Quotas first, then the global cap, oldest first in both cases."""
    for ns in list(NAMESPACE_QUOTAS):
        _enforce_quota(ns)

    with _LOCK:
        _scan_once()
        total = sum(size
                    for entries in _NS_FILES.values()
                    for _, size in entries.values())
        if total <= MAX_CACHE_BYTES:
            return

        # Protected entries still count toward the total — they occupy real
        # space — but are never the victim.
        evictable = sorted(
            ((stem, meta) for ns, entries in _NS_FILES.items()
             if not _is_protected(ns)
             for stem, meta in entries.items()),
            key=lambda kv: kv[1][0],
        )
        for stem, (_, size) in evictable:
            if total <= MAX_CACHE_BYTES:
                break
            total -= size
            _drop(stem)


def clear() -> dict:
    """Wipe the disk tier. Returns what was removed."""
    with _LOCK:
        if not CACHE_DIR.exists():
            return {"removed_files": 0, "freed_bytes": 0}
        files = list(CACHE_DIR.glob("*"))
        freed = sum(p.stat().st_size for p in files if p.is_file())
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        return {"removed_files": len(files), "freed_bytes": freed}


def stats() -> dict:
    if not CACHE_DIR.exists():
        return {"entries": 0, "bytes": 0, "dir": str(CACHE_DIR)}
    blobs = list(CACHE_DIR.glob("*.pkl"))
    return {
        "entries": len(blobs),
        "bytes": sum(p.stat().st_size for p in blobs),
        "dir": str(CACHE_DIR),
    }


def clear_namespace(namespace: str) -> int:
    """Drop every entry for this namespace from BOTH tiers.

    Disk pickles are matched by label prefix; the in-memory memoization is
    flushed via the registered clearer. Clearing only one tier is the bug this
    guards against — a stale value in the other tier is served immediately.
    """
    mem = _MEMORY_CLEARERS.get(namespace)
    if mem:
        mem()
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for meta in list(CACHE_DIR.glob("*.meta")):
        try:
            info = json.loads(meta.read_text())
        except Exception:
            continue
        if str(info.get("key", "")).startswith(namespace):
            _drop(meta.stem)
            removed += 1
    return removed


def two_tier(namespace: str, ttl: int = DEFAULT_TTL):
    """Memory first, then disk, then compute.

    A hit in memory costs nothing. A hit on disk costs a pickle load — far
    less than the query it replaces — and is then promoted into memory. Only a
    miss on both reaches SQL Server.

    Pass refresh=True to force a recompute and overwrite both tiers — used when
    the underlying data has changed and the cached answer is stale.
    """
    def deco(fn: Callable):
        memo = ttl_cache(ttl)(fn)

        def wrapper(*args, refresh: bool = False, **kwargs):
            key = make_key(namespace, args, sorted(kwargs.items()))
            if not refresh:
                # Memory first, as the docstring always said. Reading the disk
                # before it meant unpickling on every call even when the value
                # was already in memory.
                held, value = memo.peek(*args, **kwargs)
                if held:
                    return value
                hit = get(key)
                if hit is not None:
                    memo.keep(hit, *args, **kwargs)
                    return hit
            started = time.perf_counter()
            if refresh:
                value = fn(*args, **kwargs)
                # Both tiers, as the docstring says: without this the next
                # ordinary call answers from memory with the value just replaced.
                memo.keep(value, *args, **kwargs)
            else:
                value = memo(*args, **kwargs)
            cost = time.perf_counter() - started
            put(key, value, ttl=ttl, label=f"{namespace}{args}",
                namespace=namespace, cost_seconds=cost)
            return value

        _MEMORY_CLEARERS[namespace] = getattr(memo, "cache_clear", lambda: None)

        def clear():
            getattr(memo, "cache_clear", lambda: None)()
            clear_namespace(namespace)

        wrapper.cache_clear = clear
        wrapper.namespace = namespace
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        return wrapper
    return deco
