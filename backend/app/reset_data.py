"""Factory-reset Noveltrax's catalog from the command line.

Usage (inside the container):
    python -m app.reset_data --yes

Preserves app settings (paths, downloader/provider credentials). Removes releases,
tracked series/Calibre links, aliases/synonyms, metadata caches, blacklists,
review/task rows and audit history; reseeds stock feeds/prioritized groups.
A timestamped backup is written under data/backups before anything is deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.database import Database


def _data_dir() -> str:
    return os.getenv(
        "DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Factory-reset the Noveltrax catalog")
    parser.add_argument("--yes", action="store_true", help="required confirmation")
    parser.add_argument("--db", default="", help="override the database path")
    parser.add_argument("--actor", default="cli", help="audit actor label")
    args = parser.parse_args(argv)
    if not args.yes:
        parser.error("refusing to reset without --yes")

    data_dir = _data_dir()
    db_path = args.db or os.path.join(data_dir, "noveltrax.db")
    if not os.path.isfile(db_path):
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2
    db = Database(db_path)
    result = db.reset_all_data(actor=args.actor)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
