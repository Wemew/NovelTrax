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
