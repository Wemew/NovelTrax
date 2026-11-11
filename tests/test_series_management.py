#!/usr/bin/env python3
"""Regression coverage for the per-series management panel.

Verifies the Library-tab contract (releases + aliases + AniList titles), the
synonym split path, and alias re-mapping. Runs against a temporary SQLite DB;
no network, Calibre library, or running app is required.
"""

import asyncio
import json
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for path in (PROJECT_ROOT, BACKEND_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.database import Database
import app.scheduler as scheduler_module
import app.main as main_module


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def seed(db):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO series_tracking (name, authors, book_count, latest_index, "
            "is_monitored, display_title, owned_volumes_json, identity_locked, match_source) "
            "VALUES (?,?,?,?,?,?,?,?,?);",
            ("Mushoku Tensei", "Rifujin na Magonote", 3, 3.0, 1,
             "Mushoku Tensei: Jobless Reincarnation", '["1"]', 0, "calibre"),
        )
        cur.execute(
            "INSERT INTO series_tracking (name, book_count, is_monitored, "
            "owned_volumes_json) VALUES (?,?,?,?);",
            ("Alternate Target", 1, 1, '["2"]'),
        )
        cur.execute(
            "INSERT INTO releases (guid, title, series_name, volume, status, link) "
            "VALUES (?,?,?,?,?,?);",
            ("sm-1", "Mushoku Tensei Vol. 1", "Mushoku Tensei", 1, "IN_CALIBRE",
             "https://example.com/1"),
        )
        cur.execute(
            "INSERT INTO releases (guid, title, series_name, volume, status, link) "
            "VALUES (?,?,?,?,?,?);",
            ("sm-2", "Mushoku Tensei: Jobless Reincarnation Vol. 2",
             "Mushoku Tensei", 2, "REQUESTED", "https://example.com/2"),
        )
        conn.commit()


def test_static_contract():
    main_src = read(os.path.join(BACKEND_DIR, "app", "main.py"))
    series_src = read(os.path.join(BACKEND_DIR, "app", "templates", "series.html"))
    db_src = read(os.path.join(BACKEND_DIR, "app", "database.py"))

    for marker in (
        '@app.get("/api/series/{series_id}/management")',
        '@app.post("/api/series/{series_id}/identity")',
        '@app.post("/api/series/{series_id}/anilist")',
        '@app.post("/api/series/{series_id}/synonyms/spin-out")',
        '@app.post("/api/series/{series_id}/aliases/{alias_id}/retarget")',
    ):
        check(marker in main_src, "main.py is missing %r" % marker)
    for marker in (
        'id="series-manage-modal"',
        "function openSeriesManagement(",
        "function loadSeriesManagement(",
        "function manageSaveIdentity(",
        "series-manage-display-title",
        "series-manage-clear-anilist",
        "series-manage-anilist-input",
        "function manageSubmitAniList(",
        "function manageMapReleaseAniList(",
        "function manageSpinSynonym(",
        "function manageBeginAliasRetarget(",
        "mode: 'new_title'",
        "synonyms/spin-out",
        "/retarget",
    ):
        check(marker in series_src, "series.html is missing %r" % marker)
    for marker in (
        "def get_series_management(",
        "def spin_out_series_synonym(",
        "def retarget_series_alias(",
    ):
        check(marker in db_src, "database.py is missing %r" % marker)


def test_management_and_split():
    with tempfile.TemporaryDirectory(prefix="noveltrax_series_management_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        seed(db)
        series = db.get_series_by_name("Mushoku Tensei")
        check(series is not None, "seeded series is missing")
        alias = db.add_series_alias_override(
            "Mushoku Tensei: Jobless Reincarnation",
            "Mushoku Tensei",
            origin="manual",
            actor="test",
        )
        management = db.get_series_management(series["id"])
        check(management is not None, "management payload is missing")
        check(management["series"]["name"] == "Mushoku Tensei", "wrong series in payload")
        check(len(management["aliases"]) == 1, "expected one alias")
        check(len(management["releases"]) == 2, "expected two releases")
        check(management["releases"][0]["download_state"]["state"] in
              ("not_downloaded", "in_calibre", "downloaded", "downloading", "failed"),
              "release download state was not derived")

        result = db.spin_out_series_synonym(
            series["id"],
            alias_id=alias["id"],
            new_name="Jobless Reincarnation Split",
            move_matching_releases=True,
            actor="test",
        )
        check(result["status"] == "success", "split did not succeed: %r" % result)
        check(result["aliases_retargeted"] == 1, "alias was not retargeted")
        check(result["moved_releases"] == 1, "matching release was not moved")
        new_series = db.get_series_by_name("Jobless Reincarnation Split")
        check(new_series is not None, "split series was not created")
        moved = db.get_series_management(new_series["id"])["releases"]
        check(len(moved) == 1, "moved release is not under the new series")
        check(moved[0]["title"].endswith("Vol. 2"), "wrong release moved")
        check(moved[0]["series_pinned"] == 1, "moved release is not pinned")
        check(moved[0]["status"] == "REQUESTED", "moved release status was not recomputed")
        moved_row = db.get_release_by_id(moved[0]["id"])
        check(moved_row["missing_vols_json"] == "[2.0]",
              "moved release missing volumes were not recomputed: %r"
              % moved_row["missing_vols_json"])
        check(db.get_series_management(series["id"])["aliases"] == [],
              "alias mapping stayed on the old series")
        new_aliases = db.get_series_management(new_series["id"])["aliases"]
        check(len(new_aliases) == 1, "split alias is not registered on the new series")
        check(new_aliases[0]["source_name"] == "Mushoku Tensei: Jobless Reincarnation",
              "wrong alias source after split")
        check(new_aliases[0]["target_name"] == "Jobless Reincarnation Split",
              "alias does not point at the split series")
        from app.series_identity import resolve_series_title
        resolution = resolve_series_title(
            "Mushoku Tensei: Jobless Reincarnation",
            db.get_series_identity_rows(),
            db.get_series_alias_overrides(),
        )
        check(resolution.matched, "split synonym no longer resolves to a series")
        check(resolution.canonical_name == "Jobless Reincarnation Split",
              "future releases with the split synonym resolve to %r"
              % (resolution.canonical_name,))


def test_alias_retarget():
    with tempfile.TemporaryDirectory(prefix="noveltrax_alias_retarget_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        seed(db)
        series = db.get_series_by_name("Mushoku Tensei")
        alias = db.add_series_alias_override(
            "ReZero Spelling", "Mushoku Tensei", origin="manual", actor="test"
        )
        result = db.retarget_series_alias(
            series["id"], alias["id"], target_name="Alternate Target",
            move_matching_releases=False, actor="test",
        )
        check(result["status"] == "success", "retarget did not succeed: %r" % result)
        check(result["series_name"] == "Alternate Target", "wrong retarget target")
        retargeted = [a for a in db.get_series_alias_overrides()
                      if a["id"] == alias["id"]]
        check(retargeted and retargeted[0]["target_name"] == "Alternate Target",
              "alias target was not updated")


def test_bundle_split_status_and_identity_map():
    with tempfile.TemporaryDirectory(prefix="noveltrax_bundle_split_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking (name, owned_volumes_json, anilist_meta_json) "
                "VALUES (?, ?, ?);",
                ("Bundle Source", "[]", json.dumps({"synonyms": ["Bundle Source Alt"]})),
            )
            cur.execute(
                """
                INSERT INTO releases
                    (guid, title, series_name, volume, is_bundle, volumes_json,
                     status, link)
                VALUES (?,?,?,?,?,?,?,?);
                """,
                ("bundle-1", "Bundle Source Alt Vol. 1-3", "Bundle Source", None,
                 1, '[1,2,3]', "IN_CALIBRE", "https://example.com/bundle"),
            )
            conn.commit()
        series = db.get_series_by_name("Bundle Source")
        alias = db.add_series_alias_override(
            "Bundle Source Alt", "Bundle Source", origin="manual", actor="test"
        )
        result = db.spin_out_series_synonym(
            series["id"], alias_id=alias["id"], new_name="Bundle Source Alt",
            move_matching_releases=True, actor="test",
        )
        check(result["status"] == "success", "bundle split failed: %r" % result)
        new_id = result["series_id"]
        new_mgmt = db.get_series_management(new_id)
        check(len(new_mgmt["releases"]) == 1, "bundle release was not moved")
        moved = new_mgmt["releases"][0]
        check(moved["status"] == "REQUESTED",
              "multi-volume bundle was classified %r" % moved["status"])
        row = db.get_release_by_id(moved["id"])
        check(row["missing_vols_json"] == "[1.0, 2.0, 3.0]",
              "bundle missing volumes are wrong: %r" % row["missing_vols_json"])
        check(len(new_mgmt["aliases"]) == 1, "identity alias was not registered")
        check(new_mgmt["aliases"][0]["self_mapped"] is True,
              "identity alias was not flagged")
        from app.series_identity import resolve_series_title
        shadowed = resolve_series_title(
            "Bundle Source Alt",
            db.get_series_identity_rows(),
            db.get_series_alias_overrides(),
        )
        check(shadowed.matched, "identity map did not resolve the split spelling")
        check(shadowed.canonical_name == "Bundle Source Alt",
              "stale AniList spelling still resolves to %r"
              % (shadowed.canonical_name,))


def test_bundle_release_spin_out_status():
    with tempfile.TemporaryDirectory(prefix="noveltrax_bundle_release_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking (name, owned_volumes_json) VALUES (?, ?);",
                ("Bundle Old", "[]"),
            )
            cur.execute(
                """
                INSERT INTO releases
                    (guid, title, series_name, volume, is_bundle, volumes_json,
                     status, link)
                VALUES (?,?,?,?,?,?,?,?);
                """,
                ("bundle-release", "Bundle Old Vol. 1-3", "Bundle Old", None,
                 1, '[1,2,3]', "IN_CALIBRE", "https://example.com/bundle"),
            )
            conn.commit()
        manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM releases WHERE guid = 'bundle-release';")
            release_id = cur.fetchone()["id"]
        summary = manager.rebind_release(
            release_id, "new_title", target_name="Bundle Release Split"
        )
        check(summary["release_status"] == "REQUESTED",
              "bundle release split was classified %r" % summary["release_status"])


def test_display_identity_edit_and_anilist_clear():
    with tempfile.TemporaryDirectory(prefix="noveltrax_identity_edit_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking "
                "(name, display_title, anilist_id, anilist_meta_json) "
                "VALUES (?,?,?,?)",
                ("86-EIGHTY-SIX", "86—EIGHTY-SIX Alter", 999,
                 json.dumps({"synonyms": ["86 Alter"], "total_volumes": 2})),
            )
            conn.commit()
        series = db.get_series_by_name("86-EIGHTY-SIX")
        db.add_series_alias_override("86 Alter", "86-EIGHTY-SIX",
                                     origin="anilist", actor="anilist")
        db.cache_novel_metadata(
            "86-eighth-six",
            {"title": "86—EIGHTY-SIX Alter", "synopsis": "wrong", "source": "cache"},
        )
        updated = db.update_series_display_identity(
            series["id"], "86--Eighty-Six", clear_anilist=True, actor="test"
        )
        check(updated is not None, "identity edit returned no row")
        check(updated["display_title"] == "86--Eighty-Six",
              "display title was not saved")
        check(updated["anilist_id"] is None, "AniList id was not cleared")
        check(updated["anilist_meta_json"] in (None, ""),
              "AniList metadata was not cleared")
        check(updated["anilist_locked"] == 1,
              "explicit AniList clear did not lock automatic enrichment")
        check(db.get_series_alias_overrides() == [],
              "AniList-derived alias survived the clear")
        check(db.get_cached_novel_metadata("86-eighth-six") is None,
              "wrong metadata cache entry survived the clear")
        check(updated["name"] == "86-EIGHTY-SIX",
              "stable tracking name must never change")

        mapped = db.map_series_anilist(
            series["id"],
            {
                "anilist_id": 777,
                "title": "86--Eighty-Six",
                "english_title": "86--Eighty-Six",
                "canonical_title": "86--Eighty-Six",
                "synonyms": ["86--Eighty-Six", "Eighty Six"],
                "publication": {"format": "NOVEL", "status": "RELEASING",
                                "volumes": 13, "chapters": 0},
                "format": "NOVEL",
                "status": "RELEASING",
                "volumes": 13,
                "chapters": 0,
            },
            actor="test",
        )
        check(mapped is not None, "manual AniList mapping returned no row")
        check(mapped["anilist_id"] == 777, "manual AniList id was not stored")
        check(mapped["anilist_locked"] == 1, "manual mapping did not lock AniList")
        check(mapped["display_title"] == "86--Eighty-Six",
              "manual mapping did not apply the chosen title")
        aliases = db.get_series_alias_overrides()
        check(any(a["source_name"] == "Eighty Six" and a["target_name"] == "86-EIGHTY-SIX"
                  for a in aliases), "new AniList synonyms were not registered")


def test_anilist_url_parsing_and_id_query():
    from app.providers.metadata import NovelMetadataProvider

    check(NovelMetadataProvider.parse_anilist_id(
        "https://anilist.co/manga/123456/86--Eighty-Six/") == 123456,
        "AniList manga URL was not parsed")
    check(NovelMetadataProvider.parse_anilist_id(" 42 ") == 42,
          "bare AniList id was not parsed")
    check(NovelMetadataProvider.parse_anilist_id("not a link") is None,
          "invalid AniList input was accepted")

    original = NovelMetadataProvider.__dict__["_anilist_graphql"]

    def fake_graphql(cls, req_payload, label):
        check(req_payload.get("variables", {}).get("id") == 42,
              "id lookup did not send the numeric id")
        return {
            "data": {
                "Media": {
                    "id": 42,
                    "title": {"english": "86--Eighty-Six", "romaji": "86", "native": ""},
                    "synonyms": ["Eighty Six"],
                    "format": "NOVEL",
                    "status": "RELEASING",
                    "volumes": 13,
                    "chapters": 0,
                    "description": "Test",
                    "coverImage": {"large": "https://example.com/cover.jpg"},
                }
            }
        }

    NovelMetadataProvider._anilist_graphql = classmethod(fake_graphql)
    try:
        result = NovelMetadataProvider.query_anilist_by_id(42)
    finally:
        NovelMetadataProvider._anilist_graphql = original
    check(result is not None, "AniList id lookup returned no metadata")
    check(result["anilist_id"] == 42, "AniList id lookup did not preserve the id")
    check(result["english_title"] == "86--Eighty-Six",
          "AniList id lookup returned the wrong title")


def test_anilist_lock_blocks_auto_refresh():
    from app.providers.metadata import NovelMetadataProvider

    with tempfile.TemporaryDirectory(prefix="noveltrax_anilist_lock_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking (name, anilist_locked) VALUES (?, ?);",
                ("86-EIGHTY-SIX", 1),
            )
            conn.commit()
        series = db.get_series_by_name("86-EIGHTY-SIX")
        manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
        calls = []
        original_fetch = NovelMetadataProvider.__dict__["fetch_series_metadata"]
        original_by_id = NovelMetadataProvider.__dict__["query_anilist_by_id"]

        def fake_fetch(cls, title):
            calls.append(("search", title))
            return None

        def fake_by_id(cls, anilist_id):
            calls.append(("id", anilist_id))
            return None

        NovelMetadataProvider.fetch_series_metadata = classmethod(fake_fetch)
        NovelMetadataProvider.query_anilist_by_id = classmethod(fake_by_id)
        try:
            enrichment = manager.enrich_tracked_series_anilist(limit=5)
            refresh = manager.refresh_series_metadata(series["id"])
        finally:
            NovelMetadataProvider.fetch_series_metadata = original_fetch
            NovelMetadataProvider.query_anilist_by_id = original_by_id

        check(enrichment.get("synced") == 0,
              "locked series was synchronised by automatic enrichment")
        check(calls == [], "locked series still queried AniList: %r" % calls)
        check("manually cleared/pinned" in (refresh.get("message") or ""),
              "locked refresh did not explain the manual pin: %r"
              % refresh.get("message"))


def test_locked_refresh_uses_stored_anilist_id():
    from app.providers.metadata import NovelMetadataProvider

    with tempfile.TemporaryDirectory(prefix="noveltrax_anilist_pin_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking "
                "(name, display_title, anilist_id, anilist_locked) "
                "VALUES (?,?,?,?)",
                ("86-EIGHTY-SIX", "86—EIGHTY-SIX Alter", 777, 1),
            )
            conn.commit()
        series = db.get_series_by_name("86-EIGHTY-SIX")
        manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
        calls = []
        original_fetch = NovelMetadataProvider.__dict__["fetch_series_metadata"]
        original_by_id = NovelMetadataProvider.__dict__["query_anilist_by_id"]

        def fake_fetch(cls, title):
            calls.append(("search", title))
            return {"title": "86 Alter", "english_title": "86 Alter",
                    "synonyms": ["86 Alter"], "anilist_id": 999}

        def fake_by_id(cls, anilist_id):
            calls.append(("id", anilist_id))
            return {
                "anilist_id": anilist_id,
                "title": "86--Eighty-Six",
                "english_title": "86--Eighty-Six",
                "canonical_title": "86--Eighty-Six",
                "synonyms": ["Eighty Six"],
                "format": "NOVEL",
                "status": "RELEASING",
                "volumes": 13,
                "chapters": 0,
            }

        NovelMetadataProvider.fetch_series_metadata = classmethod(fake_fetch)
        NovelMetadataProvider.query_anilist_by_id = classmethod(fake_by_id)
        try:
            result = manager.refresh_series_metadata(series["id"])
        finally:
            NovelMetadataProvider.fetch_series_metadata = original_fetch
            NovelMetadataProvider.query_anilist_by_id = original_by_id

        check(calls == [("id", 777)],
              "pinned refresh searched by title instead of the stored id: %r" % calls)
        updated = db.get_series_by_name("86-EIGHTY-SIX")
        check(updated["anilist_id"] == 777, "pinned refresh lost the AniList id")
        check(updated["display_title"] == "86--Eighty-Six",
              "pinned refresh did not apply the exact display title")
        check("Alter" not in (updated["anilist_meta_json"] or ""),
              "pinned refresh retained the wrong Alter metadata")
        check(result.get("status") == "success", "pinned refresh failed: %r" % result)


def test_anilist_mapping_route():
    from app.providers.metadata import NovelMetadataProvider

    with tempfile.TemporaryDirectory(prefix="noveltrax_anilist_route_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking (name, display_title, anilist_id) "
                "VALUES (?,?,?)",
                ("86-EIGHTY-SIX", "86—EIGHTY-SIX Alter", 999),
            )
            conn.commit()
        series = db.get_series_by_name("86-EIGHTY-SIX")
        meta = {
            "anilist_id": 777,
            "title": "86--Eighty-Six",
            "english_title": "86--Eighty-Six",
            "canonical_title": "86--Eighty-Six",
            "synonyms": ["Eighty Six"],
            "format": "NOVEL",
            "status": "RELEASING",
            "volumes": 13,
            "chapters": 0,
        }
        original_db = main_module.db
        original_provider = NovelMetadataProvider.__dict__["query_anilist_by_id"]
        main_module.db = db
        NovelMetadataProvider.query_anilist_by_id = classmethod(
            lambda cls, anilist_id: dict(meta, anilist_id=int(anilist_id))
        )
        try:
            response = asyncio.run(main_module.api_map_series_anilist(
                series["id"],
                {"url_or_id": "https://anilist.co/manga/777/86--Eighty-Six/"},
            ))
        finally:
            main_module.db = original_db
            NovelMetadataProvider.query_anilist_by_id = original_provider
        check(response.status_code == 200, "AniList mapping route did not return 200")
        updated = db.get_series_by_name("86-EIGHTY-SIX")
        check(updated["anilist_id"] == 777, "route did not store the AniList id")
        check(updated["anilist_locked"] == 1, "route did not pin the AniList entry")
        check(updated["display_title"] == "86--Eighty-Six",
              "route did not apply the chosen display title")


def test_publication_metadata_respects_anilist_lock():
    from app.providers.metadata import NovelMetadataProvider

    with tempfile.TemporaryDirectory(prefix="noveltrax_publication_lock_", ignore_cleanup_errors=True) as tmp:
        db = Database(os.path.join(tmp, "test.db"))
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO series_tracking (name, anilist_locked, anilist_meta_json) "
                "VALUES (?,?,?)",
                ("86 Cleared", 1, ""),
            )
            cur.execute(
                "INSERT INTO series_tracking (name, anilist_locked, anilist_id, "
                "anilist_meta_json) VALUES (?,?,?,?)",
                ("86 Pinned", 1, 98610, ""),
            )
            conn.commit()
        cleared = db.get_series_by_name("86 Cleared")
        pinned = db.get_series_by_name("86 Pinned")
        manager = scheduler_module.TaskManager(db, "/nonexistent", data_dir=tmp)
        calls = []
        original_fetch = NovelMetadataProvider.__dict__["fetch_series_metadata"]
        original_by_id = NovelMetadataProvider.__dict__["query_anilist_by_id"]

        def fake_fetch(cls, title):
            calls.append(("search", title))
            return {"title": "86 Alter", "english_title": "86 Alter",
                    "synonyms": ["86 Alter"], "anilist_id": 162703,
                    "format": "NOVEL", "status": "RELEASING", "volumes": 2,
                    "chapters": 0}

        def fake_by_id(cls, anilist_id):
            calls.append(("id", anilist_id))
            return {"anilist_id": anilist_id, "title": "86--Eighty-Six",
                    "english_title": "86--Eighty-Six", "synonyms": ["Eighty Six"],
                    "format": "NOVEL", "status": "RELEASING", "volumes": 13,
                    "chapters": 0}

        NovelMetadataProvider.fetch_series_metadata = classmethod(fake_fetch)
        NovelMetadataProvider.query_anilist_by_id = classmethod(fake_by_id)
        try:
            cleared_result = manager.publication_metadata_for("86 Cleared")
            pinned_result = manager.publication_metadata_for("86 Pinned")
        finally:
            NovelMetadataProvider.fetch_series_metadata = original_fetch
            NovelMetadataProvider.query_anilist_by_id = original_by_id

        check(cleared_result is None,
              "cleared locked series returned publication metadata: %r" % cleared_result)
        check(pinned_result is not None and pinned_result.get("volumes") == 13,
              "pinned series did not use its exact AniList id: %r" % pinned_result)
        check(("search", "86 Cleared") not in calls,
              "cleared locked series fell through to a title search: %r" % calls)
        check(("search", "86 Pinned") not in calls,
              "pinned series fell through to a title search: %r" % calls)
        check(("id", 98610) in calls, "pinned series did not query its stored id")

        # The publication store must not overwrite a locked row's empty meta.
        db.store_series_publication_metadata(
            pinned["id"],
            {"format": "MANGA", "status": "CANCELLED", "volumes": 1, "chapters": 0},
        )
        after = db.get_series_by_id(pinned["id"])
        check(after["anilist_meta_json"] in (None, ""),
              "publication store overwrote a pinned series")
        check(cleared["anilist_id"] is None, "cleared series unexpectedly gained an id")


def main():
    print("Series management regression suite")
    test_static_contract()
    test_management_and_split()
    test_alias_retarget()
    test_bundle_split_status_and_identity_map()
    test_bundle_release_spin_out_status()
    test_display_identity_edit_and_anilist_clear()
    test_anilist_url_parsing_and_id_query()
    test_anilist_lock_blocks_auto_refresh()
    test_locked_refresh_uses_stored_anilist_id()
    test_anilist_mapping_route()
    test_publication_metadata_respects_anilist_lock()
    print("series management regression suite PASSED")


if __name__ == "__main__":
    main()
