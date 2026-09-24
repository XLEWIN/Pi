"""Two-level cache: L1 in-memory + L3 SQLite file_id store (via bot.database).

Also holds a short-TTL resolve cache so repeat URLs skip yt-dlp extraction.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from bot.database import db

from .metrics import metrics

# L1: cache_key -> (file_id, media_kind, ts)
_L1_MAX = 512
_L1: "OrderedDict[str, Tuple[str, str, float]]" = OrderedDict()
_L1_LOCK = Lock()

# L1 resolve cache: canonical URL key -> (ResolvedPost, ts).
# Short TTL — CDN URLs expire; file_id path only needs media_id.
_RESOLVE_TTL = 300.0
_RESOLVE_MAX = 128
_RESOLVE: "OrderedDict[str, Tuple[Any, float]]" = OrderedDict()
_RESOLVE_LOCK = Lock()


def _l1_get(key: str) -> Optional[Tuple[str, str]]:
    with _L1_LOCK:
        item = _L1.get(key)
        if not item:
            return None
        _L1.move_to_end(key)
        return item[0], item[1]


def _l1_put(key: str, file_id: str, kind: str) -> None:
    with _L1_LOCK:
        _L1[key] = (file_id, kind, time.time())
        _L1.move_to_end(key)
        while len(_L1) > _L1_MAX:
            _L1.popitem(last=False)


def get_file_ids(keys: List[str]) -> Dict[str, Tuple[str, str]]:
    """Return {key: (file_id, kind)} for keys present in L1 or L3."""
    out: Dict[str, Tuple[str, str]] = {}
    missing: List[str] = []
    for k in keys:
        hit = _l1_get(k)
        if hit:
            out[k] = hit
            metrics.bump("cache_hits")
        else:
            missing.append(k)
    if missing:
        try:
            rows = db.ig_get_file_ids(missing)
        except Exception:
            rows = {}
        for k, (file_id, kind) in rows.items():
            out[k] = (file_id, kind)
            _l1_put(k, file_id, kind)
            metrics.bump("cache_hits")
    metrics.bump("cache_misses", n=max(0, len(keys) - len(out)))
    return out


def put_file_id(key: str, file_id: str, kind: str, source_url: str = "") -> None:
    _l1_put(key, file_id, kind)
    try:
        db.ig_put_file_id(key, file_id, kind, source_url)
    except Exception:
        # L3 failure must not break delivery — L1 still serves this process.
        pass


def get_resolved(url_key: str) -> Optional[Any]:
    """Return a cached ResolvedPost for *url_key* if fresh (else None)."""
    with _RESOLVE_LOCK:
        item = _RESOLVE.get(url_key)
        if not item:
            return None
        post, ts = item
        if time.time() - ts > _RESOLVE_TTL:
            del _RESOLVE[url_key]
            return None
        _RESOLVE.move_to_end(url_key)
        return post


def put_resolved(url_key: str, post: Any) -> None:
    with _RESOLVE_LOCK:
        _RESOLVE[url_key] = (post, time.time())
        _RESOLVE.move_to_end(url_key)
        while len(_RESOLVE) > _RESOLVE_MAX:
            _RESOLVE.popitem(last=False)


def invalidate_resolved(url_key: str) -> None:
    with _RESOLVE_LOCK:
        _RESOLVE.pop(url_key, None)


def clear_cache() -> int:
    with _L1_LOCK:
        _L1.clear()
    with _RESOLVE_LOCK:
        _RESOLVE.clear()
    try:
        return db.ig_clear_file_cache()
    except Exception:
        return 0


def cache_stats() -> Dict[str, int]:
    try:
        db_stats = db.ig_cache_stats()
    except Exception:
        db_stats = {"rows": 0, "hits": 0}
    with _L1_LOCK:
        l1 = len(_L1)
    with _RESOLVE_LOCK:
        rsl = len(_RESOLVE)
    return {
        "l1": l1,
        "resolve": rsl,
        "rows": int(db_stats.get("rows", 0)),
        "hits": int(db_stats.get("hits", 0)),
    }
