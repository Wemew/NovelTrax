"""Pure standard-library helpers for gap-free ("catch-up") feed scanning.

NovelTrax normally scans a fixed window (the newest N pages) of every feed.
When a site publishes more releases between two scans than that window holds,
the excess silently falls through the gap between the two scan windows.  A
catch-up scan closes the gap by continuing backwards until it reaches a
release the previous scan already covered.

This module holds the arithmetic for that feature and deliberately imports
nothing outside the Python standard library: the provider stack (bs4,
feedparser, httpx) is unavailable in the lightweight test environment, so the
watermark/timestamp logic must be unit-testable on its own.

Concepts
--------
``items``
    Provider ``ReleaseItem`` objects (or dicts with the same keys), ordered
    *newest first* -- the order every provider returns.

``cutoff``
    ISO-8601 timestamp of the previous scan's coverage boundary: every release
    newer than it has already been seen.  ``None`` on the first scan.

``anchor``
    GUID (or link) of the newest item the previous scan saw.  It is the
    fallback boundary for feeds whose items carry no timestamps (J-Novels):
    keep paging until the anchor item is reached.

``watermarks``
    The three config keys persisted inside ``feeds.config_json``:
    ``last_scan_oldest_pub_date`` (cutoff), ``last_scan_anchor_guid`` (anchor)
    and ``last_scan_started_at`` (when the scan that produced them began).

Safety rule
-----------
A watermark may only move *forward* when the scan proved it reached the
previous boundary.  If a scan is stopped by its page cap (a burst larger than
``max_pages``), the cutoff is left untouched so the next scan retries the same
gap instead of skipping over unscanned releases.
"""

from __future__ import annotations

import email.utils
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Tuple

UTC = timezone.utc

#: Default number of pages a catch-up scan may walk backwards.
DEFAULT_CATCHUP_MAX_PAGES = 10
#: Absolute upper bound so a bad setting/config cannot turn a scan unbounded.
HARD_MAX_CATCHUP_PAGES = 50

_EPOCH_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


# ---------------------------------------------------------------------------
# Item accessors (ReleaseItem instances *or* plain dicts)
# ---------------------------------------------------------------------------
def item_value(item: Any, field: str, default: Any = None) -> Any:
    """Reads ``field`` from a ReleaseItem-like object or a dict."""
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)


def item_guids(item: Any) -> List[str]:
    """Returns the non-empty identity strings of *item* (guid first, then link).

    Providers identify a release either by GUID (RSS ``<guid>``, Nyaa permalink)
    or by a derived GUID (J-Novels) plus its link, so anchor matching must accept
    either.
    """
    identities: List[str] = []
    for field in ("guid", "link"):
        value = item_value(item, field)
        if isinstance(value, str):
            value = value.strip()
            if value and value not in identities:
                identities.append(value)
    return identities


def item_identity(item: Any) -> Optional[str]:
    """Returns the single best identity for *item*, or ``None``."""
    identities = item_guids(item)
    return identities[0] if identities else None


def matches_identity(item: Any, identity: Optional[str]) -> bool:
    """True when *item* carries *identity* as its GUID or its link."""
    if not identity:
        return False
    return identity.strip() in item_guids(item)


def item_pub_date(item: Any) -> Any:
    """Returns the raw ``pub_date`` value of *item* (may be ``None``)."""
    return item_value(item, "pub_date")


# ---------------------------------------------------------------------------
# Timestamp parsing / comparison
# ---------------------------------------------------------------------------
def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parses *value* into an aware UTC ``datetime``, or ``None``.

    Accepted forms (all UTC-normalised):

    * ``datetime`` instances (naive ones are assumed to be UTC);
    * numeric epoch seconds (``int``/``float``, or a numeric string);
    * ISO-8601 strings, including a trailing ``Z`` and ``+00:00``;
    * RFC 2822 date strings, as found in some RSS ``pubDate`` elements.

    Anything else -- including ``None``, empty strings and ``bool`` -- yields
    ``None``.  Unparseable input is never an error: callers treat ``None`` as
    "unknown" and keep the item.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    if isinstance(value, (int, float)):
        return _epoch_to_datetime(value)

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # Numeric epoch, e.g. "1699999999" or "1699999999.5".
    if _EPOCH_RE.match(text):
        try:
            return _epoch_to_datetime(float(text))
        except (ValueError, TypeError, OverflowError, OSError):
            return None

    # ISO-8601 (also handles "YYYY-MM-DD HH:MM:SS" and date-only strings).
    iso_text = text
    if iso_text[-1] in ("Z", "z"):
        iso_text = iso_text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso_text)
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    # RFC 2822 / RFC 822 (e.g. RSS pubDate: "Sat, 01 Nov 2025 12:00:00 +0000").
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    return None


def _epoch_to_datetime(value: float) -> Optional[datetime]:
    """Converts epoch seconds to an aware UTC datetime (``None`` on overflow)."""
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def to_iso(value: Any) -> Optional[str]:
    """Normalises any accepted timestamp form to an ISO-8601 UTC string."""
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def is_newer_than(value: Any, cutoff: Any) -> bool:
    """True when *value* is strictly newer than *cutoff*.

    Fails open: an unknown item timestamp (or an unparseable/missing cutoff)
    keeps the item, because dropping a real release is worse than one extra
    database upsert.
    """
    cutoff_dt = parse_timestamp(cutoff)
    if cutoff_dt is None:
        return True
    item_dt = parse_timestamp(value)
    if item_dt is None:
        return True
    return item_dt > cutoff_dt


# ---------------------------------------------------------------------------
# Selecting the releases still needed by this scan
# ---------------------------------------------------------------------------
def filter_newer_than(items: Iterable[Any], cutoff: Any) -> List[Any]:
    """Keeps only items newer than *cutoff*, preserving the newest-first order."""
    return [item for item in items if is_newer_than(item_pub_date(item), cutoff)]


def truncate_at_anchor(items: Iterable[Any], stop_guid: Optional[str]) -> List[Any]:
    """Drops the anchor item itself and everything older than it.

    ``items`` must be newest-first.  When *stop_guid* is missing (or never
    appears) the list is returned unchanged.
    """
    if not stop_guid:
        return list(items)
    kept: List[Any] = []
    for item in items:
        if matches_identity(item, stop_guid):
            break
        kept.append(item)
    return kept


def truncate_newest_first(
    items: Iterable[Any],
    cutoff_iso: Any = None,
    stop_guid: Optional[str] = None,
) -> Tuple[List[Any], Optional[str]]:
    """Truncates a newest-first list at the first already-covered release.

    Returns ``(kept_items, stop_reason)`` where *stop_reason* is:

    * ``"anchor"`` -- the previous scan's newest item was reached;
    * ``"cutoff"`` -- a release not newer than the cutoff was reached;
    * ``None``     -- the list was exhausted without hitting either boundary
      (the caller cannot prove that the gap was closed).

    A ``None`` *cutoff_iso*/*stop_guid* disables that particular boundary check.
    The keyword is ``cutoff_iso`` to match the provider-facing
    ``fetch_releases_since(cutoff_iso=...)`` API.
    """
    kept: List[Any] = []
    for item in items:
        if stop_guid and matches_identity(item, stop_guid):
            return kept, "anchor"
        if cutoff_iso is not None and not is_newer_than(item_pub_date(item), cutoff_iso):
            return kept, "cutoff"
        kept.append(item)
    return kept, None


# ---------------------------------------------------------------------------
# Watermark computation
# ---------------------------------------------------------------------------
def oldest_pub_date(items: Iterable[Any]) -> Optional[str]:
    """Earliest parseable ``pub_date`` among *items*, as normalised ISO-8601."""
    oldest: Optional[datetime] = None
    for item in items:
        parsed = parse_timestamp(item_pub_date(item))
        if parsed is None:
            continue
        if oldest is None or parsed < oldest:
            oldest = parsed
    return oldest.isoformat() if oldest is not None else None


def newest_identity(items: Iterable[Any]) -> Optional[str]:
    """Identity of the newest item (the first item of a newest-first list)."""
    for item in items:
        identity = item_identity(item)
        if identity:
            return identity
    return None


def compute_scan_watermarks(
    previous_cutoff: Optional[str],
    previous_anchor: Optional[str],
    items: Iterable[Any],
    reached_boundary: bool,
    scan_started_at: str,
) -> Tuple[str, Optional[str]]:
    """Computes the ``(cutoff, anchor)`` watermarks to persist after a scan.

    * First scan (no *previous_cutoff*): the cutoff is the oldest release the
      scan saw, so the next scan knows where the first window ended.
    * Scan that reached the previous boundary: the oldest release it saw is the
      new cutoff (everything between it and the previous cutoff was re-covered,
      so the watermark may advance).
    * Scan stopped by its page cap: the previous cutoff is kept unchanged, so
      the missed releases stay inside the next scan's window instead of being
      skipped forever.

    The anchor is always the newest identity seen, falling back to the previous
    anchor when the scan returned no items.
    """
    item_list = list(items or [])
    anchor = newest_identity(item_list) or previous_anchor
    oldest = oldest_pub_date(item_list)

    if not previous_cutoff:
        cutoff = oldest or scan_started_at
    elif reached_boundary:
        cutoff = oldest or previous_cutoff
    else:
        cutoff = previous_cutoff
    return cutoff, anchor


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def clamp_max_pages(value: Any, default: int = DEFAULT_CATCHUP_MAX_PAGES) -> int:
    """Clamps a configured ``max_pages`` value to ``1..HARD_MAX_CATCHUP_PAGES``.

    Invalid/missing input falls back to *default* (itself clamped).
    """
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, min(HARD_MAX_CATCHUP_PAGES, parsed))


def dedupe_by_identity(items: Iterable[Any]) -> List[Any]:
    """Removes duplicate releases (same GUID or link), preserving order."""
    seen: set = set()
    unique: List[Any] = []
    for item in items:
        identities = item_guids(item)
        if any(identity in seen for identity in identities):
            continue
        seen.update(identities)
        unique.append(item)
    return unique


def latest_meta(reached_boundary: bool, pages: int, stop_reason: Optional[str], **extra: Any) -> dict:
    """Builds the ``last_scan_meta`` payload providers expose after a scan.

    Kept here so every provider reports the same keys to ``TaskManager``.
    """
    meta = {
        "reached_boundary": bool(reached_boundary),
        "pages_scanned": max(0, int(pages)),
        "stop_reason": stop_reason or ("reached_boundary" if reached_boundary else "unknown"),
        "extra_pages": 0,
        "extra_items": 0,
        "max_pages_reached": (stop_reason == "page_cap"),
    }
    meta.update(extra)
    return meta
