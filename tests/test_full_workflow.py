import sys
import os
import shutil

# Add base directory and backend directory to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'backend'))

from tests.mock_calibre import create_mock_calibre_db, add_book_to_mock_calibre
from app.database import Database
from app.calibre.scanner import CalibreScanner
from app.calibre.matcher import CalibreMatcher
from app.parser import extract_series_and_volume

def run_e2e_test():
    import tempfile
    test_dir = os.path.join(tempfile.gettempdir(), "noveltrax_test_run")
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir)
        except Exception:
            pass
    os.makedirs(test_dir, exist_ok=True)

    calibre_db_file = os.path.join(test_dir, "calibre", "metadata.db")
    app_db_file = os.path.join(test_dir, "app", "noveltrax.db")

    print("=" * 60)
    print("STEP 1: CREATING REALISTIC MOCK CALIBRE LIBRARY")
    print("=" * 60)
    create_mock_calibre_db(calibre_db_file)
    print(f"Mock Calibre created at {calibre_db_file}")

    print("\n" + "=" * 60)
    print("STEP 2: TESTING ZERO-LOCK CALIBRE SCANNER")
    print("=" * 60)
    scanner = CalibreScanner(os.path.dirname(calibre_db_file))
    summary = scanner.get_library_summary()
    print("Library Summary:", summary)
    assert summary["total_books"] == 39  # 18 + 15 + 6
    assert summary["total_series"] == 3
    assert summary["total_authors"] == 3

    series_list = scanner.get_all_series()
    for s in series_list:
        print(f"  Series: {s['series_name']} | Books: {s['book_count']} | Latest: Vol {s['latest_index']}")

    print("\n" + "=" * 60)
    print("STEP 3: INITIALIZING APP DATABASE & AUTO-POPULATING SERIES")
    print("=" * 60)
    app_db = Database(app_db_file)
    synced_count = app_db.sync_calibre_series(series_list)
    print(f"Auto-populated {synced_count} series into NovelTrax database.")

    db_series = app_db.get_all_series()
    print("App Series Table:")
    for s in db_series:
        print(f"  [ID {s['id']}] {s['name']} (Monitored: {bool(s['is_monitored'])}, Owned: {s['book_count']})")
    assert len(db_series) == 3

    print("\n" + "=" * 60)
    print("STEP 4: SIMULATING J-NOVELS FEED SCRAPER DETECTING RELEASES")
    print("=" * 60)
    books_list = scanner.get_all_books()
    matcher = CalibreMatcher(series_list, books_list)

    incoming_feed_items = [
        "DanMachi Volume 19 Epub",                               # Missing! Latest owned is 18
        "Overlord Volume 15 Epub",                               # Already in Calibre!
        "Dungeon Crawler Carl Book 7 - This Inevitable Ruin",     # Missing! Latest owned is 6
        "Random Unrelated Light Novel Volume 1 Epub"              # Not in tracked series
    ]

    for raw_title in incoming_feed_items:
        parsed = extract_series_and_volume(raw_title)
        res = matcher.check_release({
            "title": raw_title,
            "series_name": parsed["series_name"],
            "volume": parsed["volume"]
        }, auto_request_monitored=True)

        print(f"Incoming: '{raw_title}'")
        print(f"  Parsed Series: '{parsed['series_name']}' | Vol: {parsed['volume']}")
        print(f"  Evaluation: Status = {res['status']} | Reason: {res['reason']}")

        # Store in database
        import hashlib
        guid = hashlib.sha256(raw_title.encode()).hexdigest()[:16]
        app_db.upsert_release({
            "guid": guid,
            "title": raw_title,
            "series_name": parsed["series_name"],
            "volume": parsed["volume"],
            "link": f"https://jnovels.com/?p={guid}",
            "source_feed_name": "J-Novels EPUB Releases",
            "status": res["status"],
            "calibre_matched_book_id": res["matched_book_id"]
        })

    # Check pending requests in DB
    pending = app_db.get_pending_requests()
    print(f"\nTotal Pending Requests in DB: {len(pending)}")
    for p in pending:
        print(f"  - WANTED: {p['title']} (Series: {p['series_name']}, Vol: {p['volume']})")
    assert len(pending) == 2  # DanMachi Vol 19 and DCC Book 7

    print("\n" + "=" * 60)
    print("STEP 5: SIMULATING USER ADDING 'DanMachi Volume 19' TO CALIBRE")
    print("=" * 60)
    print("Adding 'DanMachi, Vol. 19' to Calibre metadata.db...")
    add_book_to_mock_calibre(calibre_db_file, "DanMachi, Vol. 19", series_id=1, series_index=19.0)

    print("Rescanning Calibre library...")
    scanner_rescan = CalibreScanner(os.path.dirname(calibre_db_file))
    updated_series = scanner_rescan.get_all_series()
    updated_books = scanner_rescan.get_all_books()

    # Re-evaluate pending requests against updated Calibre catalog
    matcher2 = CalibreMatcher(updated_series, updated_books)
    resolved = matcher2.resolve_pending_requests(app_db.get_pending_requests())
    print(f"Newly Acquired / Auto-Resolved Books: {len(resolved)}")
    for r in resolved:
        print(f"  ✓ RESOLVED: '{r['title']}' -> Matched Calibre Book #{r['matched_book_id']} ({r['reason']})")
        app_db.update_release_status(r["release_id"], status="IN_CALIBRE", matched_book_id=r["matched_book_id"])

    # Check pending requests again
    remaining_pending = app_db.get_pending_requests()
    print(f"\nRemaining Pending Requests: {len(remaining_pending)}")
    for p in remaining_pending:
        print(f"  - WANTED: {p['title']}")

    assert len(remaining_pending) == 1
    assert remaining_pending[0]["series_name"] == "Dungeon Crawler Carl"
    print("\nSUCCESS: DanMachi Vol 19 was automatically removed from pending requests!")

    print("\n" + "=" * 60)
    print("STEP 6: TESTING NEW SERIES DISCOVERY (VOL. 1 & STANDALONES)")
    print("=" * 60)
    # Add a Volume 1 discovery release
    import hashlib
    v1_title = "Solo Leveling Volume 1 Epub"
    v1_guid = hashlib.sha256(v1_title.encode()).hexdigest()[:16]
    app_db.upsert_release({
        "guid": v1_guid,
        "title": v1_title,
        "series_name": "Solo Leveling",
        "volume": 1.0,
        "link": "https://jnovels.com/solo-leveling-vol-1/",
        "source_feed_name": "J-Novels EPUB Releases",
        "status": "NEW"
    })

    # Add an unnumbered standalone release
    st_title = "Three Days of Happiness Epub"
    st_guid = hashlib.sha256(st_title.encode()).hexdigest()[:16]
    app_db.upsert_release({
        "guid": st_guid,
        "title": st_title,
        "series_name": "Three Days of Happiness",
        "volume": None,
        "link": "https://jnovels.com/three-days-of-happiness/",
        "source_feed_name": "J-Novels EPUB Releases",
        "status": "NEW"
    })

    discovered = app_db.get_discovery_releases()
    print(f"Discovered potential new series: {len(discovered)}")
    for d in discovered:
        print(f"  - DISCOVERED: '{d['title']}' (Series: {d['series_name']}, Vol: {d['volume']})")
    assert len(discovered) >= 2

    # User tracks "Solo Leveling"
    print("\nUser clicks 'Track Series' on Solo Leveling...")
    app_db.track_discovered_series("Solo Leveling")
    # Verify Solo Leveling is now in tracked series and in pending requests
    tracked_series = [s["name"] for s in app_db.get_all_series(monitored_only=True)]
    assert "Solo Leveling" in tracked_series
    print("✓ 'Solo Leveling' is now actively monitored!")

    # Verify it is now removed from discovery (since it's monitored)
    discovered_after_track = [d["series_name"] for d in app_db.get_discovery_releases()]
    assert "Solo Leveling" not in discovered_after_track
    print("✓ 'Solo Leveling' moved from Discovery to Active Tracking.")

    print("\n" + "=" * 60)
    print("STEP 7: TESTING SERIES BLACKLISTING")
    print("=" * 60)
    # User blacklists "Three Days of Happiness"
    print("User blacklists 'Three Days of Happiness'...")
    app_db.add_to_blacklist("Three Days of Happiness", reason="Not my genre")
    assert app_db.is_series_blacklisted("Three Days of Happiness")
    print("✓ Series is recorded in blacklist table!")

    # Verify it disappeared from Discovery
    discovered_after_bl = [d["series_name"] for d in app_db.get_discovery_releases()]
    assert "Three Days of Happiness" not in discovered_after_bl
    print("✓ Blacklisted series is completely hidden from Discovery.")

    # Simulate incoming release for the blacklisted series (Vol 2)
    bl_vol2_title = "Three Days of Happiness Volume 2 Epub"
    bl_vol2_guid = hashlib.sha256(bl_vol2_title.encode()).hexdigest()[:16]
    is_bl = app_db.is_series_blacklisted("Three Days of Happiness")
    status_to_set = "BLACKLISTED" if is_bl else "REQUESTED"
    app_db.upsert_release({
        "guid": bl_vol2_guid,
        "title": bl_vol2_title,
        "series_name": "Three Days of Happiness",
        "volume": 2.0,
        "link": "https://jnovels.com/tdoh-vol-2/",
        "status": status_to_set
    })

    # Verify no pending request was created
    for p in app_db.get_pending_requests():
        assert p["series_name"] != "Three Days of Happiness"
    print("✓ Future releases for blacklisted series were blocked from generating requests!")

    # Test removing from blacklist (editable)
    bl_list = app_db.get_blacklisted_series()
    target_bl = next(b for b in bl_list if b["name"] == "Three Days of Happiness")
    app_db.remove_from_blacklist(target_bl["id"])
    assert not app_db.is_series_blacklisted("Three Days of Happiness")
    print("✓ Successfully removed/restored series from Blacklist!")

    print("\n" + "=" * 60)
    print("STEP 8: TESTING SECURITY HARDENING & DEFENSE-IN-DEPTH")
    print("=" * 60)
    from app.covers import is_safe_public_url, detect_valid_image

    print("Testing SSRF URL validation:")
    blocked_urls = [
        "http://127.0.0.1/image.jpg",
        "http://localhost/test.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/admin.jpg",
        "http://10.0.0.5/secret.webp",
        "http://172.17.0.1:8080/internal",
        "file:///etc/passwd",
        "ftp://internal.lan/file.jpg",
        "javascript:alert(1)"
    ]
    for url in blocked_urls:
        assert not is_safe_public_url(url), f"Failed to block unsafe URL: {url}"
        print(f"  ✓ Blocked unsafe URL: {url}")

    print("\nTesting image magic-byte verification:")
    assert detect_valid_image(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00") == "image/jpeg"
    print("  ✓ Allowed valid JPEG binary header")

    assert detect_valid_image(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10") == "image/png"
    print("  ✓ Allowed valid PNG binary header")

    assert detect_valid_image(b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00") == "image/webp"
    print("  ✓ Allowed valid WEBP binary header")

    assert detect_valid_image(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04") == "image/gif"
    print("  ✓ Allowed valid GIF binary header")

    # Reject non-images and script payloads
    malicious_payloads = [
        b"<svg onload=alert(1)>",
        b"<html><head><script>evil()</script></head><body>hello</body></html>",
        b"<?php system($_GET['cmd']); ?>",
        b"#!/bin/bash\nrm -rf /",
        b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00",  # Windows PE binary
        b"Short text"
    ]
    for payload in malicious_payloads:
        assert detect_valid_image(payload) is None, f"Failed to reject malicious payload: {payload[:20]}"
        print(f"  ✓ Rejected unsafe content payload: {payload[:20]!r}")

    print("\n" + "=" * 60)
    print("STEP 9: TESTING SYNOPSIS ENRICHMENT & MASTER INDEX DISCOVERY")
    print("=" * 60)
    # Test release detail update with synopsis and canonical original post
    v1_releases = app_db.get_discovery_releases()
    test_rel = v1_releases[0]
    sample_synopsis = "A mysterious gate opens in Tokyo, revealing a fantasy world filled with monsters and magic."
    canonical_hub = "https://jnovels.com/gate-thus-the-jsdf-fought-there-light-novel-epub/"

    app_db.update_release_details(
        test_rel["id"],
        description=sample_synopsis,
        series_link=canonical_hub
    )
    with app_db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT description, series_link FROM releases WHERE id = ?;", (test_rel["id"],))
        updated_row = cur.fetchone()
        assert updated_row["description"] == sample_synopsis
        assert updated_row["series_link"] == canonical_hub
    print(f"  ✓ Successfully updated release #{test_rel['id']} with synopsis & original post link")

    # Test ReleaseItem with source_feed_name
    from app.providers.base import ReleaseItem
    test_item = ReleaseItem(
        title="Mushoku Tensei (Light Novel)",
        link="https://jnovels.com/mushoku-tensei-epub/",
        series_name="Mushoku Tensei",
        volume=1.0,
        series_link="https://jnovels.com/mushoku-tensei-epub/",
        source_feed_name="J-Novels Master Index"
    )
    assert test_item.source_feed_name == "J-Novels Master Index"
    item_dict = test_item.to_dict()
    assert item_dict["source_feed_name"] == "J-Novels Master Index"
    is_new, rel_id = app_db.upsert_release(item_dict)
    assert rel_id > 0
    print("  ✓ ReleaseItem successfully handles source_feed_name and persists cleanly to database")

    # Test parse_description_from_soup with #editdescription
    from bs4 import BeautifulSoup
    from app.providers.jnovels import JNovelsProvider

    sample_user_html = """
    <article class="post">
    <h4>Description</h4>
    <div id="editdescription">
    <p>Even someone like me discovered a way to find a girlfriend!<br/>
    The unexpected development in this high school love comedy will bring a smile to your mouth!!</p>
    <p>With my friends getting attached one after another, I became the last bachelor within my group. As I was sighing and lamenting such a high school life, an advertising email about a latest game app was sent to my phone. This game could actually guide the player to meet with girls by predicting the future——</p>
    <p>Was this true? If it was, please let me meet with the daughter of the capitalist family that I looked up to, Mochizuki Rika, and the innocent model student Nakajima Konoha. In order to meet the heroines within the in-game time limit of five days, the curtain was pulled to the daily struggles with the game itself.</p>
    </div>
    <img class="wp-post-image" src="https://jnovels.com/wp-content/uploads/2019/06/heroine-cover.jpg" />
    </article>
    """
    soup_user = BeautifulSoup(sample_user_html, "html.parser")
    desc_result = JNovelsProvider.parse_description_from_soup(soup_user)
    assert desc_result is not None
    assert "Even someone like me discovered a way to find a girlfriend!" in desc_result
    assert "Mochizuki Rika" in desc_result
    cover_result = JNovelsProvider.parse_cover_from_soup(soup_user)
    assert cover_result == "https://jnovels.com/wp-content/uploads/2019/06/heroine-cover.jpg"
    print("  ✓ JNovelsProvider correctly extracts full description from #editdescription and novel cover image")

    # Test edge case 1: unlabelled narrative description paragraphs
    sample_edge_1 = """
    <article class="post">
    <p>You can now download YAKIMOCHI NO KOTAE light novel in (.epub) Format</p>
    <p>A novel adaption of HoneyWorks “Yakimochi no Kotae (A Solution for Jealousy)“ about Mochizuki Souta (nickname: Mochita), a third-year at Sakuragaoka High School, who holds long-standing unrequited feelings for her the air-headed pretty girl, Akari, who’s the president of the Art Club.</p>
    <p>As the Art Club helps the Film Club with making a movie, he musters up the courage to confess and she actually accepts?! But when they accidentally witness Natsuki and Haruki’s “confession,” things start to get complicated….?<br/>
    <b>Associated Names –</b><br/>Confession Rehearsal<br/>A Solution for Jealousy</p>
    <p><b>Genre –<br/>Romance, School Life.</b></p>
    <ol><li><b>VOLUME 01 — DOWNLOAD</b></li></ol>
    </article>
    """
    desc_1 = JNovelsProvider.parse_description_from_soup(BeautifulSoup(sample_edge_1, "html.parser"))
    assert desc_1 is not None
    assert "Mochizuki Souta" in desc_1
    assert "Associated Names" not in desc_1
    print("  ✓ Edge case 1 passed: narrative paragraph detection cleanly isolates synopsis without associated names")

    # Test edge case 2: <b>Description</b><br/> with <b>Associated Names</b> in next <p>
    sample_edge_2 = """
    <article class="post">
    <p><b>Description</b><br/>
    Due to the schemes of the mysterious existence “Demon God”, a young man, Nakajima Ryou, was reincarnated in another world as the Demon King Vermudol. However, the place that he had gotten down to was, a Mazoku continent that had grown wild at the height of chaos.</p>
    <p><b>Associated Names</b><br/>
    An Easy Job of Only Needing to be Defeated by the Hero<br/>
    YHKO</p>
    </article>
    """
    desc_2 = JNovelsProvider.parse_description_from_soup(BeautifulSoup(sample_edge_2, "html.parser"))
    assert desc_2 is not None
    assert "Demon God" in desc_2
    assert "Nakajima Ryou" in desc_2
    assert "Associated Names" not in desc_2
    print("  ✓ Edge case 2 passed: inline <b>Description</b><br/> correctly parsed without picking up Associated Names")

    # Test edge case 3: Nested <p><span><b>Synopsis</b></span><br/>...
    sample_edge_3 = """
    <article class="post">
    <p><span style="color: #ff0000;"><b>Synopsis</b></span><br/>
    A college student spends an evening out, unwittingly attracting the attention of various men whose paths she crosses. One in particular, an upperclassman who has been nursing a crush on her for some time, has chosen this night to make his true feelings known.<br/>
    Will the two come together, or will this girl just keep on walking…?</p>
    <p><b>Associated Titles</b><br/>The Night Is Short, Walk on Girl</p>
    </article>
    """
    desc_3 = JNovelsProvider.parse_description_from_soup(BeautifulSoup(sample_edge_3, "html.parser"))
    assert desc_3 is not None
    assert "A college student spends an evening out" in desc_3
    assert "Associated Titles" not in desc_3
    print("  ✓ Edge case 3 passed: deeply nested span/b Synopsis heading correctly parsed")

    # Edge Case 4: Post with no synopsis but user comments (e.g. The Devil is a Part-Timer) -> Must NOT pull comments
    sample_edge_4 = """
    <article class="post">
    <div class="post-content">
        <p><b>You can now Download The Devil is a Part Timer Light Novel PDF volumes in (.epub) Format</b></p>
        <ol>
            <li>VOLUME 01 ———– <a href="http://example.com/1">DOWNLOAD</a></li>
            <li>VOLUME 02 ———– <a href="http://example.com/2">DOWNLOAD</a></li>
        </ol>
    </div>
    <div class="comments-area" id="comments">
        <div class="comment">
            <p>So. . .apparently the link for Vol 2 actually leads to 10!</p>
        </div>
        <div class="comment">
            <p>When are you going to upload volume 16 bruh?</p>
        </div>
    </div>
    </article>
    """
    desc_4 = JNovelsProvider.parse_description_from_soup(BeautifulSoup(sample_edge_4, "html.parser"))
    assert desc_4 is None, f"Expected None for post with no description, but got: {desc_4}"
    print("  ✓ Edge case 4 passed: pages without description strictly ignore comments and return None")

    # Edge Case 5: Post with expander div synopsis + comments (e.g. The Disabled Tyrant's Pet Fish)
    sample_edge_5 = """
    <article class="post">
    <div class="post-content">
        <p>You can now Download The Disabled Tyrant’s Beloved Pet Fish Light Novel</p>
        <h2><span style="color: #ff0000;"><strong>Synopsis</strong></span></h2>
        <div class="a-expander-content">
            <div id="synopsis">
                When Li Yu falls asleep reading a webnovel about a ruthless, mute tyrant, he wakes up inside the world of the novel as a fish!
            </div>
            Associated Names: Can Ji Bao Jun
        </div>
        <ol>
            <li>VOLUME 01 ———– <a href="http://example.com/1">DOWNLOAD</a></li>
        </ol>
    </div>
    <div id="comments">
        <div class="wpd-comment">
            <p>Pretty sure vol 2 translation is up and ty!</p>
        </div>
    </div>
    </article>
    """
    desc_5 = JNovelsProvider.parse_description_from_soup(BeautifulSoup(sample_edge_5, "html.parser"))
    assert desc_5 is not None
    assert "When Li Yu falls asleep" in desc_5
    assert not desc_5.startswith("\n")
    assert "Pretty sure vol 2" not in desc_5
    assert "Associated Names" not in desc_5
    print("  ✓ Edge case 5 passed: expander div synopsis cleanly extracted without comments or leading newlines")

    # STEP 9B: Test NovelMetadataProvider (Kitsu / AniList query and title cleaning)
    from app.providers.metadata import NovelMetadataProvider
    assert NovelMetadataProvider.clean_search_title("The Devil is a Part-Timer all volumes EPUB") == "The Devil is a Part-Timer"
    assert NovelMetadataProvider.clean_search_title("DanMachi Volume 19 Epub") == "DanMachi"
    assert NovelMetadataProvider.clean_search_title("Overlord Volume 16") == "Overlord"
    assert NovelMetadataProvider.clean_search_title("Solo Leveling Volume 1 Epub") == "Solo Leveling"
    assert NovelMetadataProvider.clean_novel_title("The Disabled Tyrant's Beloved Pet Fish (Novel)") == "The Disabled Tyrant's Beloved Pet Fish"
    print("  ✓ NovelMetadataProvider: title normalizer cleanly strips release cruft and prioritizes clean English titles")

    # Verify release details update with English series name and title (no light novel suffix)
    app_db.update_release_details(
        6,
        description="Official English synopsis from AniList.",
        series_name="Is It Wrong to Try to Pick Up Girls in a Dungeon?",
        title="Is It Wrong to Try to Pick Up Girls in a Dungeon?",
        cover_url="https://s4.anilist.co/danmachi-large.jpg"
    )
    with app_db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT series_name, title, description, cover_url FROM releases WHERE id = 6;")
        r6 = cur.fetchone()
        assert r6["series_name"] == "Is It Wrong to Try to Pick Up Girls in a Dungeon?"
        assert r6["title"] == "Is It Wrong to Try to Pick Up Girls in a Dungeon?"
        assert r6["description"] == "Official English synopsis from AniList."
    print("  ✓ Database correctly stores English series_name, title, and synopsis without (Light Novel) noise")

    # STEP 9C: Test Discovery Series Deduplication & (Light Novel) Removal
    app_db.upsert_release({
        "guid": "test_kubishime_1",
        "title": "Strangulation: Kubishime Romanticist (Light Novel)",
        "series_name": "Strangulation: Kubishime Romanticist (Light Novel)",
        "volume": 1.0,
        "link": "https://jnovels.com/strangulation-1/",
        "status": "NEW",
        "description": "Synopsis 1"
    })
    app_db.upsert_release({
        "guid": "test_kubishime_2",
        "title": "Strangulation: Kubishime Romanticist",
        "series_name": "Strangulation: Kubishime Romanticist",
        "volume": 1.0,
        "link": "https://jnovels.com/strangulation-2/",
        "status": "NEW",
        "cover_url": "https://jnovels.com/strangulation-cover.jpg"
    })

    disc_releases = app_db.get_discovery_releases()
    kubishime_cards = [d for d in disc_releases if "strangulation" in (d.get("series_name") or "").lower()]
    assert len(kubishime_cards) == 1, f"Expected exactly 1 deduplicated card for Kubishime, but got {len(kubishime_cards)}"
    assert "(light novel)" not in kubishime_cards[0]["series_name"].lower()
    assert "(light novel)" not in kubishime_cards[0]["title"].lower()
    assert kubishime_cards[0]["series_name"] == "Strangulation: Kubishime Romanticist"
    print("  ✓ Discovery deduplication passed: Duplicate series merged and (Light Novel) tags removed")

    # Verify tracking carries over description and cover
    app_db.track_discovered_series("Is It Wrong to Try to Pick Up Girls in a Dungeon?", release_id=6)
    with app_db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT description, cover_url FROM series_tracking WHERE name = 'Is It Wrong to Try to Pick Up Girls in a Dungeon?';")
        st = cur.fetchone()
        assert st is not None
        assert st["description"] == "Official English synopsis from AniList."
        assert st["cover_url"] == "https://s4.anilist.co/danmachi-large.jpg"
    print("  ✓ track_discovered_series carries over metadata description and cover into series_tracking")

    # Verify fallback integration when post has no synopsis
    sample_empty_post = """
    <article class="post">
        <h1>The Devil is a Part-Timer all volumes EPUB</h1>
        <div class="post-content">
            <p>You can now download the novel.</p>
        </div>
    </article>
    """
    # Mock soup parser without network
    soup_empty = BeautifulSoup(sample_empty_post, "html.parser")
    assert JNovelsProvider.parse_description_from_soup(soup_empty) is None
    print("  ✓ Empty post returns None from soup parser, ready for external metadata fallback")

    app_db.update_series_details(
        1,
        description="Bell Cranel is a solo adventurer in Orario.",
        cover_url="https://jnovels.com/danmachi-cover.jpg",
        website_url="https://jnovels.com/danmachi-light-novel-epub/"
    )
    all_s = app_db.get_all_series()
    s_danmachi = [s for s in all_s if s["id"] == 1][0]
    assert s_danmachi["description"] == "Bell Cranel is a solo adventurer in Orario."
    print("  ✓ Successfully verified series_tracking description persistence")

    print("\n" + "=" * 60)
    print("STEP 10: TESTING DEBUG & MAINTENANCE TOOLS")
    print("=" * 60)
    # Test setting retrieval for backfill_max_pages
    app_db.set_setting("backfill_max_pages", "45")
    assert app_db.get_setting("backfill_max_pages") == "45"
    print("  ✓ backfill_max_pages setting successfully saved and retrieved")

    # Test clear_all_descriptions
    cleared_desc_count = app_db.clear_all_descriptions()
    assert cleared_desc_count >= 1
    with app_db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM releases WHERE description IS NOT NULL AND description != '';")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM series_tracking WHERE description != '';")
        assert cur.fetchone()[0] == 0
    print(f"  ✓ clear_all_descriptions successfully cleared descriptions across {cleared_desc_count} records")

    # Test clear_discovered_series
    initial_requests = len(app_db.get_pending_requests())
    cleared_disc = app_db.clear_discovered_series()
    assert cleared_disc >= 1
    # Ensure active wishlist requests were NOT affected
    assert len(app_db.get_pending_requests()) == initial_requests
    assert len(app_db.get_discovery_releases()) == 0
    print(f"  ✓ clear_discovered_series successfully purged {cleared_disc} unreviewed discovery items without affecting requests")

    # Test CoverCache clear_cache
    from app.covers import CoverCache
    test_cover_dir = os.path.join(test_dir, "test_cache")
    c_cache = CoverCache(test_cover_dir)
    # Create dummy image file
    dummy_file = os.path.join(c_cache.covers_dir, "test_cover.jpg")
    with open(dummy_file, "wb") as f:
        f.write(b"dummy image data")
    assert os.path.isfile(dummy_file)
    removed_files = c_cache.clear_cache()
    assert removed_files == 1
    assert not os.path.exists(dummy_file)
    print(f"  ✓ CoverCache.clear_cache successfully purged {removed_files} cached image file")

    print("\n" + "=" * 60)
    print("STEP 11: TESTING PAGINATION, DYNAMIC RESCHEDULING & WEBHOOK DIAGNOSTICS")
    print("=" * 60)
    # 1. Release counts and pagination
    tot_releases = app_db.count_releases()
    status_counts = app_db.get_release_status_counts()
    assert tot_releases >= 1
    assert "ALL" in status_counts and "REQUESTED" in status_counts
    page1 = app_db.get_releases(limit=2, offset=0)
    page2 = app_db.get_releases(limit=2, offset=2)
    assert len(page1) <= 2
    if tot_releases > 2:
        assert page1[0]["id"] != page2[0]["id"]
    print(f"  ✓ Release pagination passed (Total: {tot_releases}, Page 1: {len(page1)}, Page 2: {len(page2)})")

    # 2. Discovery pagination & counts
    # Insert dummy discovery releases
    app_db.upsert_release({
        "guid": "disc_p1",
        "title": "Discovery Novel Alpha Vol 1",
        "series_name": "Discovery Novel Alpha",
        "volume": 1.0,
        "status": "NEW"
    })
    app_db.upsert_release({
        "guid": "disc_p2",
        "title": "Discovery Novel Beta Vol 1",
        "series_name": "Discovery Novel Beta",
        "volume": 1.0,
        "status": "NEW"
    })
    disc_count = app_db.count_discovery_releases()
    assert disc_count >= 2
    d_page1 = app_db.get_discovery_releases(limit=1, offset=0)
    d_page2 = app_db.get_discovery_releases(limit=1, offset=1)
    assert len(d_page1) == 1
    assert len(d_page2) == 1
    assert d_page1[0]["id"] != d_page2[0]["id"]
    print(f"  ✓ Discovery pagination passed (Total: {disc_count}, Offset 0: '{d_page1[0]['series_name']}', Offset 1: '{d_page2[0]['series_name']}')")

    # 3. Dynamic feed interval updates
    app_db.update_all_feed_intervals(42)
    feeds = app_db.get_all_feeds()
    for f in feeds:
        assert f["check_interval_minutes"] == 42
    print(f"  ✓ Feed interval dynamic synchronization verified across {len(feeds)} feeds")

    # 4. TaskManager reschedule & backfill status
    from app.scheduler import TaskManager
    tm = TaskManager(app_db, os.path.dirname(calibre_db_file), data_dir=test_dir)
    tm.start()
    tm.reschedule_jobs(calibre_minutes=25, feed_minutes=45)
    b_status = tm.get_backfill_status()
    assert "delay_seconds" in b_status
    assert "max_pages" in b_status
    tm.stop()
    print("  ✓ TaskManager live job rescheduling & backfill status metadata verified")

    # 5. Webhook diagnostics
    from app.notifier.webhook import NotificationService
    ns = NotificationService()
    empty_res = ns.test_webhook()
    assert empty_res["status"] == "error"
    # Test invalid connection gracefully handled
    invalid_res = ns.test_webhook(discord_url="http://127.0.0.1:59999/nonexistent")
    assert invalid_res["status"] == "error"
    assert "discord" in invalid_res["results"]
    assert invalid_res["results"]["discord"]["success"] is False
    print("  ✓ Webhook diagnostics safely reported connection error without crashing")

    print("\n" + "=" * 60)
    print("STEP 12: TESTING MULTI-VOLUME BUNDLE PARSING & CANONICAL SERIES GROUPING")
    print("=" * 60)
    # 1. Bundle parsing
    bundle1 = extract_series_and_volume("Overlord Vol 1-14 Epub")
    assert bundle1["is_bundle"] is True
    assert bundle1["series_name"] == "Overlord"
    assert len(bundle1["volumes"]) == 14
    assert bundle1["volumes"][0] == 1.0 and bundle1["volumes"][-1] == 14.0
    print(f"  ✓ Bundle range parsed: '{bundle1['clean_title']}' -> {len(bundle1['volumes'])} volumes")

    bundle2 = extract_series_and_volume("DanMachi Volumes 18-20 Epub")
    assert bundle2["is_bundle"] is True
    assert bundle2["series_name"] == "DanMachi"
    assert bundle2["volumes"] == [18.0, 19.0, 20.0]

    bundle3 = extract_series_and_volume("Monogatari Box Set Vol 1, 2, 3 Epub")
    assert bundle3["is_bundle"] is True
    assert bundle3["volumes"] == [1.0, 2.0, 3.0]
    print("  ✓ Comma-separated volume list bundle parsed")

    # 2. Bundle matching against Calibre
    # In Step 2, Overlord has 15 volumes in Calibre. Overlord 1-14 is fully owned:
    m_overlord = matcher.check_release(bundle1, auto_request_monitored=True)
    assert m_overlord["status"] == "IN_CALIBRE"
    assert len(m_overlord["missing_volumes"]) == 0
    print("  ✓ Overlord Vol 1-14 bundle correctly evaluated as IN_CALIBRE (all owned)")

    # DanMachi has 18 volumes owned. Vol 18-20 should be REQUESTED with missing: [19.0, 20.0]
    # Note: earlier in test, book 19 was added in step 5, so DanMachi has 19 books!
    # Therefore Vol 18-20 has missing: [20.0]
    m_danmachi = matcher.check_release(bundle2, auto_request_monitored=True)
    assert m_danmachi["status"] == "REQUESTED"
    assert 20.0 in m_danmachi["missing_volumes"]
    print(f"  ✓ DanMachi Vol 18-20 bundle evaluated as REQUESTED (missing: {m_danmachi['missing_volumes']})")

    # 3. Store bundles in DB
    app_db.upsert_release({
        "guid": "b_overlord_1_14",
        "title": "Overlord Vol 1-14 Epub",
        "series_name": "Overlord",
        "volume": 1.0,
        "is_bundle": True,
        "volumes": bundle1["volumes"],
        "missing_volumes": m_overlord["missing_volumes"],
        "status": m_overlord["status"]
    })
    app_db.upsert_release({
        "guid": "b_danmachi_18_20",
        "title": "DanMachi Volumes 18-20 Epub",
        "series_name": "DanMachi",
        "volume": 18.0,
        "is_bundle": True,
        "volumes": bundle2["volumes"],
        "missing_volumes": m_danmachi["missing_volumes"],
        "status": m_danmachi["status"]
    })

    # 4. Grouped by Series
    grouped = app_db.get_releases_grouped_by_series()
    assert len(grouped) >= 2
    overlord_group = next((g for g in grouped if g["series_name"].lower() == "overlord"), None)
    assert overlord_group is not None
    assert overlord_group["in_calibre"] is True
    assert 1.0 in overlord_group["all_volumes"]
    assert 14.0 in overlord_group["all_volumes"]
    print(f"  ✓ Group by Series verified: {len(grouped)} canonical series groups created")
    print(f"    - Overlord Group: {overlord_group['releases_count']} releases, {len(overlord_group['all_volumes'])} detected volumes")

    print("\n" + "=" * 60)
    print("ALL TESTS (INCLUDING SECURITY, SYNOPSIS, PAGINATION & BUNDLES) PASSED WITH 100% SUCCESS!")
    print("=" * 60)

if __name__ == "__main__":
    run_e2e_test()
