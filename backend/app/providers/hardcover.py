"""Hardcover.app metadata source (series identity sanity check).

Hardcover groups multi-volume light novels under one series, while AniList often
lists each volume as its own media entry (or uses a different series size).  This
provider performs a small, cached, rate-limited GraphQL lookup so NovelTrax can
cross-check the two sources and route disagreements to manual review instead of
silently merging.

Auth: ``HARDCOVER_API_KEY`` env or the masked ``hardcover_api_key`` setting.
The key is never logged. Cache: ``hardcover_series_cache`` (persistent) plus an
in-process copy; failures are not cached.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from app.parser import normalize_text, string_similarity

logger = logging.getLogger("noveltrax.providers.hardcover")

API_URL = "https://api.hardcover.app/v1/graphql"
CACHE_TTL_SECONDS = 7 * 24 * 3600
# Key limits: burst 10, 60 requests/minute total.  Stay below the burst window.
MAX_REQUESTS = 8
WINDOW_SECONDS = 60.0


class HardcoverRateLimiter:
    def __init__(self, max_requests: int = MAX_REQUESTS,
                 window: float = WINDOW_SECONDS):
        self.max_requests = max(1, int(max_requests))
        self.window = float(window)
        self._events: list = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._events = [ts for ts in self._events if now - ts < self.window]
                if len(self._events) < self.max_requests:
                    self._events.append(now)
                    return
                wait = self.window - (now - self._events[0]) + 0.05
            time.sleep(max(0.05, wait))


class HardcoverProvider:
    _limiter = HardcoverRateLimiter()
    _memory_cache: Dict[str, Dict[str, Any]] = {}
    _cache_lock = threading.Lock()
    _db: Any = None

    @classmethod
    def set_db(cls, db: Any) -> None:
        cls._db = db

    @classmethod
    def api_key(cls) -> str:
        value = str(os.getenv("HARDCOVER_API_KEY") or "").strip()
        if value:
            return value
        db = cls._db
        if db is not None:
            try:
                return str(db.get_setting("hardcover_api_key", "") or "").strip()
            except Exception:
                return ""
        return ""

    @classmethod
    def _cache_key(cls, title: str) -> str:
        return "series:" + normalize_text(title)

    @classmethod
    def _cache_get(cls, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with cls._cache_lock:
            entry = cls._memory_cache.get(key)
            if entry and now - float(entry.get("ts") or 0.0) < CACHE_TTL_SECONDS:
                return entry.get("value")
        db = cls._db
        if db is not None:
            try:
                row = db.get_hardcover_cache(key, max_age_seconds=CACHE_TTL_SECONDS)
            except Exception:
                row = None
            if row:
                with cls._cache_lock:
                    cls._memory_cache[key] = {"ts": now, "value": row}
                return row
        return None

    @classmethod
    def _cache_put(cls, key: str, value: Dict[str, Any]) -> None:
        now = time.time()
        with cls._cache_lock:
            cls._memory_cache[key] = {"ts": now, "value": value}
        db = cls._db
        if db is not None:
            try:
                db.set_hardcover_cache(key, value)
            except Exception as exc:
                logger.debug("Could not persist Hardcover cache entry: %s", exc)

    @classmethod
    def _graphql(cls, query: str, variables: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = cls.api_key()
        if not key:
            logger.debug("Hardcover lookups skipped: no API key configured")
            return None
        cls._limiter.acquire()
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            API_URL, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "NovelTrax/1.0 (+self-hosted metadata check)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            logger.warning("Hardcover HTTP %s (lookup skipped)", exc.code)
            return None
        except Exception as exc:
            logger.warning("Hardcover request failed: %s", type(exc).__name__)
            return None
        if payload.get("errors"):
            logger.warning("Hardcover GraphQL error: %s", str(payload["errors"])[:250])
            return None
        return payload.get("data") or {}

    @staticmethod
    def _query_variants(title: str) -> list:
        """Search queries for a title, base grouping first.

        AniList often names one volume ``Rascal Does Not Dream of a Sister Home
        Alone`` while Hardcover groups ``Rascal Does Not Dream (Light Novel)``.
        Querying the trailing ``of …`` subtitle scores below the similarity floor,
        so try a conservative base title (≥3 words) first, then the full title.
        """
        text = str(title or "").strip()
        if not text:
            return []
        variants = []
        trimmed = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
        for separator in (r"\s+of\s+", r"\s*:\s*"):
            parts = re.split(separator, trimmed, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2 and len(parts[0].split()) >= 3:
                variants.append(parts[0].strip(" -–—:,"))
                break
        variants.append(text)
        unique = []
        for value in variants:
            if value and normalize_text(value) not in {normalize_text(v) for v in unique}:
                unique.append(value)
        return unique

    @classmethod
    def search_series(cls, title: str, per_page: int = 5) -> Optional[Dict[str, Any]]:
        """Best matching Hardcover series for *title*, or ``None``.

        Queries are cached per input title; a head-prefix match (the volume title
        starts with the grouped series name) is accepted even when whole-string
        similarity is below the floor, because that is exactly the per-volume
        AniList case this check exists for.
        """
        query_title = str(title or "").strip()
        if not query_title:
            return None
        cache_key = cls._cache_key(query_title)
        cached = cls._cache_get(cache_key)
        if cached is not None:
            return cached or None
        wanted = normalize_text(query_title)
        for query in cls._query_variants(query_title):
            data = cls._graphql(
                'query($q:String!){ search(query:$q, query_type:"series", per_page:10)'
                '{ results } }',
                {"q": query},
            )
            if not data:
                continue
            results = ((data.get("search") or {}).get("results") or {}).get("hits") or []
            best: Optional[Dict[str, Any]] = None
            best_score = 0.0
            for hit in results:
                document = (hit or {}).get("document") or {}
                name = str(document.get("name") or "").strip()
                if not name:
                    continue
                candidate = normalize_text(name)
                candidate_tokens = [
                    token for token in candidate.split()
                    if token not in ("light", "novel", "ln")
                ]
                wanted_tokens = wanted.split()
                head_prefix = bool(candidate_tokens) and bool(wanted_tokens) and (
                    wanted_tokens[:len(candidate_tokens)] == candidate_tokens
                    or candidate_tokens[:len(wanted_tokens)] == wanted_tokens
                )
                score = 1.0 if candidate == wanted else max(
                    string_similarity(wanted, candidate),
                    0.95 if wanted and (wanted in candidate or candidate in wanted) else 0.0,
                    0.9 if head_prefix else 0.0,
                )
                if score > best_score:
                    best_score = score
                    best = {
                        "id": int(document.get("id") or 0) or None,
                        "name": name,
                        "books_count": int(document.get("books_count") or 0),
                        "primary_books_count": int(document.get("primary_books_count") or 0),
                        "slug": str(document.get("slug") or ""),
                        "author_name": str(document.get("author_name") or ""),
                        "score": round(score, 4),
                    }
            if best is None or best_score < 0.72:
                continue
            series = cls.series_details(best.get("id")) or best
            cls._cache_put(cache_key, series)
            return series
        cls._cache_put(cache_key, {})
        return None

    @classmethod
    def has_cached(cls, title: str) -> bool:
        """True when this title was already looked up (hit or miss)."""
        key = cls._cache_key(title)
        with cls._cache_lock:
            if key in cls._memory_cache:
                return True
        db = cls._db
        if db is not None:
            try:
                return db.get_hardcover_cache(key, max_age_seconds=CACHE_TTL_SECONDS) is not None
            except Exception:
                return False
        return False

    @classmethod
    def series_details(cls, series_id: Any) -> Optional[Dict[str, Any]]:
        """Authoritative series size/completion for one Hardcover series id."""
        try:
            sid = int(series_id)
        except (TypeError, ValueError):
            return None
        if sid <= 0:
            return None
        cache_key = f"series_id:{sid}"
        cached = cls._cache_get(cache_key)
        if cached is not None:
            return cached or None
        data = cls._graphql(
            'query($id:Int!){ series_by_pk(id:$id){ id name books_count '
            'primary_books_count is_completed state } }',
            {"id": sid},
        )
        row = (data or {}).get("series_by_pk") if data else None
        if not row:
            return None
        details = {
            "id": int(row.get("id") or 0),
            "name": str(row.get("name") or ""),
            "books_count": int(row.get("books_count") or 0),
            "primary_books_count": int(row.get("primary_books_count") or 0),
            "is_completed": bool(row.get("is_completed")),
            "state": str(row.get("state") or ""),
        }
        cls._cache_put(cache_key, details)
        return details

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._memory_cache.clear()
