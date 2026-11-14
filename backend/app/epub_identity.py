"""Validate that a downloaded archive belongs to the requested release."""

from __future__ import annotations

import difflib
import os
import re
from typing import Any, Dict

from app.epub_metadata import read_epub_metadata
from app.parser import extract_series_and_volume


def _identity_float(value: Any):
    try:
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        return float(value)
    except (TypeError, ValueError):
        return None


def _identity_text(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b(?:vol(?:ume)?|book|part)\.?\s*\d+(?:\.\d+)?\b", " ", text)
    text = re.sub(r"\b(?:epub|pdf|cbz|novel)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def download_identity_error(release: Dict[str, Any], path: str) -> str:
    """Return a mismatch description, or empty when the archive is acceptable."""
    metadata: Dict[str, Any] = {}
    if str(path).lower().endswith(".epub"):
        try:
            metadata = read_epub_metadata(path) or {}
        except Exception:
            metadata = {}
    filename = os.path.basename(path)
    release_title = str(release.get("title") or "")
    parsed_release = extract_series_and_volume(release_title)
    release_series = str(release.get("series_name") or parsed_release.get("series_name") or release_title)
    release_volume = _identity_float(release.get("volume"))
    if release_volume is None:
        release_volume = _identity_float(parsed_release.get("volume"))

    file_volume = _identity_float(metadata.get("series_index"))
    if file_volume is None:
        parsed_file = extract_series_and_volume(metadata.get("title") or filename)
        file_volume = _identity_float(parsed_file.get("volume"))
    if release_volume is not None and file_volume is not None and abs(release_volume - file_volume) > 0.01:
        return f"downloaded volume {file_volume:g} does not match release volume {release_volume:g}"

    release_norm = _identity_text(release_series)
    if not release_norm:
        return ""
    candidates = [
        _identity_text(metadata.get("series")),
        _identity_text(metadata.get("title")),
        _identity_text(os.path.splitext(filename)[0]),
    ]
    candidates = [c for c in candidates if c and c not in {"unknown", "untitled", "novel"}]
    if not candidates:
        return ""
    best = max(difflib.SequenceMatcher(None, release_norm, candidate).ratio() for candidate in candidates)
    if best < 0.35:
        return f"downloaded title does not match release series {release_series!r}"
    return ""
