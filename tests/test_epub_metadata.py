"""EPUB OPF metadata read/write and prefill tests."""

import os
import sys
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.epub_metadata import read_epub_metadata, write_epub_metadata  # noqa: E402
from app import epub_prefill  # noqa: E402
from app.database import Database  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def make_epub(path):
    container = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    opf = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Old Title</dc:title>
    <dc:creator>Old Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>'''
    with zipfile.ZipFile(path, "w") as book:
        mimetype = zipfile.ZipInfo("mimetype")
        mimetype.compress_type = zipfile.ZIP_STORED
        book.writestr(mimetype, "application/epub+zip")
        book.writestr("META-INF/container.xml", container)
        book.writestr("OEBPS/content.opf", opf)
        book.writestr("OEBPS/chapter1.xhtml", "<html><body>payload</body></html>")


def test_roundtrip_preserves_epub():
    fd, path = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    make_epub(path)
    written = write_epub_metadata(path, {
        "title": "Grimgar of Fantasy and Ash Vol. 23",
        "author": "Ao Jyumonji",
        "series": "Grimgar of Fantasy and Ash",
        "series_index": 23,
    })
    check(written["title"] == "Grimgar of Fantasy and Ash Vol. 23", written)
    check(written["author"] == "Ao Jyumonji", written)
    check(written["series"] == "Grimgar of Fantasy and Ash", written)
    check(written["series_index"] == 23.0, written)
    with zipfile.ZipFile(path) as book:
        infos = book.infolist()
        check(infos[0].filename == "mimetype", infos[0].filename)
        check(infos[0].compress_type == zipfile.ZIP_STORED, infos[0].compress_type)
        check(book.read("mimetype") == b"application/epub+zip", "mimetype")
        check(book.read("OEBPS/chapter1.xhtml") == b"<html><body>payload</body></html>", "payload")
        check(b"Grimgar of Fantasy and Ash" in book.read("OEBPS/content.opf"), "opf series")


def test_prefill_writes_and_queues_review():
    fd, path = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    make_epub(path)
    captured = {}

    class FakeDB:
        def get_all_series(self):
            return [{"name": "Grimgar of Fantasy and Ash", "authors": "Ao Jyumonji"}]
        def upsert_epub_metadata_review(self, release_id, file_path, fields, source="", note="", review_status="pending", is_new_volume=False):
            captured.update({"release_id": release_id, "file_path": file_path, "fields": fields, "source": source, "note": note, "status": review_status})
            return 7

    original = epub_prefill.NovelMetadataProvider.fetch_series_metadata
    epub_prefill.NovelMetadataProvider.fetch_series_metadata = classmethod(
        lambda cls, title: {"english_title": "Grimgar of Fantasy and Ash", "author": "Ao Jyumonji"}
    )
    try:
        result = epub_prefill.prepare_downloaded_epub(FakeDB(), {
            "id": 42, "series_name": "Grimgar of Fantasy and Ash", "title": "Grimgar of Fantasy and Ash Volume 23 Epub",
            "volume": 23.0, "author": "",
        }, path)
    finally:
        epub_prefill.NovelMetadataProvider.fetch_series_metadata = original

    check(result and result["review_id"] == 7, result)
    check(captured["source"] == "calibre+anilist", captured)
    check(captured["status"] == "pending", captured)
    check(result["is_new_volume"] is True, result)
    written = read_epub_metadata(path)
    check(written["title"] == "Grimgar of Fantasy and Ash Vol. 23", written)
    check(written["series"] == "Grimgar of Fantasy and Ash", written)
    check(written["series_index"] == 23.0, written)
    check(written["author"] == "Ao Jyumonji", written)
    check(epub_prefill.prepare_downloaded_epub(FakeDB(), {}, "book.pdf") is None, "pdf ignored")


def test_prefill_uses_real_series_row():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path)
    db.init_db()
    db.sync_calibre_series([{
        "series_id": 9, "series_name": "Grimgar of Fantasy and Ash",
        "book_count": 22, "latest_index": 22.0, "authors": "Ao Jyumonji",
    }])
    original = epub_prefill.NovelMetadataProvider.fetch_series_metadata
    epub_prefill.NovelMetadataProvider.fetch_series_metadata = classmethod(
        lambda cls, title: {"english_title": "Grimgar of Fantasy and Ash", "author": "Ao Jyumonji", "synonyms": []}
    )
    try:
        fields = epub_prefill.build_epub_prefill(db, {
            "id": 1, "series_name": "Grimgar of Fantasy and Ash", "volume": 23.0,
            "title": "Grimgar of Fantasy and Ash Volume 23 Epub",
        })
    finally:
        epub_prefill.NovelMetadataProvider.fetch_series_metadata = original
    check(fields["series"] == "Grimgar of Fantasy and Ash", fields)
    check(fields["author"] == "Ao Jyumonji", fields)
    check(fields["series_index"] == 23.0, fields)
    check(fields["calibre_matched"] is True, fields)
    check(fields["is_new_volume"] is True, fields)


def test_read_missing_and_non_epub():
    try:
        read_epub_metadata("book.pdf")
    except Exception as exc:
        check("EPUB" in str(exc), exc)
    else:
        raise AssertionError("PDF accepted")


def main():
    test_roundtrip_preserves_epub()
    test_prefill_writes_and_queues_review()
    test_prefill_uses_real_series_row()
    test_read_missing_and_non_epub()
    print("epub metadata suite PASSED")


if __name__ == "__main__":
    main()
