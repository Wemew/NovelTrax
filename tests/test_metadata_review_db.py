"""EPUB metadata review queue persistence/upsert tests."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.database import Database  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    review_id = db.upsert_epub_metadata_review(
        12, "/downloads/book.epub",
        {"title": "Title", "author": "Author", "series": "Series", "series_index": 3},
        source="calibre+anilist", note="check me", is_new_volume=True,
    )
    check(review_id > 0, review_id)
    same_id = db.upsert_epub_metadata_review(
        12, "/downloads/book.epub",
        {"title": "Title 2", "author": "Author", "series": "Series", "series_index": 3},
        source="calibre+anilist",
    )
    check(same_id == review_id, (review_id, same_id))
    rows = db.list_epub_metadata_reviews()
    check(len(rows) == 1 and rows[0]["title"] == "Title 2", rows)
    check(rows[0]["is_new_volume"] == 1, rows)
    db.update_epub_metadata_review(review_id, {"title": "Final", "author": "A", "series": "S", "series_index": 4}, "approved", "")
    row = db.get_epub_metadata_review(review_id)
    check(row["review_status"] == "approved" and row["series_index"] == 4.0, row)
    template = open(os.path.join(_BASE, "backend", "app", "templates", "metadata_review.html"), encoding="utf-8").read()
    check("review.is_new_volume" in template and "New volume" in template, "new-volume badge missing")
    print("metadata review DB suite PASSED")


if __name__ == "__main__":
    main()
