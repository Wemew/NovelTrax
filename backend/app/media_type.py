"""Media-type classification for feed releases.

NovelTrax tracks light-novel ebooks. Nyaa's "Literature" category (3_1) is
polluted with manga volumes, chapter compilations and audiobooks, and the title
formats overlap (both LN and manga rips use ``v01-05`` tags), so the category
alone cannot be trusted.

Two independent signals are used:

1. ``classify_title`` -- cheap, offline. Decisive for clear markers such as
   ``[Audiobook]``, ``(Digital-Compilation)``, ``as v01-02`` or ``Chapter 222``.
   Returns ``None`` when the title is ambiguous.

2. ``classify_torrent`` -- authoritative. The torrent metainfo lists the actual
   filenames, so ``.epub`` vs ``.cbz`` settles it. A Nyaa torrent is ~40 KB, and
   results are cached per release, so the cost is paid once per release.

Policy
------
* ``novel``     -> an ebook (EPUB/AZW3/MOBI). The normal tracking path.
* ``audiobook`` -> audio files, or an audiobook-tagged title. Stored, but shown
                   only in the dedicated Audiobooks section, never in Releases,
                   Discovery or the wishlist.
* ``manga``     -> comic archives/images, or a title carrying an unambiguous
                   manga marker. Not stored at all.
* ``unknown``   -> could not be determined (no torrent, fetch failed). Kept so a
                   real release is never silently lost; retried on a later scan.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("noveltrax.media_type")

NOVEL = "novel"
AUDIOBOOK = "audiobook"
MANGA = "manga"
UNKNOWN = "unknown"

# Media types that must never appear in Releases / Discovery / the wishlist.
NON_TRACKED = (MANGA, AUDIOBOOK)

# --- Title markers -------------------------------------------------------

_AUDIOBOOK_TITLE_RE = re.compile(
    r"(?i)[\[\(]\s*(?:audio\s?books?|audio|m4b|m4a|mp3|flac|opus)\s*[\]\)]"
    r"|\baudio\s?book\b"
    r"|\b(?:seven\s+seas\s+siren|tantor\s+media|podium\s+audio|sound\s+booth)\b"
)

_MANGA_TITLE_RE = re.compile(
    r"(?i)"
    r"\bas\s+v\d"                                         # "001-009 as v01-02"
    r"|\(\s*digital(?:\s*[+-]\s*digital)?[\s-]*compilation\s*\)"
    r"|\(\s*digital\s*\)"                                 # "(Digital)" scanlation tag
    r"|\(\s*\d{4}(?:\s*-\s*\d{4})?\s*\)"                  # "(2025-2026)" year range
    r"|\[\s*jpeg[\s-]*xl\s*\]"                            # image-only format tags
    r"|\[\s*png\s*\]"
    r"|\((?:[^)]*\b(?:anon|anonymous|oak(?:-jxl)?|1r0n|rillant|ushi|lucaz|shizu|nao|"
    r"aquila|digitalmangafan|k-team|danke-empire|xra-empire|sticky\s+oak)\b[^)]*)\)"
    r"|\bchapters?\s+\d+"                                  # "Chapter 222"
    r"|\bch\.\s*\d+"
    r"|\bchapter\s+updates?\b"
    r"|\b(?:weekly|monthly)\s+(?:viz|shonen|jump|manga|k\s?manga|alpha)\b"
    r"|\bmanga\b"
)

# Bracketed publisher / ebook-format tags: the light-novel release convention
# ("… v01-04 [Seven Seas] [Stick]", "… v01-17 [J-Novel Club] [CleanBookGuy]").
_LIGHT_NOVEL_TITLE_RE = re.compile(
    r"(?i)"
    r"\[\s*(?:j-novel\s*club|j-novel\s*heart|yen\s*press|seven\s*seas|seven\s*seas\s+siren|"
    r"hanashi\s*media|cross\s+infinite\s+world|airship|one\s+peace\s+books|"
    r"tentai\s+books|vertical|sol\s+press|jnc|yen\s*on|orbit|drawn\s+&?\s*quarterly)"
    r"[^\]]*\]"
    r"|\[\s*(?:epub|azw3|mobi|kepub)\s*\]"
)

# Filename extensions inside the torrent metainfo.
_NOVEL_EXTS = {".epub", ".azw3", ".azw", ".mobi", ".kepub", ".fb2"}
_AUDIO_EXTS = {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
_COMIC_EXTS = {".cbz", ".cbr", ".cb7", ".cbt"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


def classify_title(title: str) -> Optional[str]:
    """
    Classify a release from its title alone.

    Returns ``AUDIOBOOK`` / ``MANGA`` / ``NOVEL`` when the title carries a
    decisive marker, otherwise ``None`` (ambiguous -- needs torrent
    verification).

    The discriminating convention, as observed in Nyaa category 3_1:

    * manga  -> parenthesised scanlation metadata after the volume token:
      ``… v01-03 (2025-2026) (Digital) (1r0n, Anon)``
    * novel  -> bracketed publisher/format tags instead:
      ``… v01-04 [Seven Seas] [Stick]``, ``… v01-17 [J-Novel Club] [CleanBookGuy]``

    When a title carries BOTH signals the title is treated as ambiguous and the
    torrent file list decides, rather than guessing.
    """
    if not title or not isinstance(title, str):
        return None
    # Audio wins: "… v01-02 [Audiobook] [Seven Seas Siren]" is still audio.
    if _AUDIOBOOK_TITLE_RE.search(title):
        return AUDIOBOOK

    looks_manga = bool(_MANGA_TITLE_RE.search(title))
    looks_novel = bool(_LIGHT_NOVEL_TITLE_RE.search(title))

    if looks_manga and looks_novel:
        return None
    if looks_manga:
        return MANGA
    if looks_novel:
        return NOVEL
    return None


# --- Minimal bencode decoder (torrent metainfo) --------------------------

def _bdecode_int(data: bytes, idx: int) -> Tuple[int, int]:
    end = data.index(b"e", idx)
    return int(data[idx:end]), end + 1


def _bdecode_bytes(data: bytes, idx: int) -> Tuple[bytes, int]:
    sep = data.index(b":", idx)
    length = int(data[idx:sep])
    start = sep + 1
    return data[start:start + length], start + length


def _bdecode(data: bytes, idx: int = 0) -> Tuple[Any, int]:
    char = data[idx:idx + 1]
    if char == b"i":
        return _bdecode_int(data, idx + 1)
    if char == b"l":
        out = []
        idx += 1
        while data[idx:idx + 1] != b"e":
            item, idx = _bdecode(data, idx)
            out.append(item)
        return out, idx + 1
    if char == b"d":
        out: Dict[bytes, Any] = {}
        idx += 1
        while data[idx:idx + 1] != b"e":
            key, idx = _bdecode(data, idx)
            value, idx = _bdecode(data, idx)
            if isinstance(key, bytes):
                out[key] = value
        return out, idx + 1
    if char.isdigit():
        return _bdecode_bytes(data, idx)
    raise ValueError(f"invalid bencode at offset {idx}")


def torrent_filenames(data: bytes) -> list:
    """Returns the file paths declared inside a .torrent metainfo blob."""
    meta, _ = _bdecode(data, 0)
    if not isinstance(meta, dict):
        raise ValueError("torrent metainfo is not a dictionary")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent metainfo has no info dictionary")

    names: list = []
    files = info.get(b"files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                continue
            parts = entry.get(b"path")
            if isinstance(parts, list):
                joined = "/".join(
                    p.decode("utf-8", "ignore") if isinstance(p, bytes) else str(p)
                    for p in parts
                )
                names.append(joined)
    name = info.get(b"name")
    if isinstance(name, bytes):
        names.append(name.decode("utf-8", "ignore"))
    return names


def classify_torrent(data: bytes) -> Tuple[str, str]:
    """
    Classify a torrent from its metainfo blob.

    Returns ``(media_type, detail)`` where detail names the extensions found.
    """
    names = torrent_filenames(data)

    counts: Dict[str, int] = {}
    for name in names:
        lowered = name.lower()
        dot = lowered.rfind(".")
        if dot == -1:
            continue
        ext = lowered[dot:]
        counts[ext] = counts.get(ext, 0) + 1

    novel = sum(counts.get(e, 0) for e in _NOVEL_EXTS)
    audio = sum(counts.get(e, 0) for e in _AUDIO_EXTS)
    comic = sum(counts.get(e, 0) for e in _COMIC_EXTS)
    images = sum(counts.get(e, 0) for e in _IMAGE_EXTS)

    detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no files"

    if audio and not novel:
        return AUDIOBOOK, detail
    if novel and not comic:
        return NOVEL, detail
    if comic or (images and not novel and not audio):
        return MANGA, detail
    if novel and comic:
        # Mixed bundle: trust the ebooks, they are what Calibre tracks.
        return NOVEL, detail
    return UNKNOWN, detail


def nyaa_torrent_url(link: Optional[str]) -> Optional[str]:
    """Derives the .torrent download URL from a Nyaa view page link."""
    if not link or not isinstance(link, str):
        return None
    match = re.search(r"nyaa\.si/view/(\d+)", link)
    if not match:
        return None
    return f"https://nyaa.si/download/{match.group(1)}.torrent"


# --- Resolver ------------------------------------------------------------

class MediaTypeResolver:
    """
    Resolves the media type for a release, using the DB cache where possible.

    ``fetcher(url) -> bytes`` downloads a torrent metainfo blob. It defaults to
    an SSRF-validated httpx GET and can be swapped in tests.
    """

    def __init__(
        self,
        db: Any,
        fetcher: Optional[Callable[[str], Optional[bytes]]] = None,
        min_interval: float = 1.0,
        enabled: bool = True,
    ):
        self.db = db
        self._fetcher = fetcher
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.enabled = enabled
        self._budget = 0

    def begin_run(self, budget: int = 25):
        """
        Resets the per-scan fetch budget.

        Torrent verification costs one request per ambiguous release, so a feed
        scan only verifies a bounded number of them; the rest stay ``unknown``
        (kept, never lost) and are retried on later scans, with the cache
        draining the backlog over time.
        """
        self._budget = max(0, int(budget))

    @property
    def budget_left(self) -> int:
        return self._budget

    def _default_fetch(self, url: str) -> Optional[bytes]:
        """
        Downloads a torrent metainfo blob, validating the destination.

        Uses httpx when available (the production path) and falls back to a
        urllib opener that re-validates every redirect hop, so the SSRF
        guarantees hold in either case.
        """
        try:
            from app.security import is_safe_public_url
        except Exception:  # pragma: no cover
            return None

        if not is_safe_public_url(url):
            logger.info(f"Refusing to fetch torrent from unsafe destination: {url}")
            return None

        try:
            import httpx  # noqa: F401
            from app.security import safe_client_get
        except Exception:
            httpx = None

        if httpx is not None:
            try:
                with httpx.Client(timeout=15.0, follow_redirects=False,
                                  headers={"User-Agent": "NovelTrax/1.0"}) as client:
                    resp = safe_client_get(client, url, max_redirects=3)
                    if getattr(resp, "status_code", 0) == 200:
                        return resp.content
            except Exception as exc:
                logger.debug(f"httpx torrent fetch failed for {url}: {exc}")
            return None

        # Fallback: guarded urllib (no httpx in this runtime).
        try:
            import urllib.error
            import urllib.request
            from urllib.parse import urljoin

            class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
                max_redirections = 3

                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    target = urljoin(req.full_url, newurl)
                    if not is_safe_public_url(target):
                        raise urllib.error.URLError(f"SSRF blocked redirect hop: {target}")
                    return super().redirect_request(req, fp, code, msg, headers, target)

            opener = urllib.request.build_opener(_GuardedRedirect)
            request = urllib.request.Request(url, headers={"User-Agent": "NovelTrax/1.0"})
            with opener.open(request, timeout=15) as resp:
                return resp.read()
        except Exception as exc:
            logger.debug(f"urllib torrent fetch failed for {url}: {exc}")
        return None

    def _throttle(self):
        with self._lock:
            elapsed = time.time() - self._last_request
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request = time.time()

    def resolve(self, guid: str, title: str, link: Optional[str],
                download_url: Optional[str] = None) -> str:
        """
        Returns the media type for a release and caches the decision by GUID.

        Decisions made from the title are cached too: they never need a fetch.
        """
        if not self.enabled:
            return NOVEL

        if guid:
            cached = self.db.get_media_type_cache(guid)
            if cached:
                return cached

        by_title = classify_title(title)
        if by_title:
            if guid:
                self.db.set_media_type_cache(guid, by_title, "title marker")
            return by_title

        torrent_url = download_url if (download_url or "").startswith("http") and ".torrent" in (download_url or "") else None
        if not torrent_url:
            torrent_url = nyaa_torrent_url(link)

        if torrent_url and (title or "").strip():
            if self._budget <= 0:
                return UNKNOWN
            self._budget -= 1
            fetch = self._fetcher or self._default_fetch
            self._throttle()
            try:
                blob = fetch(torrent_url)
            except Exception as exc:
                logger.debug(f"Torrent fetch error for {torrent_url}: {exc}")
                blob = None
            if blob:
                try:
                    media_type, detail = classify_torrent(blob)
                except Exception as exc:
                    logger.debug(f"Torrent parse failed for {torrent_url}: {exc}")
                    media_type, detail = UNKNOWN, f"parse error: {exc}"
                if media_type != UNKNOWN and guid:
                    self.db.set_media_type_cache(guid, media_type, detail)
                return media_type

        # Inconclusive: keep the release, retried on a later scan.
        return UNKNOWN
