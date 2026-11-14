"""Prefill EPUB OPF metadata from Calibre + AniList and flag it for review."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from app.epub_metadata import EpubMetadataError, read_epub_metadata, write_epub_metadata
from app.parser import match_series_in_calibre
from app.providers.metadata import NovelMetadataProvider

logger = logging.getLogger("noveltrax.epub_prefill")


def _volume_text(volume: Any) -> str:
    try:
        number = float(volume)
    except (TypeError, ValueError):
        return ""
    return str(int(number)) if number.is_integer() else str(number)


def build_epub_prefill(db: Any, release: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve title/author/series/index from the matched Calibre series + AniList."""
    release_series = str(release.get("series_name") or "").strip()
    release_title = str(release.get("title") or "").strip()
    search_title = release_series or release_title
    volume = release.get("volume")
    series_rows = db.get_all_series()
    matched_series = match_series_in_calibre(search_title, series_rows) if search_title else None

    anilist = None
    try:
        anilist = NovelMetadataProvider.fetch_series_metadata(search_title)
    except Exception as exc:
        logger.debug("AniList prefill lookup failed for %r: %s", search_title, exc)
    if matched_series is None and anilist and search_title:
        alt_names = [
            anilist.get("english_title"), anilist.get("title"),
            anilist.get("canonical_title"), anilist.get("romaji_title"),
            *(anilist.get("synonyms") or []),
        ]
        matched_series = match_series_in_calibre(
            search_title, series_rows,
            alt_names=[name for name in alt_names if name],
        )

    source = []
    if matched_series:
        source.append("calibre")
    if anilist:
        source.append("anilist")

    canonical_series = ""
    if matched_series:
        canonical_series = str(matched_series.get("series_name") or matched_series.get("name") or "").strip()
    if not canonical_series and anilist:
        canonical_series = str(
            anilist.get("english_title") or anilist.get("title") or anilist.get("canonical_title") or ""
        ).strip()
    if not canonical_series:
        canonical_series = release_series or release_title

    author = ""
    if matched_series:
        author = str(matched_series.get("authors") or "").strip()
        if author.lower() == "unknown":
            author = ""
    if not author and anilist:
        author = str(anilist.get("author") or "").strip()
    if not author:
        author = str(release.get("author") or "").strip()
    if not author:
        author = "Unknown"
        source.append("author-missing")

    book_title = str(
        (anilist or {}).get("english_title") or (anilist or {}).get("title") or canonical_series
    ).strip()
    vol_text = _volume_text(volume)
    if vol_text:
        book_title = f"{book_title} Vol. {vol_text}"

    notes = []
    if not matched_series:
        notes.append("No Calibre series match")
    if not volume:
        notes.append("Volume not detected")
    if not anilist:
        notes.append("No AniList metadata")

    owned_volumes = set()
    if matched_series:
        try:
            owned_volumes = {round(float(v), 2) for v in json.loads(matched_series.get("owned_volumes_json") or "[]")}
        except (TypeError, ValueError):
            owned_volumes = set()
    try:
        is_new_volume = volume is None or round(float(volume), 2) not in owned_volumes
    except (TypeError, ValueError):
        is_new_volume = True

    return {
        "title": book_title,
        "author": author,
        "series": canonical_series,
        "series_index": float(volume) if volume is not None else None,
        "source": "+".join(source) or "release",
        "note": "; ".join(notes),
        "calibre_matched": bool(matched_series),
        "anilist_matched": bool(anilist),
        "is_new_volume": is_new_volume,
    }


def prepare_downloaded_epub(db: Any, release: Dict[str, Any], file_path: str) -> Optional[Dict[str, Any]]:
    """Write prefill metadata into an EPUB and add/refresh its review row.

    Metadata failure never breaks the download; it is recorded for review.
    PDFs and non-EPUB targets are ignored.
    """
    path = str(file_path or "")
    if not path.lower().endswith(".epub") or not os.path.isfile(path):
        return None

    fields = build_epub_prefill(db, release)
    note = fields.get("note") or ""
    try:
        written = write_epub_metadata(path, fields)
        # Reflect exactly what landed in the file (preserves manually curated
        # values already inside the EPUB when a field was blank in prefill).
        for key in ("title", "author", "series", "series_index"):
            if not fields.get(key) and written.get(key):
                fields[key] = written[key]
    except EpubMetadataError as exc:
        note = (note + "; " if note else "") + f"Metadata write failed: {exc}"
        logger.warning("EPUB metadata prefill failed for %s: %s", path, exc)

    review_id = db.upsert_epub_metadata_review(
        release.get("id"), path, fields, source=fields.get("source") or "",
        note=note, review_status="pending",
        is_new_volume=bool(fields.get("is_new_volume")),
    )
    return {
        "review_id": review_id, "path": path, "fields": fields,
        "title": fields.get("title"), "author": fields.get("author"),
        "series": fields.get("series"), "series_index": fields.get("series_index"),
        "is_new_volume": bool(fields.get("is_new_volume")),
    }


def backfill_downloaded_epubs(db: Any, reporter: Any = None) -> Dict[str, int]:
    """Tag/download-review EPUBs recorded before the prefill feature existed."""
    rows = db.list_releases_with_download_files(limit=1000)
    paths = []
    releases = {}
    for release in rows:
        try:
            recorded = json.loads(release.get("download_files_json") or "[]")
        except (TypeError, ValueError):
            recorded = []
        for path in recorded:
            path = str(path or "")
            if not path.lower().endswith(".epub") or not os.path.isfile(path):
                continue
            if db.get_epub_metadata_review_by_path(path):
                continue
            if path not in releases:
                paths.append(path)
                releases[path] = release
    total = len(paths)
    if reporter is not None:
        reporter.report(total=total, message=f"Scanning {total} existing EPUB(s)…", force=True)
    queued = 0
    failed = 0
    for index, path in enumerate(paths, 1):
        try:
            if prepare_downloaded_epub(db, releases[path], path):
                queued += 1
        except Exception as exc:
            failed += 1
            logger.debug("Existing EPUB prefill failed for %s: %s", path, exc)
        if reporter is not None:
            reporter.report(
                done=index - 1, total=total,
                message=f"Prefilling EPUB {index}/{total}: {os.path.basename(path)}",
            )
    if reporter is not None:
        reporter.report(done=total, total=total, message=f"Queued {queued} EPUB review(s)", force=True)
    return {"found": total, "queued": queued, "failed": failed}
