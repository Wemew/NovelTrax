"""Database identity + consolidated detection fields (spec Part II, §12.1/§12.6/§12.11.2/§12.13.2).

Covers:
  * idempotent migrations for the five new ``releases`` columns;
  * release groups persisted as a set with the primary retained, plus the
    one-shot split backfill of a legacy single ``release_group`` value;
  * set-based prioritized matching for joint releases (either group matches,
    in both directions) and for un-backfilled legacy rows;
  * status precedence: the five §12.6 rules, verbatim API mode, and the
    ``upsert_release`` / ``update_release_status`` wiring;
  * stable identity keys: a metadata title never rewrites a stored
    ``series_name``, and ``repair_rekeyed_series_names`` is idempotent and
    skips aliases claimed by more than one tracked series;
  * revision / chapter-number / chapter-range round-trip.

Calibre's interpreter runs with ``optimize=2``, which strips bare ``assert``;
every check therefore raises ``AssertionError`` explicitly.
"""

import json
import os
import shutil
import sys
import tempfile
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
for _path in (BASE_DIR, BACKEND_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app.database import Database
from app.status_precedence import resolve_status

PASSED = 0
FAILED = 0
FAILURES = []


class CheckFailure(AssertionError):
    pass


def check(condition, message):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {message}")
        return
    FAILED += 1
    FAILURES.append(message)
    print(f"  [FAIL] {message}")
    raise CheckFailure(message)


def section(title):
    print(f"\n--- {title} ---")


def make_db(prefix="noveltrax_db_identity_"):
    tmp_dir = tempfile.mkdtemp(prefix=prefix)
    return tmp_dir, Database(os.path.join(tmp_dir, "app.db"))


def raw_row(db, guid):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM releases WHERE guid = ?;", (guid,))
        row = cur.fetchone()
        return dict(row) if row else None


def insert_legacy_release(db, guid, series_name, release_group):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO releases (guid, title, series_name, volume, status, release_group, release_groups_json)
            VALUES (?, ?, ?, 1.0, 'REQUESTED', ?, '[]');
            """,
            (guid, f"{series_name} Vol 1", series_name, release_group),
        )
        conn.commit()


def add_tracked_series(db, name):
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO series_tracking (name, is_monitored) VALUES (?, 1);",
            (name,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1. Schema migrations
# ---------------------------------------------------------------------------
def test_new_columns_survive_reinit():
    section("1. New detection columns are created once and survive repeated init_db")
    tmp_dir, db = make_db()
    try:
        is_new, rel_id = db.upsert_release({
            "guid": "schema-1",
            "title": "Schema Probe Vol 3 (f2) [Yen Press]",
            "series_name": "Schema Probe",
            "volume": 3.0,
            "status": "REQUESTED",
            "release_groups": ["1r0n", "Ushi"],
            "publisher": "Yen Press",
            "revision": 2,
            "chapter_number": 12.5,
            "chapter_ranges": [[1, 5], [6, 9]],
        })
        check(is_new is True and rel_id > 0, "insert with detection fields returned a new row id")

        for _ in range(4):
            db.init_db()  # suite runs init 5x in total; must stay idempotent

        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(releases);")
            columns = {row["name"] for row in cur.fetchall()}
        for column in (
            "release_groups_json",
            "publisher",
            "revision",
            "chapter_number",
            "chapter_ranges_json",
        ):
            check(column in columns, f"releases has the {column} column after re-init")

        row = raw_row(db, "schema-1")
        check(json.loads(row["release_groups_json"]) == ["1r0n", "Ushi"],
              "release groups JSON survives re-init unchanged")
        check(row["publisher"] == "Yen Press", "publisher survives re-init unchanged")
        check(row["revision"] == 2, "revision survives re-init unchanged")
        check(row["chapter_number"] == 12.5, "chapter_number survives re-init unchanged")
        check(json.loads(row["chapter_ranges_json"]) == [[1, 5], [6, 9]],
              "chapter_ranges JSON survives re-init unchanged")
        check(str(db.get_setting("groups_backfill_applied")) == "1",
              "groups_backfill_applied marker is set after init")
        check(str(db.get_setting("stable_key_repair_applied")) == "1",
              "stable_key_repair_applied marker is set after init")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Release groups: persistence, primary, derivation and backfill
# ---------------------------------------------------------------------------
def test_release_groups_persist_and_backfill():
    section("2. Group sets persist, keep the first as primary, derive from title, and backfill")
    tmp_dir, db = make_db()
    try:
        db.upsert_release({
            "guid": "joint-1",
            "title": "Sister and Giant v01-06 (2024-2026) (Digital) (1r0n, Ushi)",
            "series_name": "Sister and Giant",
            "volume": 1.0,
            "status": "REQUESTED",
            "release_groups": ["1r0n", "Ushi"],
            "release_group": "1r0n",
        })
        row = raw_row(db, "joint-1")
        check(json.loads(row["release_groups_json"]) == ["1r0n", "Ushi"],
              "explicit group list is persisted as JSON")
        check(row["release_group"] == "1r0n", "release_group keeps the first/primary group")
        item = db.get_release_by_id(row["id"])
        check(item["release_groups"] == ["1r0n", "Ushi"],
              "get_release_by_id exposes release_groups as a parsed list")

        # Derivation path: no explicit groups/release_group, parser helper monkeypatched
        # so the test is independent of the optional parser deployment.
        import app.parser as parser_module
        original_extract_groups = getattr(parser_module, "extract_release_groups", None)
        parser_module.extract_release_groups = lambda raw_title, known_groups=None: ["DerivedA", "DerivedB"]
        try:
            db.upsert_release({
                "guid": "derived-1",
                "title": "Derived Groups Title Vol 4",
                "series_name": "Derived Groups Series",
                "volume": 4.0,
                "status": "REQUESTED",
            })
        finally:
            if original_extract_groups is None:
                del parser_module.extract_release_groups
            else:
                parser_module.extract_release_groups = original_extract_groups
        derived = raw_row(db, "derived-1")
        check(json.loads(derived["release_groups_json"]) == ["DerivedA", "DerivedB"],
              "missing groups are derived from the title via extract_release_groups")
        check(derived["release_group"] == "DerivedA",
              "derived group list keeps the first group as primary")

        # Legacy single-value fallback: a bracketed publisher must still reach
        # release_group even with no group list supplied.
        db.upsert_release({
            "guid": "fallback-1",
            "title": "[Yen Press] Overlord, Vol. 16 [Digital] [EPUB]",
            "series_name": "Overlord",
            "volume": 16.0,
            "status": "REQUESTED",
        })
        fallback = raw_row(db, "fallback-1")
        check(fallback["release_group"] == "Yen Press",
              "legacy single-value fallback still stores the extracted primary group")
        check("Yen Press" in json.loads(fallback["release_groups_json"]),
              "legacy fallback also persists the group in the JSON set")

        # One-shot backfill of legacy rows written before the JSON set existed.
        legacy_cases = [
            ("legacy-comma", "Alpha, Beta", ["Alpha", "Beta"]),
            ("legacy-amp", "Gamma & Delta", ["Gamma", "Delta"]),
            ("legacy-slash", "Epsilon / Zeta", ["Epsilon", "Zeta"]),
            ("legacy-plus", "Eta + Theta", ["Eta", "Theta"]),
            ("legacy-and", "Iota and Kappa", ["Iota", "Kappa"]),
        ]
        for guid, legacy_group, _expected in legacy_cases:
            insert_legacy_release(db, guid, f"Legacy {guid}", legacy_group)

        repaired = db.backfill_release_groups()
        check(repaired == len(legacy_cases),
              f"backfill split exactly {len(legacy_cases)} legacy group values (got {repaired})")
        for guid, legacy_group, expected in legacy_cases:
            legacy_row = raw_row(db, guid)
            check(json.loads(legacy_row["release_groups_json"]) == expected,
                  f"backfill split {legacy_group!r} into {expected}")
            check(legacy_row["release_group"] == expected[0],
                  f"backfill sets the primary release_group to {expected[0]!r}")
        check(db.backfill_release_groups() == 0,
              "a second backfill pass is a no-op (idempotent)")
        unbackfilled = raw_row(db, "joint-1")
        check(json.loads(unbackfilled["release_groups_json"]) == ["1r0n", "Ushi"],
              "backfill leaves already-populated group sets untouched")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Set-based prioritized matching (§12.11.2)
# ---------------------------------------------------------------------------
def test_joint_release_prioritized_matching():
    section("3. Joint release counts as prioritized when either group is configured")
    tmp_dir, db = make_db()
    try:
        db.upsert_release({
            "guid": "joint-prio-1",
            "title": "Sister and Giant v01-06 (2024-2026) (Digital) (JointAlpha, JointBeta)",
            "series_name": "Sister and Giant",
            "volume": 1.0,
            "status": "REQUESTED",
            "release_groups": ["JointAlpha", "JointBeta"],
            "release_group": "JointAlpha",
        })
        db.add_prioritized_group("JointAlpha")
        alpha_groups = {g["name"].lower() for g in db.get_prioritized_groups()}
        check("jointalpha" in alpha_groups, "JointAlpha configured as a prioritized group")

        item = next(r for r in db.get_releases(limit=50) if r["guid"] == "joint-prio-1")
        check(item["is_prioritized"] is True,
              "joint release is prioritized when its primary matches")
        check(any(r["guid"] == "joint-prio-1" for r in db.get_prioritized_releases(limit=50)),
              "prioritized-only listing includes the joint release via the primary group")
        check(db.count_prioritized_releases() == 1,
              "count_prioritized_releases counts the joint release exactly once")
        grouped = db.get_releases_grouped_by_series(prioritized_only=True)
        joint_group = next((g for g in grouped if "sister" in g["series_name"].lower()), None)
        check(joint_group is not None and joint_group["has_prioritized"] is True,
              "grouped view marks has_prioritized for the joint release")
        check(joint_group is not None and "display_name" in joint_group,
              "grouped view exposes a display_name while grouping keys stay stable")
        if joint_group is not None:
            check(isinstance(joint_group["display_name"], str) and joint_group["display_name"],
                  "display_name is a non-empty string")

        # Reverse direction: only the *secondary* group is configured.
        alpha_id = next(g["id"] for g in db.get_prioritized_groups() if g["name"].lower() == "jointalpha")
        db.remove_prioritized_group(alpha_id)
        db.add_prioritized_group("JointBeta")
        item = next(r for r in db.get_releases(limit=50) if r["guid"] == "joint-prio-1")
        check(item["is_prioritized"] is True,
              "joint release is prioritized when only the secondary group is configured")
        check(db.count_prioritized_releases() == 1,
              "count_prioritized_releases follows set membership, not whole-string equality")
        release_count = next(g for g in db.get_prioritized_groups() if g["name"].lower() == "jointbeta")["release_count"]
        check(release_count == 1,
              "per-group release_count uses set membership (JointBeta count is 1)")
        beta_removed = False
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM prioritized_groups WHERE lower(name) = 'jointbeta';")
            beta_id = cur.fetchone()["id"]
        beta_removed = db.remove_prioritized_group(beta_id)
        check(beta_removed is True, "JointBeta can be removed from the prioritized list")
        item = next(r for r in db.get_releases(limit=50) if r["guid"] == "joint-prio-1")
        check(item["is_prioritized"] is False,
              "joint release is not prioritized when neither group is configured")

        # Legacy row that still has no JSON set: the single column is honoured.
        db.add_prioritized_group("LegacyOnlyGroup")
        insert_legacy_release(db, "legacy-prio-1", "Legacy Priority Series", "LegacyOnlyGroup")
        legacy_item = next(r for r in db.get_releases(limit=50) if r["guid"] == "legacy-prio-1")
        check(legacy_item["release_groups"] == ["LegacyOnlyGroup"],
              "read path defaults release_groups to [release_group] for un-backfilled rows")
        check(legacy_item["is_prioritized"] is True,
              "in-memory prioritized check honours the legacy release_group fallback")
        check(any(r["guid"] == "legacy-prio-1" for r in db.get_prioritized_releases(limit=50)),
              "SQL prioritized listing honours the legacy release_group fallback")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Status precedence (§12.6)
# ---------------------------------------------------------------------------
def test_status_precedence_rules():
    section("4. Status precedence: five rules, verbatim mode and DB wiring")
    # Rule 1: blank incoming never clears an existing status.
    check(resolve_status("REQUESTED", "") == "REQUESTED",
          "rule 1: blank incoming never clears REQUESTED")
    check(resolve_status("IGNORED", None) == "IGNORED",
          "rule 1: None incoming never clears IGNORED")
    # Rule 2: unset existing adopts incoming.
    check(resolve_status("", "NEW") == "NEW",
          "rule 2: empty existing adopts NEW")
    check(resolve_status(None, "REQUESTED") == "REQUESTED",
          "rule 2: None existing adopts REQUESTED")
    # Rule 3: NEW / DISCOVERY are automation-owned/recomputable.
    check(resolve_status("NEW", "DISCOVERY") == "DISCOVERY",
          "rule 3: NEW is recomputable to DISCOVERY")
    check(resolve_status("DISCOVERY", "NEW") == "NEW",
          "rule 3: DISCOVERY is recomputable to NEW")
    # Rule 4: REQUESTED / IN_CALIBRE are never downgraded to a classification.
    check(resolve_status("REQUESTED", "NEW") == "REQUESTED",
          "rule 4: REQUESTED is not downgraded to NEW")
    check(resolve_status("REQUESTED", "DISCOVERY") == "REQUESTED",
          "rule 4: REQUESTED is not downgraded to DISCOVERY")
    check(resolve_status("IN_CALIBRE", "NEW") == "IN_CALIBRE",
          "rule 4: IN_CALIBRE is not downgraded to NEW")
    check(resolve_status("IN_CALIBRE", "REQUESTED") == "IN_CALIBRE",
          "rule 4: IN_CALIBRE is not downgraded to a wishlist request")
    # Rule 5: the user-veto tier cannot be lifted by automation.
    check(resolve_status("IGNORED", "NEW") == "IGNORED",
          "rule 5: IGNORED is not lifted by a NEW classification")
    check(resolve_status("BLACKLISTED", "REQUESTED") == "BLACKLISTED",
          "rule 5: BLACKLISTED is not lifted by a REQUESTED classification")
    check(resolve_status("IGNORED", "REQUESTED") == "IGNORED",
          "rule 5: IGNORED is not lifted by an automated request")
    # Verbatim mode writes the incoming value with no precedence.
    check(resolve_status("REQUESTED", "NEW", enforce_precedence=False) == "NEW",
          "verbatim mode writes NEW over REQUESTED")
    check(resolve_status("IGNORED", "REQUESTED", enforce_precedence=False) == "REQUESTED",
          "verbatim mode allows an explicit API action to lift IGNORED")
    check(resolve_status("REQUESTED", "", enforce_precedence=False) == "",
          "verbatim mode writes a blank incoming value as-is")

    tmp_dir, db = make_db()
    try:
        is_new, rel_id = db.upsert_release({
            "guid": "status-1",
            "title": "Status Probe Vol 3",
            "series_name": "Status Probe",
            "volume": 3.0,
            "status": "NEW",
        })
        check(is_new is True, "status probe release inserted")
        check(db.get_release_by_id(rel_id)["status"] == "NEW", "new row starts as NEW")

        # A payload-only re-ingest (no status supplied) keeps the stored status,
        # matching the historical upsert behaviour.
        db.upsert_release({
            "guid": "status-1",
            "title": "Status Probe Vol 3 (payload refresh)",
            "series_name": "Status Probe",
            "volume": 3.0,
        })
        check(db.get_release_by_id(rel_id)["status"] == "NEW",
              "payload-only upsert preserves the existing status")

        # Automation may recompute NEW into a user-intent status...
        db.upsert_release({
            "guid": "status-1",
            "title": "Status Probe Vol 3",
            "series_name": "Status Probe",
            "volume": 3.0,
            "status": "REQUESTED",
        })
        check(db.get_release_by_id(rel_id)["status"] == "REQUESTED",
              "upsert promotes NEW to REQUESTED (classification is automation-owned)")
        # ...but it may never downgrade REQUESTED back to a classification.
        db.upsert_release({
            "guid": "status-1",
            "title": "Status Probe Vol 3",
            "series_name": "Status Probe",
            "volume": 3.0,
            "status": "NEW",
        })
        check(db.get_release_by_id(rel_id)["status"] == "REQUESTED",
              "upsert never downgrades REQUESTED to NEW")

        # Default update_release_status path is verbatim: an explicit action
        # can lift the veto tier.
        db.update_release_status(rel_id, "IGNORED")
        check(db.get_release_by_id(rel_id)["status"] == "IGNORED", "verbatim update sets IGNORED")
        db.update_release_status(rel_id, "REQUESTED")
        check(db.get_release_by_id(rel_id)["status"] == "REQUESTED",
              "verbatim update_release_status lifts IGNORED back to REQUESTED")

        # Automated caller opting in is protected.
        db.update_release_status(rel_id, "NEW", enforce_precedence=True)
        check(db.get_release_by_id(rel_id)["status"] == "REQUESTED",
              "opt-in precedence keeps REQUESTED over an automated NEW")

        # Blank automated writes never clear the stored status.
        db.update_release_status(rel_id, "", enforce_precedence=True)
        check(db.get_release_by_id(rel_id)["status"] == "REQUESTED",
              "opt-in precedence ignores a blank automated status")

        # An unset existing status adopts the incoming one in precedence mode.
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE releases SET status = '' WHERE id = ?;", (rel_id,))
            conn.commit()
        db.update_release_status(rel_id, "DISCOVERY", enforce_precedence=True)
        check(db.get_release_by_id(rel_id)["status"] == "DISCOVERY",
              "opt-in precedence lets an unset existing status adopt DISCOVERY")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Stable identity key + repair (§12.1)
# ---------------------------------------------------------------------------
def test_stable_key_repair():
    section("5. Stable series keys: upsert preservation and repair_rekeyed_series_names")
    tmp_dir, db = make_db()
    try:
        check(str(db.get_setting("stable_key_repair_applied")) == "1",
              "stable_key_repair_applied marker set by the one-shot startup pass")

        # A metadata/canonical title must never rewrite a stored series_name.
        db.upsert_release({
            "guid": "stable-1",
            "title": "Stable Series Vol 1",
            "series_name": "Dungeon Meshi",
            "volume": 1.0,
            "status": "REQUESTED",
        })
        db.upsert_release({
            "guid": "stable-1",
            "title": "Stable Series Vol 1 (metadata refresh)",
            "series_name": "Delicious in Dungeon",
            "volume": 1.0,
            "status": "REQUESTED",
        })
        check(db.get_release_by_id(raw_row(db, "stable-1")["id"])["series_name"] == "Dungeon Meshi",
              "a metadata/canonical title never overwrites the stored stable series_name")

        # The metadata-enrichment path is also prevented from replacing a stored
        # alias with its canonical display spelling.
        db.upsert_release({
            "guid": "stable-alias-1",
            "title": "Alya Volume 3",
            "series_name": "The Neighboring Aarya-San Who Sometimes Acts Affectionate and Murmuring in Russian",
            "volume": 3.0,
            "status": "REQUESTED",
        })
        db.update_release_details(
            raw_row(db, "stable-alias-1")["id"],
            series_name="Alya Sometimes Hides Her Feelings in Russian",
        )
        check(db.get_release_by_id(raw_row(db, "stable-alias-1")["id"])["series_name"]
              == "The Neighboring Aarya-San Who Sometimes Acts Affectionate and Murmuring in Russian",
              "metadata enrichment keeps the stored alias instead of its canonical title")
        db.update_release_details(
            raw_row(db, "stable-alias-1")["id"],
            series_name="A Completely Different Series",
        )
        check(db.get_release_by_id(raw_row(db, "stable-alias-1")["id"])["series_name"]
              == "A Completely Different Series",
              "a genuinely different series name can still be written by an explicit update")

        # Re-key an alias to the tracked stable name.
        add_tracked_series(db, "86-Eighty Six")
        db.upsert_release({
            "guid": "rekey-1",
            "title": "86 Volume 4",
            "series_name": "86",
            "volume": 4.0,
            "status": "REQUESTED",
        })
        with db.get_connection() as conn:
            count_before = conn.cursor().execute("SELECT count(*) FROM releases;").fetchone()[0]
        rekeyed = db.repair_rekeyed_series_names()
        check(rekeyed == 1, f"repair re-keyed exactly the one alias release (got {rekeyed})")
        check(db.get_release_by_id(raw_row(db, "rekey-1")["id"])["series_name"] == "86-Eighty Six",
              "release stored under '86' is re-keyed to the tracked name '86-Eighty Six'")
        check(db.repair_rekeyed_series_names() == 0,
              "a second repair pass is a no-op (idempotent)")
        with db.get_connection() as conn:
            count_after = conn.cursor().execute("SELECT count(*) FROM releases;").fetchone()[0]
        check(count_after == count_before, "repair is non-destructive (no rows added or deleted)")

        # An alias claimed by two tracked series is skipped, never merged.
        add_tracked_series(db, "Alya Sometimes Hides Her Feelings in Russian")
        add_tracked_series(db, "The Neighboring Aarya-San Who Sometimes Acts Affectionate and Murmuring in Russian")
        db.upsert_release({
            "guid": "ambiguous-1",
            "title": "Roshidere Volume 2",
            "series_name": "Roshidere",
            "volume": 2.0,
            "status": "REQUESTED",
        })
        ambiguous_repaired = db.repair_rekeyed_series_names()
        check(ambiguous_repaired == 0,
              "ambiguous alias claimed by two tracked series is skipped (0 re-keyed)")
        check(db.get_release_by_id(raw_row(db, "ambiguous-1")["id"])["series_name"] == "Roshidere",
              "ambiguous release keeps its stored series_name (never merged)")
        check(db.repair_rekeyed_series_names() == 0,
              "repair remains a no-op after the ambiguous skip")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Revision / chapter round-trip (§12.13.2)
# ---------------------------------------------------------------------------
def test_revision_and_chapter_roundtrip():
    section("6. revision, publisher, chapter_number and chapter_ranges round-trip")
    tmp_dir, db = make_db()
    try:
        db.upsert_release({
            "guid": "detection-1",
            "title": "The Hero and the Sage 001-039 as v01-08 (Digital-Compilation) (Square Enix) (DigitalMangaFan) (f2)",
            "series_name": "The Hero and the Sage",
            "volume": 1.0,
            "status": "REQUESTED",
            "release_groups": ["DigitalMangaFan"],
            "publisher": "Square Enix",
            "revision": 2,
            "chapter_number": 12.5,
            "chapter_ranges": [[1, 39]],
        })
        raw = raw_row(db, "detection-1")
        check(raw["revision"] == 2, "revision is persisted as INTEGER")
        check(raw["publisher"] == "Square Enix", "publisher is persisted")
        check(raw["chapter_number"] == 12.5, "chapter_number is persisted as REAL")
        check(json.loads(raw["chapter_ranges_json"]) == [[1, 39]],
              "chapter_ranges are persisted as JSON")
        check(json.loads(raw["release_groups_json"]) == ["DigitalMangaFan"],
              "group set persists alongside the revision marker")

        item = db.get_release_by_id(raw["id"])
        check(item["revision"] == 2 and isinstance(item["revision"], int),
              "get_release_by_id exposes revision as an int")
        check(item["publisher"] == "Square Enix", "get_release_by_id exposes publisher")
        check(item["chapter_number"] == 12.5, "get_release_by_id exposes chapter_number")
        check(item["chapter_ranges"] == [[1, 39]], "get_release_by_id parses chapter_ranges")
        check(item["release_groups"] == ["DigitalMangaFan"],
              "get_release_by_id exposes release_groups")

        listed_items = db.get_releases(limit=20)
        listed = next(r for r in listed_items if r["guid"] == "detection-1")
        check(listed["chapter_ranges"] == [[1, 39]], "get_releases parses chapter_ranges")
        check(listed["release_groups"] == ["DigitalMangaFan"], "get_releases parses release_groups")

        db.upsert_release({
            "guid": "detection-plain",
            "title": "Plain Quiet Title Vol 1",
            "series_name": "Plain Quiet Series",
            "volume": 1.0,
            "status": "REQUESTED",
        })
        plain = db.get_release_by_id(raw_row(db, "detection-plain")["id"])
        check(plain["revision"] == 0, "revision defaults to 0 when no marker is supplied")
        check(plain["chapter_ranges"] == [], "chapter_ranges defaults to an empty list")
        check(plain["publisher"] == "", "publisher defaults to an empty string")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


TESTS = [
    test_new_columns_survive_reinit,
    test_release_groups_persist_and_backfill,
    test_joint_release_prioritized_matching,
    test_status_precedence_rules,
    test_stable_key_repair,
    test_revision_and_chapter_roundtrip,
]


def main():
    global FAILED
    print("=" * 74)
    print("  NovelTrax DB identity + consolidated detection fields")
    print(f"  interpreter optimize level: {sys.flags.optimize}")
    print("=" * 74)
    for test_fn in TESTS:
        try:
            test_fn()
        except CheckFailure:
            continue
        except Exception as exc:
            FAILED += 1
            FAILURES.append(f"{test_fn.__name__}: {exc}")
            print(f"  [FAIL] {test_fn.__name__} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print("\n" + "=" * 74)
    print(f"  {PASSED} checks passed, {FAILED} failed")
    print("=" * 74)
    if FAILED:
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("ALL DB IDENTITY TESTS PASSED")


if __name__ == "__main__":
    main()
