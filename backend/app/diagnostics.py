"""Read-only storage, network and series-attribution diagnostics for NovelTrax.

CONTRACT_V2_REBIND §6.2.  The module only depends on the standard library plus the
project's own stdlib-safe helpers (``app.security``, ``app.parser``,
``app.series_identity``, ``app.calibre.scanner``); those imports are guarded so the
module stays importable in the bundled stdlib-only test interpreter (no
httpx/jinja2/FastAPI there).

Every public function is defensive: ``storage_health`` / ``network_diagnostics`` /
``series_attribution_audit`` never raise for normal failures and degrade to a dict
with the failing section marked ``ok: false`` (or ``error``) instead.
"""

import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("noveltrax.diagnostics")

try:  # SSRF-guarded HTTP client (stdlib fallback lives inside app.security).
    from app.security import safe_http_get
except Exception:  # pragma: no cover - security always ships with the app
    safe_http_get = None

try:
    from app.calibre.scanner import CalibreScanner
except Exception:  # pragma: no cover - guarded for the stdlib-only interpreter
    CalibreScanner = None

try:
    from app.parser import clean_feed_title, extract_series_and_volume
except Exception:  # pragma: no cover
    clean_feed_title = None
    extract_series_and_volume = None

try:
    from app.series_identity import normalize_series, resolve_series_title
except Exception:  # pragma: no cover
    normalize_series = None
    resolve_series_title = None


# Provider reachability probes: AniList GraphQL, Kitsu API, Nyaa RSS, J-Novels.
NETWORK_TARGETS = (
    ("AniList", "https://graphql.anilist.co"),
    ("Kitsu", "https://kitsu.io/api/edge/manga?page[limit]=1"),
    ("Nyaa", "https://nyaa.si/?page=rss&c=3_1"),
    ("J-Novels", "https://jnovels.com/"),
)

MAX_NETWORK_TIMEOUT = 5.0
MAX_NETWORK_BYTES = 64 * 1024


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_bytes(n: Any) -> str:
    """Human-readable byte size (e.g. ``1536 -> '1.5 KB'``); never raises."""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "0 B"
    if value != value or value in (float("inf"), float("-inf")):
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while abs(value) >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d B" % int(value)
    return "%.1f %s" % (value, units[index])


def _dir_health(path: Any, label: str) -> Dict[str, Any]:
    """Existence / writability / free-space check for one directory; never raises."""
    info: Dict[str, Any] = {
        "ok": False,
        "path": str(path or ""),
        "exists": False,
        "writable": False,
        "free_bytes": 0,
        "total_bytes": 0,
        "free_human": "0 B",
        "error": "",
    }
    probe_path = None
    try:
        if not path:
            raise ValueError("%s is not configured" % label)
        info["exists"] = os.path.isdir(str(path))
        if not info["exists"]:
            raise FileNotFoundError("%s does not exist" % info["path"])
        info["writable"] = bool(os.access(str(path), os.W_OK))
        fd, probe_path = tempfile.mkstemp(prefix=".noveltrax-probe-", dir=str(path))
        os.close(fd)
        info["writable"] = True
        usage = shutil.disk_usage(str(path))
        info["free_bytes"] = int(usage.free)
        info["total_bytes"] = int(usage.total)
        info["free_human"] = format_bytes(info["free_bytes"])
        info["ok"] = True
    except Exception as exc:
        info["error"] = str(exc)[:300] or type(exc).__name__
    finally:
        if probe_path:
            try:
                os.remove(probe_path)
            except OSError:
                pass
    return info


def _calibre_health(calibre_path: Any) -> Dict[str, Any]:
    """Calibre library availability/books/series; never raises."""
    info: Dict[str, Any] = {
        "ok": False,
        "path": str(calibre_path or ""),
        "available": False,
        "books": 0,
        "series": 0,
        "error": "",
    }
    try:
        if CalibreScanner is None:
            raise RuntimeError("Calibre scanner is not available")
        scanner = CalibreScanner(str(calibre_path or ""))
        info["path"] = str(getattr(scanner, "db_path", "") or calibre_path or "")
        info["available"] = bool(scanner.is_available())
        if info["available"]:
            summary = scanner.get_library_summary() or {}
            info["books"] = int(summary.get("total_books") or 0)
            info["series"] = int(summary.get("total_series") or 0)
        info["ok"] = info["available"]
    except Exception as exc:
        info["error"] = str(exc)[:300] or type(exc).__name__
    return info


def _database_health(db: Any, data_dir: Any) -> Dict[str, Any]:
    """Database file/WAL sizes and core row counts; never raises."""
    info: Dict[str, Any] = {
        "ok": False,
        "path": "",
        "size_bytes": 0,
        "size_human": "0 B",
        "wal_bytes": 0,
        "wal_human": "0 B",
        "row_counts": {},
        "error": "",
    }
    try:
        db_path = str(getattr(db, "db_path", "") or "")
        if not db_path and data_dir:
            db_path = os.path.join(str(data_dir), "noveltrax.db")
        info["path"] = db_path
        if db_path and os.path.isfile(db_path):
            info["size_bytes"] = int(os.path.getsize(db_path))
        wal_path = (db_path + "-wal") if db_path else ""
        if wal_path and os.path.isfile(wal_path):
            info["wal_bytes"] = int(os.path.getsize(wal_path))
        info["size_human"] = format_bytes(info["size_bytes"])
        info["wal_human"] = format_bytes(info["wal_bytes"])

        counts: Dict[str, int] = {}
        if db is not None and db_path:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                for table in ("releases", "series_tracking", "activity_logs"):
                    try:
                        cursor.execute("SELECT count(*) FROM %s;" % table)
                        counts[table] = int(cursor.fetchone()[0])
                    except Exception:
                        counts[table] = 0
                try:
                    cursor.execute("SELECT count(*) FROM releases WHERE status = 'NEW';")
                    counts["releases_new"] = int(cursor.fetchone()[0])
                except Exception:
                    counts["releases_new"] = 0
        info["row_counts"] = counts
        info["ok"] = True
    except Exception as exc:
        info["error"] = str(exc)[:300] or type(exc).__name__
    return info


def storage_health(db: Any, calibre_path: str, data_dir: str, downloads_dir: str) -> dict:
    """Aggregate Calibre/directory/database health for the Settings diagnostics card."""
    try:
        calibre = _calibre_health(calibre_path)
        data = _dir_health(data_dir, "data_dir")
        downloads = _dir_health(downloads_dir, "downloads_dir")
        database = _database_health(db, data_dir)
        overall_ok = bool(
            calibre.get("ok") and data.get("ok") and downloads.get("ok") and database.get("ok")
        )
        return {
            "status": "success" if overall_ok else "degraded",
            "checked_at": _utc_iso(),
            "overall_ok": overall_ok,
            "calibre": calibre,
            "data_dir": data,
            "downloads_dir": downloads,
            "database": database,
        }
    except Exception as exc:  # final containment: this endpoint must never raise
        return {
            "status": "degraded",
            "checked_at": _utc_iso(),
            "overall_ok": False,
            "calibre": {"ok": False, "error": str(exc)[:300]},
            "data_dir": {"ok": False, "error": str(exc)[:300]},
            "downloads_dir": {"ok": False, "error": str(exc)[:300]},
            "database": {"ok": False, "error": str(exc)[:300]},
        }


def network_diagnostics(timeout: float = 5.0) -> dict:
    """Probe provider reachability with the SSRF-guarded HTTP client; never raises."""
    try:
        capped = float(timeout if timeout is not None else MAX_NETWORK_TIMEOUT)
    except (TypeError, ValueError):
        capped = MAX_NETWORK_TIMEOUT
    capped = max(0.1, min(capped, MAX_NETWORK_TIMEOUT))

    results: List[Dict[str, Any]] = []
    for name, url in NETWORK_TARGETS:
        started = time.monotonic()
        entry: Dict[str, Any] = {
            "name": name,
            "url": url,
            "ok": False,
            "status_code": None,
            "latency_ms": 0,
            "error": "",
        }
        try:
            if safe_http_get is None:
                raise RuntimeError("HTTP client is not available")
            response = safe_http_get(url, timeout=capped, max_bytes=MAX_NETWORK_BYTES)
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                entry["status_code"] = int(status_code)
            # A 4xx still proves the host is reachable; only 5xx counts as a failure.
            entry["ok"] = entry["status_code"] is not None and entry["status_code"] < 500
            if not entry["ok"] and not entry["error"]:
                entry["error"] = "HTTP %s" % entry["status_code"]
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc)[:300] or type(exc).__name__
        finally:
            entry["latency_ms"] = int(round((time.monotonic() - started) * 1000))
        results.append(entry)

    return {
        "checked_at": _utc_iso(),
        "timeout_seconds": capped,
        "ok": all(entry["ok"] for entry in results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Series attribution audit (read-only)
# ---------------------------------------------------------------------------
def _norm_series(name: Any) -> str:
    if normalize_series is not None:
        try:
            return normalize_series(str(name or ""))
        except Exception:
            pass
    return " ".join(str(name or "").lower().split())


def _attribution_release_rows(db: Any, scan_limit: int) -> List[Dict[str, Any]]:
    """Release rows for the audit (prefers owner A's dedicated attribution query)."""
    getter = getattr(db, "get_series_attribution_rows", None)
    if callable(getter):
        for call in (
            lambda: getter(limit=scan_limit),
            lambda: getter(scan_limit=scan_limit),
            lambda: getter(scan_limit),
            lambda: getter(),
        ):
            try:
                rows = call()
            except TypeError:
                continue
            except Exception as exc:
                logger.debug("Series attribution row query failed: %s", exc)
                return []
            return [row for row in (rows or []) if isinstance(row, dict)]
    fallback = getattr(db, "get_releases", None)
    if callable(fallback):
        try:
            rows = fallback(limit=scan_limit, include_ignored=False)
        except Exception as exc:
            logger.debug("Series attribution fallback query failed: %s", exc)
            return []
        return [row for row in (rows or []) if isinstance(row, dict)]
    return []


#: Tokens that, when they are the ONLY thing following the stored series name in a
#: release title, cannot change the attribution (volume/format noise).  Anything else
#: (e.g. "short story collection", a spin-off suffix, publisher words) forces the
#: expensive parser path so the audit cannot miss a real drift.
_ATTRIBUTION_SUFFIX_NOISE_TOKENS = frozenset({
    "vol", "volume", "vols", "v", "book", "books", "part", "parts",
    "epub", "pdf", "mobi", "azw3", "azw", "fb2", "txt", "html",
    "light", "novel", "novels", "ln", "wn", "complete", "completed",
    "series", "edition", "digital", "official", "retail", "rip", "batch",
    "pack", "box", "set", "omnibus", "all", "the", "a", "of",
})


def _attribution_title_noise_only(title_norm: str, stored_norm: str) -> bool:
    """True when the stored series spelling is present and only volume/format noise remains.

    Used as the audit's cheap fast path: a release title that is just
    `<stored series> Vol N` / format tags cannot have a different parsed series, so the
    parser/resolver can be skipped.  A suffix like "Short Story Collection" is not noise
    and forces the full check.
    """
    if not title_norm or not stored_norm or stored_norm not in title_norm:
        return False
    remainder = title_norm.replace(stored_norm, " ", 1)
    for token in remainder.split():
        if token.isdigit():
            continue
        if token in _ATTRIBUTION_SUFFIX_NOISE_TOKENS:
            continue
        return False
    return True


def _parsed_release_series(title: Any) -> str:
    """Best-effort series spelling derived from a release title."""
    raw_title = str(title or "").strip()
    if not raw_title or extract_series_and_volume is None:
        return raw_title
    candidates = [raw_title]
    if clean_feed_title is not None:
        try:
            cleaned = str(clean_feed_title(raw_title) or "").strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
        except Exception:
            pass
    for candidate in candidates:
        try:
            parsed = extract_series_and_volume(candidate) or {}
        except Exception:
            continue
        series_name = str(parsed.get("series_name") or "").strip()
        if series_name:
            return series_name
    return candidates[-1]


def _stored_calibre_series_id(stored_name: Any, series_rows: List[Dict[str, Any]]) -> Optional[int]:
    target = _norm_series(stored_name)
    if not target:
        return None
    for row in series_rows:
        if not isinstance(row, dict):
            continue
        if _norm_series(row.get("name")) != target:
            continue
        try:
            calibre_id = row.get("calibre_series_id")
            return int(calibre_id) if calibre_id not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return None


#: Hard wall-clock budget for one attribution audit request.  The audit is a read-only
#: diagnostic: it must return quickly enough for the UI (and any reverse proxy in front of
#: the app) instead of scanning an unbounded release table and timing out as an HTML error
#: page.  The response marks ``truncated`` when the budget was hit so the operator knows to
#: narrow the audit (e.g. lower the scan limit or re-run after the library settles).
ATTRIBUTION_TIME_BUDGET_SECONDS = 8.0


def series_attribution_audit(db: Any, limit: int = 200, scan_limit: int = 50000,
                            time_budget: Optional[float] = None) -> dict:
    """Read-only audit: releases whose stored series disagrees with the identity resolver.

    Performance: ``resolve_series_title`` builds its alias/row lookups from the tracked
    series list on every call, so resolving every release independently is O(releases x
    series) and can take long enough for a reverse proxy to time out (the browser then
    receives an HTML gateway page instead of JSON).  The audit therefore:
    * precomputes the stored-name -> Calibre-id map once;
    * memoises resolver results per normalised parsed-series spelling (releases of one
      series share the same resolution);
    * enforces a hard time budget (default 8 s) and reports ``truncated`` when it is hit,
      so the request always returns JSON promptly.
    """
    started = time.monotonic()
    result: Dict[str, Any] = {
        "checked": 0, "count": 0, "items": [], "error": "",
        "truncated": False, "elapsed_ms": 0,
    }
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 200
    cap = max(1, min(cap, 1000))
    try:
        scan_cap = int(scan_limit)
    except (TypeError, ValueError):
        scan_cap = 50000
    scan_cap = max(1, min(scan_cap, 500000))
    try:
        budget = float(time_budget) if time_budget is not None else ATTRIBUTION_TIME_BUDGET_SECONDS
    except (TypeError, ValueError):
        budget = ATTRIBUTION_TIME_BUDGET_SECONDS
    budget = max(0.25, min(budget, 60.0))

    try:
        series_rows = db.get_series_identity_rows() or []
    except Exception as exc:
        logger.debug("Series identity rows unavailable for attribution audit: %s", exc)
        series_rows = []
    try:
        aliases = db.get_series_alias_overrides() or []
    except Exception as exc:
        logger.debug("Series alias overrides unavailable for attribution audit: %s", exc)
        aliases = []

    # One pass over the tracked rows: stored-name -> Calibre id.  The old code looped the
    # whole series list for every release.
    stored_calibre_by_name: Dict[str, Optional[int]] = {}
    for row in series_rows:
        if not isinstance(row, dict):
            continue
        norm = _norm_series(row.get("name"))
        if not norm or norm in stored_calibre_by_name:
            continue
        calibre_id = row.get("calibre_series_id")
        try:
            stored_calibre_by_name[norm] = (
                int(calibre_id) if calibre_id not in (None, "") else None
            )
        except (TypeError, ValueError):
            stored_calibre_by_name[norm] = None

    try:
        rows = _attribution_release_rows(db, scan_cap)
    except Exception as exc:
        result["error"] = str(exc)[:300]
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    deadline = started + budget
    resolution_cache: Dict[str, Any] = {}
    items: List[Dict[str, Any]] = []
    for row in rows:
        if time.monotonic() >= deadline:
            result["truncated"] = True
            break
        result["checked"] += 1
        status = str(row.get("status") or "").strip().upper()
        if status in ("IGNORED", "BLACKLISTED"):
            continue
        try:
            if int(row.get("series_pinned") or 0) == 1:
                continue
        except (TypeError, ValueError):
            pass

        title = str(row.get("title") or "").strip()
        stored_series = str(row.get("series_name") or "").strip()
        # Cheap fast path: when the release title still contains the stored series
        # spelling, parsing cannot reveal a different attribution; skip the expensive
        # parser/resolver entirely.  This keeps large audits (tens of thousands of
        # correctly-attributed releases) well inside the time budget.
        norm_title = _norm_series(title)
        norm_stored = _norm_series(stored_series)
        if _attribution_title_noise_only(norm_title, norm_stored):
            continue
        parsed_series = _parsed_release_series(title) or stored_series
        suggested_series = parsed_series
        suggested_calibre_id = None
        parsed_key = _norm_series(parsed_series)
        if resolve_series_title is not None and parsed_series and parsed_key:
            if parsed_key in resolution_cache:
                resolution = resolution_cache[parsed_key]
            else:
                try:
                    resolution = resolve_series_title(parsed_series, series_rows, aliases)
                except Exception:
                    resolution = None
                resolution_cache[parsed_key] = resolution
            if resolution is not None:
                canonical = str(getattr(resolution, "canonical_name", "") or "").strip()
                if canonical:
                    suggested_series = canonical
                try:
                    resolved_calibre_id = getattr(resolution, "calibre_series_id", None)
                    suggested_calibre_id = (
                        int(resolved_calibre_id) if resolved_calibre_id not in (None, "") else None
                    )
                except (TypeError, ValueError):
                    suggested_calibre_id = None

        stored_calibre_id = stored_calibre_by_name.get(_norm_series(stored_series))
        name_differs = (
            bool(stored_series)
            and bool(suggested_series)
            and _norm_series(stored_series) != _norm_series(suggested_series)
        )
        calibre_differs = (
            stored_calibre_id is not None
            and suggested_calibre_id is not None
            and stored_calibre_id != suggested_calibre_id
        )
        if not name_differs and not calibre_differs:
            continue

        items.append({
            "release_id": row.get("id"),
            "title": title,
            "series_name": stored_series,
            "parsed_series": parsed_series,
            "suggested_series": suggested_series,
            "suggested_calibre_series_id": suggested_calibre_id,
            "pinned": bool(row.get("series_pinned")),
        })
        if len(items) >= cap:
            break

    result["items"] = items
    result["count"] = len(items)
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


__all__ = [
    "format_bytes",
    "storage_health",
    "network_diagnostics",
    "series_attribution_audit",
    "NETWORK_TARGETS",
]
