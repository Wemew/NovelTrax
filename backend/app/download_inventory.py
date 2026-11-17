"""Bounded, symlink-safe inventory for the downloads folder browser."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List


def format_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


_EBOOK_SUFFIXES = (".epub", ".azw3", ".mobi", ".kepub")

# The series-card API is polled while scrolling; a filesystem walk per card page
# is wasted work when the folder has not changed.  A short TTL keeps new
# downloads visible without hammering the disk.
_VOLUME_SCAN_CACHE: Dict[str, Any] = {"key": None, "ts": 0.0, "value": {}}
_VOLUME_SCAN_TTL_SECONDS = 5.0


def scan_downloaded_volumes(root: str, max_files: int = 5000,
                            max_depth: int = 4) -> Dict[str, List[float]]:
    """Maps normalized series name -> volumes present in the downloads folder.

    A downloaded volume is not necessarily ``IN_CALIBRE`` yet (or tracked by any
    release row), so the series card derives per-volume state from the files on
    disk as well as from release download status.  Bounded and symlink-safe:
    files are only counted when a series name and at least one volume number can
    be parsed from the filename (falling back to the containing folder name for
    torrent folders whose files are named bare ``Volume 3.epub``).
    """
    from app.parser import extract_series_and_volume, normalize_text

    result: Dict[str, set] = {}
    root = os.path.abspath(str(root or "/downloads"))
    cache_key = (root, int(max_files), int(max_depth))
    now = time.monotonic()
    if (
        _VOLUME_SCAN_CACHE.get("key") == cache_key
        and now - float(_VOLUME_SCAN_CACHE.get("ts") or 0.0) < _VOLUME_SCAN_TTL_SECONDS
    ):
        return _VOLUME_SCAN_CACHE.get("value") or {}
    if not os.path.isdir(root):
        _VOLUME_SCAN_CACHE.update(key=cache_key, ts=now, value={})
        return {}
    seen = 0

    def keyed(series: str) -> str:
        return normalize_text(series or "")

    def add(series: str, volumes) -> None:
        key = keyed(series)
        numbers = set()
        for value in volumes or []:
            try:
                number = round(float(value), 2)
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 10000:
                numbers.add(number)
        if key and numbers:
            result.setdefault(key, set()).update(numbers)

    def walk(path: str, depth: int, folder_series: str = "") -> None:
        nonlocal seen
        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
        except OSError:
            return
        entries.sort(key=lambda entry: entry.name.lower())
        for entry in entries:
            if seen >= max_files:
                return
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        walk(entry.path, depth + 1, entry.name)
                    continue
                if not entry.name.lower().endswith(_EBOOK_SUFFIXES):
                    continue
                seen += 1
                stem = os.path.splitext(entry.name)[0]
                parsed = extract_series_and_volume(stem)
                volumes = parsed.get("volumes") or []
                if not volumes and parsed.get("volume") is not None:
                    volumes = [parsed.get("volume")]
                if not volumes:
                    continue
                series = str(parsed.get("series_name") or "").strip()
                # "Volume 4.epub" parses as series "Volume"; use the torrent
                # folder name instead of inventing a bogus series.
                if normalize_text(series) in ("volume", "vol", "vols", "book", "v"):
                    series = ""
                if not series:
                    series = folder_series
                add(series, volumes)
            except OSError:
                continue

    walk(root, 0)
    resolved = {key: sorted(values) for key, values in result.items()}
    _VOLUME_SCAN_CACHE.update(key=cache_key, ts=now, value=resolved)
    return resolved


def scan_download_tree(root: str, max_entries: int = 2000, max_depth: int = 8,
                       max_per_dir: int = 500) -> Dict[str, Any]:
    """Build a bounded display tree; never follows directory symlinks."""
    root = os.path.abspath(str(root or "/downloads"))
    tree: Dict[str, Any] = {
        "name": os.path.basename(root) or root, "path": root, "is_dir": True,
        "children": [], "file_count": 0, "folder_count": 0, "total_size": 0,
        "truncated": False, "error": "",
    }
    if not os.path.isdir(root):
        tree["error"] = "Downloads folder is not available inside the app container."
        return tree

    entries_seen = 0

    def scan(path: str, depth: int) -> List[Dict[str, Any]]:
        nonlocal entries_seen
        nodes: List[Dict[str, Any]] = []
        try:
            with os.scandir(path) as iterator:
                raw = list(iterator)
        except OSError as exc:
            return [{
                "name": f"(unreadable: {exc})", "is_dir": False, "error": True,
                "children": [], "size_label": "", "modified": "", "child_count": 0,
            }]
        raw.sort(key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name.lower()))
        for entry in raw:
            if entries_seen >= max_entries or len(nodes) >= max_per_dir:
                tree["truncated"] = True
                break
            entries_seen += 1
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_link = entry.is_symlink()
                stat = entry.stat(follow_symlinks=False)
                node = {
                    "name": entry.name, "is_dir": is_dir, "is_symlink": is_link,
                    "children": [], "size": 0, "size_label": "",
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                }
                if is_dir and not is_link and depth < max_depth:
                    node["children"] = scan(entry.path, depth + 1)
                    node["folder_count"] = 1 + sum(child.get("folder_count", 0) for child in node["children"])
                    node["file_count"] = sum(child.get("file_count", 0) for child in node["children"])
                    node["total_size"] = sum(child.get("total_size", 0) for child in node["children"])
                    node["size_label"] = format_bytes(node["total_size"])
                    tree["folder_count"] += 1
                elif is_dir:
                    node["size_label"] = "folder"
                    tree["folder_count"] += 1
                else:
                    node["size"] = int(stat.st_size)
                    node["total_size"] = node["size"]
                    node["file_count"] = 1
                    node["size_label"] = format_bytes(node["size"])
                    tree["file_count"] += 1
                    tree["total_size"] += node["size"]
                node["child_count"] = len(node["children"])
                nodes.append(node)
            except OSError:
                continue
        return nodes

    tree["children"] = scan(root, 0)
    tree["size_label"] = format_bytes(tree["total_size"])
    return tree
