"""Source-link extraction, persistence and UI exposure (J-Novels "VOLUME n")."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.database import Database  # noqa: E402
from app.epub_identity import download_identity_error  # noqa: E402
from app.providers.jnovels import extract_source_link, find_jnovels_article, is_jnovels_page  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_extractor():
    html = '<a href="https://files.example.net/abc123">VOLUME 23</a>'
    check(extract_source_link(html) == "https://files.example.net/abc123", "arbitrary host")
    check(
        extract_source_link('<a href="/direct/vol-3.epub">Volume 3</a>', "https://jnovels.com/post/")
        == "https://jnovels.com/direct/vol-3.epub",
        "relative href resolves against article",
    )
    check(
        extract_source_link('<a href="https://mirror.example.org/get/42">Download</a>')
        == "https://mirror.example.org/get/42",
        "DOWNLOAD fallback",
    )
    check(extract_source_link('<a href="javascript:void(0)">VOLUME 2</a>') == "", "non-http ignored")
    check(extract_source_link('<a href="https://example.com/x">Other</a>') == "", "no match")


def test_jnovels_pages_are_not_sources():
    html = (
        '<a href="https://jnovels.com/tag/volume-3-epub/">Volume 3 Epub</a>'
        '<a href="https://jnovels.com/classroom-of-the-elite-year-3-volume-3-epub/">'
        'Classroom of the Elite year 3 Volume 3 Epub</a>'
        '<a href="https://charexempire.com/jg7AN">YEAR 3 VOLUME 3</a>'
    )
    check(extract_source_link(html, "https://jnovels.com/tag/volume-3-epub/") == "https://charexempire.com/jg7AN", "external volume link")
    check(is_jnovels_page("https://jnovels.com/tag/volume-3-epub/"), "tag page detected")
    only_tags = '<a href="https://jnovels.com/tag/volume-3-epub/">Volume 3</a>'
    check(extract_source_link(only_tags) == "", "tag-only is not a source")

    listing = (
        '<a href="https://jnovels.com/fired-but-i-maintain-all-the-software-volume-3-epub/">'
        'Fired? But I Maintain All the Software! Volume 3 Epub</a>'
        '<a href="https://jnovels.com/classroom-of-the-elite-year-3-volume-3-epub/">'
        'Classroom of the Elite year 3 Volume 3 Epub</a>'
    )
    check(
        find_jnovels_article(listing, "https://jnovels.com/tag/volume-3-epub/", "Classroom of the Elite year 3 Volume 3 Epub")
        == "https://jnovels.com/classroom-of-the-elite-year-3-volume-3-epub/",
        "title picks the right article",
    )


def test_source_rejects_ads_and_wrong_volume():
    html = (
        '<a href="https://app.adjust.com/12mstyjh">Download</a>'
        '<a href="https://charexempire.com/wrong">VOLUME 02</a>'
        '<a href="https://charexempire.com/right">VOLUME 03</a>'
    )
    check(extract_source_link(html, expected_volume=3) == "https://charexempire.com/right", "expected volume wins")
    check(extract_source_link(html, expected_volume=7) == "", "no wrong-volume fallback")


def test_download_identity_rejects_mismatched_release():
    release = {
        "title": "I Like Villains, so I Reincarnated as One Volume 3 Epub",
        "series_name": "I Like Villains, so I Reincarnated as One",
        "volume": 3,
    }
    wrong = "History of the Kingdom of Orcsen Volume 02.epub"
    check(bool(download_identity_error(release, wrong)), "wrong volume/series rejected")
    right = "I Like Villains, so I Reincarnated as One Vol. 3.epub"
    check(not download_identity_error(release, right), "matching release accepted")
    generic = "novel.epub"
    check(not download_identity_error(release, generic), "generic filename is not guessed")


def test_persistence():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.init_db()
    is_new, release_id = db.upsert_release({
        "guid": "source-1",
        "title": "Grimgar of Fantasy and Ash Volume 23 Epub",
        "link": "https://jnovels.com/grimgar-of-ash-volume-23-epub/",
        "series_name": "Grimgar of Fantasy and Ash",
        "volume": 23.0,
        "source_link": "https://any-host.example/download/9",
    })
    check(is_new and release_id > 0, "release inserted")
    row = db.get_release_by_id(release_id)
    check(row and row["source_link"] == "https://any-host.example/download/9", row)

    # Re-ingest without the link never clears the stored one.
    db.upsert_release({
        "guid": "source-1",
        "title": "Grimgar of Fantasy and Ash Volume 23 Epub",
        "link": "https://jnovels.com/grimgar-of-ash-volume-23-epub/",
        "series_name": "Grimgar of Fantasy and Ash",
        "volume": 23.0,
        "source_link": "",
    })
    check(db.get_release_by_id(release_id)["source_link"] == "https://any-host.example/download/9", "kept")

    candidates = db.list_releases_missing_source_link(10)
    check(release_id not in {row["id"] for row in candidates}, "filled row not queued")

    # Backfill is scoped to J-Novels article pages; Nyaa/torrent links have no
    # hidden source anchor and must not be fetched by the maintenance job.
    db.upsert_release({
        "guid": "nyaa-1",
        "title": "Example Novel Volume 1 Epub",
        "link": "https://nyaa.si/view/123",
        "series_name": "Example Novel",
        "volume": 1.0,
    })
    _, older_id = db.upsert_release({
        "guid": "jnovels-older",
        "title": "Older Novel Volume 1 Epub",
        "link": "https://jnovels.com/older-novel-volume-1-epub/",
        "series_name": "Older Novel",
        "volume": 1.0,
    })
    _, newer_id = db.upsert_release({
        "guid": "jnovels-newer",
        "title": "Newer Novel Volume 2 Epub",
        "link": "https://jnovels.com/newer-novel-volume-2-epub/",
        "series_name": "Newer Novel",
        "volume": 2.0,
    })
    candidates = db.list_releases_missing_source_link(10)
    check(
        all("jnovels.com" in str(row["link"]) for row in candidates),
        f"backfill scope: {[row['link'] for row in candidates]}",
    )
    candidate_ids = [row["id"] for row in candidates]
    check(
        candidate_ids.index(newer_id) < candidate_ids.index(older_id),
        f"backfill newest first: {candidate_ids}",
    )
    check(db.set_release_source_link(release_id, "https://mirror.example/new") is True, "update setter")
    check(db.get_release_by_id(release_id)["source_link"] == "https://mirror.example/new", "updated")


def test_ui_exposes_source_buttons():
    base = os.path.join(_BASE, "backend", "app", "templates", "partials")
    for name in ("flat_release_row_chunk.html", "series_card_chunk.html"):
        source = open(os.path.join(base, name), encoding="utf-8").read()
        check("r.source_link" in source, f"{name} source button")
        check("'jnovels.com' in (r.link or '')" in source, f"{name} fetch available before backfill")
        check("rel=\"noopener noreferrer\"" in source, f"{name} noopener")
    settings = open(os.path.join(_BASE, "backend", "app", "templates", "settings.html"), encoding="utf-8").read()
    check("backfillSourceLinks" in settings, "settings backfill action")
    check("/api/maintenance/source-links/backfill" in settings, "settings endpoint")
    check("setting-epub-fetcher-url" in settings, "settings fetcher URL field")
    check("epub_fetcher_url" in settings, "settings fetcher payload")
    check("fetchEpub" in source, "per-release fetch action")


def main():
    test_extractor()
    test_persistence()
    test_ui_exposes_source_buttons()
    print("source-link suite PASSED")


if __name__ == "__main__":
    main()
