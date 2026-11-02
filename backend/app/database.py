import os
import re
import sqlite3
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, Set, Union

logger = logging.getLogger("noveltrax.database")

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self):
        """Creates the initial database schema if not present."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            
            # Series tracking table (populated from Calibre)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS series_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calibre_series_id INTEGER,
                    name TEXT UNIQUE NOT NULL,
                    authors TEXT DEFAULT '',
                    book_count INTEGER DEFAULT 0,
                    latest_index REAL DEFAULT 0.0,
                    is_monitored INTEGER DEFAULT 1,
                    website_url TEXT DEFAULT '',
                    owned_volumes_json TEXT DEFAULT '[]',
                    last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Releases / Requests table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guid TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    series_name TEXT,
                    volume REAL,
                    link TEXT,
                    pub_date TEXT,
                    cover_url TEXT,
                    description TEXT,
                    author TEXT,
                    source_feed_id INTEGER,
                    source_feed_name TEXT,
                    series_link TEXT,
                    status TEXT DEFAULT 'REQUESTED',
                    calibre_matched_book_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Migrations for existing databases
            try:
                cur.execute("ALTER TABLE series_tracking ADD COLUMN website_url TEXT DEFAULT '';")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE releases ADD COLUMN series_link TEXT;")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE series_tracking ADD COLUMN description TEXT DEFAULT '';")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE series_tracking ADD COLUMN cover_url TEXT DEFAULT '';")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE releases ADD COLUMN is_bundle INTEGER DEFAULT 0;")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE releases ADD COLUMN volumes_json TEXT DEFAULT '[]';")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE releases ADD COLUMN missing_vols_json TEXT DEFAULT '[]';")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE releases ADD COLUMN is_official INTEGER DEFAULT 1;")
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute("ALTER TABLE series_tracking ADD COLUMN owned_volumes_json TEXT DEFAULT '[]';")
            except sqlite3.OperationalError:
                pass

            # Releases table downloader and prioritized group migrations
            for col_def in [
                ("download_url", "TEXT"),
                ("download_status", "TEXT DEFAULT 'IDLE'"),
                ("torrent_hash", "TEXT"),
                ("release_group", "TEXT DEFAULT ''"),
                ("ignored", "INTEGER DEFAULT 0"),
            ]:
                try:
                    cur.execute(f"ALTER TABLE releases ADD COLUMN {col_def[0]} {col_def[1]};")
                except sqlite3.OperationalError:
                    pass

            # Performance indexes on releases table
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_release_group ON releases(release_group);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_ignored ON releases(ignored);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status);")

            # Series blacklist table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS series_blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    reason TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Feeds configuration table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    feed_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    config_json TEXT DEFAULT '{}',
                    enabled INTEGER DEFAULT 1,
                    auto_request INTEGER DEFAULT 1,
                    check_interval_minutes INTEGER DEFAULT 60,
                    last_checked TIMESTAMP,
                    last_status TEXT,
                    last_error TEXT
                );
            """)

            # Activity audit log table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # App settings table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Seed default settings if empty
            cur.execute("SELECT count(*) FROM app_settings;")
            if cur.fetchone()[0] == 0:
                defaults = {
                    "calibre_path": os.getenv("CALIBRE_DIR", "/calibre"),
                    "calibre_sync_interval_minutes": "15",
                    "feed_check_interval_minutes": "60",
                    "auto_request_monitored": "true",
                    "discord_webhook_url": "",
                    "apprise_url": "",
                    "backfill_max_pages": "30",
                    "backfill_delay_seconds": "2.0",
                    "track_official_only": "true",
                }
                for k, v in defaults.items():
                    cur.execute("INSERT INTO app_settings (key, value) VALUES (?, ?);", (k, v))

            # Downloader settings defaults migration
            downloader_defaults = [
                ("qbit_enabled", "true"),
                ("qbit_host", os.getenv("QBIT_HOST", "localhost")),
                ("qbit_port", os.getenv("QBIT_PORT", "8080")),
                ("qbit_username", os.getenv("QBIT_USERNAME", "admin")),
                ("qbit_password", os.getenv("QBIT_PASSWORD", "")),
                ("qbit_use_ssl", os.getenv("QBIT_USE_SSL", "false")),
                ("qbit_category", os.getenv("QBIT_CATEGORY", "novels")),
                ("qbit_savepath", os.getenv("DOWNLOAD_DIR", "/downloads")),
                ("qbit_url", os.getenv("QBIT_URL", "http://localhost:8080")),
            ]
            for k, v in downloader_defaults:
                cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?);", (k, v))

            # Seed default J-Novels feed if feeds table is empty
            cur.execute("SELECT count(*) FROM feeds;")
            if cur.fetchone()[0] == 0:
                cur.execute("""
                    INSERT INTO feeds (name, feed_type, url, config_json, enabled, auto_request, check_interval_minutes)
                    VALUES (?, ?, ?, ?, 1, 1, 60);
                """, (
                    "J-Novels EPUB Releases",
                    "jnovels",
                    "https://jnovels.com/?s=epub",
                    json.dumps({
                        "description": "Monitors J-Novels for newly posted light novel and web novel EPUB releases.",
                        "max_pages": 3
                    })
                ))

            # Seed default Nyaa feed if not present
            cur.execute("SELECT count(*) FROM feeds WHERE feed_type = 'nyaa' OR url LIKE '%nyaa.si%';")
            if cur.fetchone()[0] == 0:
                cur.execute("""
                    INSERT INTO feeds (name, feed_type, url, config_json, enabled, auto_request, check_interval_minutes)
                    VALUES (?, ?, ?, ?, 1, 1, 60);
                """, (
                    "Nyaa Light Novels (3_1)",
                    "nyaa",
                    "https://nyaa.si/?page=rss&c=3_1",
                    json.dumps({
                        "category": "3_1",
                        "max_pages": 2,
                        "delay_seconds": 1.5,
                        "description": "Monitors Nyaa (Category 3_1: Literature - English-translated) for light novel releases."
                    })
                ))

            # Repair legacy mangled titles from prior clean_novel_title defect
            cur.execute("UPDATE releases SET title = REPLACE(title, 'J- Club]', '[J-Novel Club]') WHERE title LIKE 'J- Club]%';")

            # Clean up corrupted titles from Iteration 3 overbroad prepend (unbalanced leading bracket)
            cur.execute("""
                UPDATE releases SET title = substr(title, 2)
                WHERE title LIKE '[%'
                  AND (length(title) - length(replace(title, '[', ''))) > (length(title) - length(replace(title, ']', '')));
            """)

            # Reset contaminated release_group values containing '['
            cur.execute("UPDATE releases SET release_group = '' WHERE release_group LIKE '%[%';")

            # Safe repair of missing opening bracket (only when no opening bracket precedes closing bracket)
            cur.execute("""
                UPDATE releases SET title = '[' || title
                WHERE instr(title, ']') > 0
                  AND (instr(title, '[') = 0 OR instr(title, '[') > instr(title, ']'));
            """)

            cur.execute("UPDATE releases SET release_group = 'J-Novel Club' WHERE (release_group IS NULL OR release_group = '') AND title LIKE '%J-Novel Club%';")

            # Backfill release_group for existing releases where currently empty
            try:
                from app.parser import extract_release_group
                cur.execute("SELECT id, title FROM releases WHERE release_group IS NULL OR release_group = '';")
                for r_row in cur.fetchall():
                    rg = extract_release_group(r_row["title"])
                    if rg:
                        cur.execute("UPDATE releases SET release_group = ? WHERE id = ?;", (rg, r_row["id"]))
            except Exception:
                pass

            # Synchronize ignored flag invariants across all existing releases
            cur.execute("UPDATE releases SET ignored = 1 WHERE status = 'IGNORED' AND (ignored = 0 OR ignored IS NULL);")
            cur.execute("UPDATE releases SET ignored = 0 WHERE status != 'IGNORED' AND ignored = 1;")

            conn.commit()

        self._run_chapter_purge_once()

    def _run_chapter_purge_once(self):
        """One-shot cleanup of chapter rows stored before chapter filtering existed."""
        try:
            if self.get_setting("chapter_filter_applied", "") == "1":
                return
            removed = self.purge_chapter_releases()
            self.set_setting("chapter_filter_applied", "1")
            if removed:
                logger.info(f"Chapter filter: removed {removed} chapter instalment(s) misclassified as volumes.")
        except Exception as exc:
            logger.warning(f"Chapter filter cleanup skipped: {exc}")

    def purge_chapter_releases(self) -> int:
        """
        Deletes stored chapter instalments from the releases table.

        A chapter is a release whose number is a bare trailing number with no
        volume keyword ("Dandadan 250") or which carries an explicit
        Chapter/Ch. marker -- see ``app.parser.is_chapter_release``.

        Only unreviewed rows (NEW / REQUESTED) are removed. Rows matched to the
        Calibre library (IN_CALIBRE) are facts about the library and are never
        deleted, and user decisions (IGNORED / BLACKLISTED) are preserved.

        Returns the number of rows removed.
        """
        from app.parser import is_chapter_release

        victim_ids: List[int] = []
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, title FROM releases WHERE status IN ('NEW', 'REQUESTED') OR status IS NULL;")
            for row in cur.fetchall():
                if is_chapter_release(row["title"] or ""):
                    victim_ids.append(row["id"])

            if victim_ids:
                cur.executemany("DELETE FROM releases WHERE id = ?;", [(rid,) for rid in victim_ids])
                conn.commit()

        if victim_ids:
            self.log_activity(
                "CHAPTER_PURGE",
                "Removed chapter releases",
                f"Removed {len(victim_ids)} chapter instalment(s) that had been misclassified as volumes."
            )
        return len(victim_ids)

    # --- Setting Operations ---
    def get_setting(self, key: str, default: str = "") -> str:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key = ?;", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?;", (key, value, value))
            conn.commit()

    def get_all_settings(self) -> Dict[str, str]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM app_settings;")
            return {row["key"]: row["value"] for row in cur.fetchall()}

    # --- Series Tracking Operations ---
    def sync_calibre_series(self, calibre_series_list: List[Dict[str, Any]], volumes_map: Optional[Dict[str, Set[float]]] = None) -> int:
        """
        Updates series_tracking from Calibre series list.
        Inserts new series, updates book_count, latest_index, and owned_volumes_json.
        Preserves user's is_monitored status!
        """
        updated_count = 0
        from urllib.parse import quote_plus
        from app.parser import normalize_text
        with self.get_connection() as conn:
            cur = conn.cursor()
            for s in calibre_series_list:
                name = s["series_name"]
                ser_id = s["series_id"]
                count = s["book_count"]
                latest = s["latest_index"]
                authors = s.get("authors", "")
                default_web_url = f"https://jnovels.com/?s={quote_plus(name)}"

                owned_vols_set: Set[float] = set()
                if volumes_map:
                    for k in (str(ser_id), ser_id, name, name.lower(), normalize_text(name)):
                        if k in volumes_map:
                            owned_vols_set.update(volumes_map[k])
                    parts = [p.strip() for p in re.split(r"\s*[|/–—]\s*|\s+-\s+|//|;", name) if p.strip()]
                    if len(parts) > 1:
                        for p in parts:
                            for pk in (p, p.lower(), normalize_text(p)):
                                if pk in volumes_map:
                                    owned_vols_set.update(volumes_map[pk])
                elif latest and float(latest) > 0:
                    owned_vols_set.add(float(latest))

                owned_json = json.dumps(sorted(list(owned_vols_set)))

                cur.execute("""
                    INSERT INTO series_tracking (
                        calibre_series_id, name, authors, book_count, latest_index, is_monitored, website_url, owned_volumes_json, last_sync
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(name) DO UPDATE SET
                        calibre_series_id = excluded.calibre_series_id,
                        authors = CASE WHEN excluded.authors != '' THEN excluded.authors ELSE series_tracking.authors END,
                        book_count = excluded.book_count,
                        latest_index = excluded.latest_index,
                        website_url = CASE WHEN series_tracking.website_url = '' OR series_tracking.website_url IS NULL THEN excluded.website_url ELSE series_tracking.website_url END,
                        owned_volumes_json = CASE WHEN excluded.owned_volumes_json != '[]' THEN excluded.owned_volumes_json ELSE series_tracking.owned_volumes_json END,
                        last_sync = CURRENT_TIMESTAMP;
                """, (ser_id, name, authors, count, latest, default_web_url, owned_json))
                updated_count += 1
            conn.commit()
        return updated_count

    def get_all_series(self, monitored_only: bool = False) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            base_query = """
                SELECT 
                    st.*,
                    (SELECT r.cover_url FROM releases r WHERE lower(r.series_name) = lower(st.name) AND r.cover_url IS NOT NULL AND r.cover_url != '' ORDER BY r.id DESC LIMIT 1) AS cover_url
                FROM series_tracking st
            """
            if monitored_only:
                cur.execute(f"{base_query} WHERE st.is_monitored = 1 ORDER BY st.name COLLATE NOCASE ASC;")
            else:
                cur.execute(f"{base_query} ORDER BY st.is_monitored DESC, st.name COLLATE NOCASE ASC;")
            return [dict(row) for row in cur.fetchall()]

    def toggle_series_monitored(self, series_id: int) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE series_tracking SET is_monitored = 1 - is_monitored WHERE id = ?;", (series_id,))
            conn.commit()
            cur.execute("SELECT is_monitored FROM series_tracking WHERE id = ?;", (series_id,))
            row = cur.fetchone()
            return bool(row["is_monitored"]) if row else False

    def update_series_website_url(self, series_id: int, website_url: str):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE series_tracking SET website_url = ? WHERE id = ?;", (website_url.strip(), series_id))
            conn.commit()

    def update_series_details(self, series_id: int, description: Optional[str] = None, cover_url: Optional[str] = None, website_url: Optional[str] = None):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE series_tracking SET
                    description = COALESCE(?, description),
                    cover_url = COALESCE(?, cover_url),
                    website_url = COALESCE(?, website_url)
                WHERE id = ?;
            """, (description, cover_url, website_url, series_id))
            conn.commit()

    # --- Release Operations ---
    def purge_pdf_releases(self) -> int:
        """
        Permanently deletes any PDF releases from the database.
        NovelTrax strictly monitors digital novels/EPUBs and rejects PDFs.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM releases 
                WHERE lower(title) LIKE '%pdf%' 
                   OR lower(link) LIKE '%.pdf%' 
                   OR lower(link) LIKE '%/pdf/%'
                   OR lower(guid) LIKE '%pdf%';
            """)
            deleted_count = cur.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info(f"Purged {deleted_count} PDF release(s) from database.")
            return deleted_count

    def upsert_release(self, release_data: Dict[str, Any]) -> Tuple[bool, int]:
        """
        Inserts or updates a release.
        Returns (is_new, release_id).
        """
        from app.parser import is_pdf_release
        if is_pdf_release(release_data.get("title"), release_data.get("link"), release_data.get("guid")):
            logger.debug(f"Rejecting PDF release: {release_data.get('title')}")
            return False, -1

        from app.providers.metadata import NovelMetadataProvider
        if release_data.get("series_name"):
            release_data["series_name"] = NovelMetadataProvider.clean_novel_title(release_data["series_name"])
        # Retain release_data["title"] strictly intact — do not mutate with clean_novel_title

        guid = release_data["guid"]
        with self.get_connection() as conn:
            cur = conn.cursor()

            # Blacklist guard: never let a release for a blacklisted series become
            # requested/discoverable. The lookup reuses this connection (no nested
            # connection, no extra lock) instead of calling is_series_blacklisted().
            blacklisted = False
            series_name_check = (release_data.get("series_name") or "").strip()
            if series_name_check:
                cur.execute(
                    "SELECT 1 FROM series_blacklist WHERE lower(trim(name)) = lower(trim(?)) LIMIT 1;",
                    (series_name_check,),
                )
                blacklisted = cur.fetchone() is not None

            # Deduplication for discovery (status == 'NEW') releases
            # Only merge if incoming release is a discovery candidate (volume is None or volume == 1.0)
            incoming_vol = release_data.get("volume")
            is_discovery_cand = (incoming_vol is None or incoming_vol == 1.0)
            if release_data.get("status") == "NEW" and release_data.get("series_name") and is_discovery_cand and not blacklisted:
                cur.execute("""
                    SELECT id, description, cover_url, series_link, title, volume FROM releases 
                    WHERE status = 'NEW' 
                      AND (volume IS NULL OR volume = 1.0)
                      AND lower(trim(series_name)) = lower(trim(?));
                """, (release_data["series_name"],))
                dup = cur.fetchone()
                if dup:
                    rel_id = dup["id"]
                    new_title = release_data.get("title")
                    existing_title = dup["title"]
                    # Do not overwrite clean existing title if incoming title has volume noise
                    use_title = existing_title if (existing_title and "volume" not in existing_title.lower() and new_title and "volume" in new_title.lower()) else (new_title or existing_title)
                    cur.execute("""
                        UPDATE releases SET
                            description = COALESCE(?, description),
                            cover_url = COALESCE(?, cover_url),
                            series_link = COALESCE(?, series_link),
                            title = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?;
                    """, (
                        release_data.get("description"),
                        release_data.get("cover_url"),
                        release_data.get("series_link"),
                        use_title,
                        rel_id
                    ))
                    conn.commit()
                    return False, rel_id

            is_bundle_val = 1 if release_data.get("is_bundle") else 0
            is_official_val = 1 if release_data.get("is_official", 1) else 0

            vols_list = release_data.get("volumes")
            if vols_list is None and release_data.get("volumes_json"):
                try:
                    vols_list = json.loads(release_data["volumes_json"])
                except Exception:
                    pass
            vols_val = json.dumps(vols_list or ([release_data["volume"]] if release_data.get("volume") is not None else []))

            missing_list = release_data.get("missing_volumes")
            if missing_list is None and release_data.get("missing_vols_json"):
                try:
                    missing_list = json.loads(release_data["missing_vols_json"])
                except Exception:
                    pass
            missing_val = json.dumps(missing_list or [])

            # Extract release_group if omitted
            rel_group_val = release_data.get("release_group")
            if not rel_group_val and release_data.get("title"):
                try:
                    from app.parser import extract_release_group
                    rel_group_val = extract_release_group(release_data["title"])
                except Exception:
                    rel_group_val = ""
            rel_group_val = (rel_group_val or "").strip()

            dl_url_val = release_data.get("download_url") or release_data.get("link") or ""
            status_in = (release_data.get("status") or "REQUESTED").strip().upper()
            is_ignored = (status_in == "IGNORED") or bool(release_data.get("ignored"))
            ignored_val = 1 if is_ignored else 0
            status_val = "IGNORED" if is_ignored else status_in
            # A blacklisted series may never be stored as pending/discoverable.
            if blacklisted and status_val in ("NEW", "REQUESTED"):
                status_val = "BLACKLISTED"

            cur.execute("SELECT id, status FROM releases WHERE guid = ?;", (guid,))
            existing = cur.fetchone()

            if existing:
                rel_id = existing["id"]
                # Re-ingest updates the payload only: whatever lifecycle status the
                # row already carries (IN_CALIBRE, IGNORED, REQUESTED, ...) is kept
                # so that a later feed scan cannot resurrect or downgrade it.
                cur.execute("""
                    UPDATE releases SET
                        title = ?,
                        series_name = ?,
                        volume = ?,
                        link = ?,
                        pub_date = ?,
                        cover_url = ?,
                        description = ?,
                        author = ?,
                        series_link = COALESCE(?, series_link),
                        is_bundle = ?,
                        volumes_json = ?,
                        missing_vols_json = ?,
                        is_official = ?,
                        download_url = COALESCE(?, download_url),
                        release_group = COALESCE(NULLIF(?, ''), release_group),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (
                    release_data["title"],
                    release_data.get("series_name"),
                    release_data.get("volume"),
                    release_data.get("link"),
                    release_data.get("pub_date"),
                    release_data.get("cover_url"),
                    release_data.get("description"),
                    release_data.get("author"),
                    release_data.get("series_link"),
                    is_bundle_val,
                    vols_val,
                    missing_val,
                    is_official_val,
                    dl_url_val,
                    rel_group_val,
                    rel_id
                ))
                conn.commit()
                return False, rel_id
            else:
                cur.execute("""
                    INSERT INTO releases (
                        guid, title, series_name, volume, link, pub_date,
                        cover_url, description, author, source_feed_id,
                        source_feed_name, series_link, status, calibre_matched_book_id,
                        is_bundle, volumes_json, missing_vols_json, is_official,
                        download_url, release_group, ignored
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    guid,
                    release_data["title"],
                    release_data.get("series_name"),
                    release_data.get("volume"),
                    release_data.get("link"),
                    release_data.get("pub_date"),
                    release_data.get("cover_url"),
                    release_data.get("description"),
                    release_data.get("author"),
                    release_data.get("source_feed_id"),
                    release_data.get("source_feed_name"),
                    release_data.get("series_link"),
                    status_val,
                    release_data.get("calibre_matched_book_id"),
                    is_bundle_val,
                    vols_val,
                    missing_val,
                    is_official_val,
                    dl_url_val,
                    rel_group_val,
                    ignored_val
                ))
                rel_id = cur.lastrowid

                # If this release has a series link, update the tracked series website_url if currently empty
                s_name = release_data.get("series_name")
                s_link = release_data.get("series_link")
                if s_name and s_link:
                    cur.execute("""
                        UPDATE series_tracking 
                        SET website_url = ? 
                        WHERE lower(name) = lower(?) AND (website_url IS NULL OR website_url = '' OR website_url LIKE '%?s=%');
                    """, (s_link, s_name))

                conn.commit()
                return True, rel_id

    def update_release_status(self, release_id: int, status: str, matched_book_id: Optional[int] = None):
        with self.get_connection() as conn:
            cur = conn.cursor()
            clean_status = (status or "").strip().upper()
            ignored_val = 1 if clean_status == 'IGNORED' else 0
            cur.execute("""
                UPDATE releases 
                SET status = ?, ignored = ?, calibre_matched_book_id = COALESCE(?, calibre_matched_book_id), updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (clean_status, ignored_val, matched_book_id, release_id))
            conn.commit()

    def ignore_release(self, release_id: int) -> bool:
        """Marks a release as IGNORED (sets status='IGNORED' and ignored=1)."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE releases 
                SET status = 'IGNORED', ignored = 1, updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?;
            """, (release_id,))
            conn.commit()
            return cur.rowcount > 0

    def unignore_release(self, release_id: int) -> bool:
        """Restores an IGNORED release to REQUESTED (sets status='REQUESTED' and ignored=0)."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE releases 
                SET status = 'REQUESTED', ignored = 0, updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?;
            """, (release_id,))
            conn.commit()
            return cur.rowcount > 0

    # --- Prioritized Release Groups Operations ---

    def ensure_prioritized_groups_table(self):
        """Ensures the prioritized_groups table exists with full schema and default seeding."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prioritized_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE COLLATE NOCASE NOT NULL,
                    is_vip INTEGER DEFAULT 1,
                    notes TEXT DEFAULT '',
                    priority INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("PRAGMA table_info(prioritized_groups);")
            cols = {r["name"] for r in cur.fetchall()}
            if "is_vip" not in cols:
                try:
                    cur.execute("ALTER TABLE prioritized_groups ADD COLUMN is_vip INTEGER DEFAULT 1;")
                except sqlite3.OperationalError:
                    pass
            if "notes" not in cols:
                try:
                    cur.execute("ALTER TABLE prioritized_groups ADD COLUMN notes TEXT DEFAULT '';")
                except sqlite3.OperationalError:
                    pass
            if "priority" not in cols:
                try:
                    cur.execute("ALTER TABLE prioritized_groups ADD COLUMN priority INTEGER DEFAULT 1;")
                except sqlite3.OperationalError:
                    pass

            # Check if existing table lacks COLLATE NOCASE on name column
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='prioritized_groups';")
            tbl_row = cur.fetchone()
            if tbl_row and "collate nocase" not in tbl_row[0].lower():
                # Deduplicate any existing duplicate rows (preserve row with highest priority or lowest id)
                cur.execute("""
                    DELETE FROM prioritized_groups 
                    WHERE id NOT IN (
                        SELECT min(id) FROM prioritized_groups GROUP BY lower(name)
                    );
                """)
                # Rebuild table with COLLATE NOCASE
                cur.execute("""
                    CREATE TABLE prioritized_groups_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE COLLATE NOCASE NOT NULL,
                        is_vip INTEGER DEFAULT 1,
                        notes TEXT DEFAULT '',
                        priority INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    INSERT INTO prioritized_groups_new (id, name, is_vip, notes, priority, created_at)
                    SELECT id, name, is_vip, notes, priority, created_at FROM prioritized_groups;
                """)
                cur.execute("DROP TABLE prioritized_groups;")
                cur.execute("ALTER TABLE prioritized_groups_new RENAME TO prioritized_groups;")

            cur.execute("DROP INDEX IF EXISTS idx_prioritized_groups_name;")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_prioritized_groups_name ON prioritized_groups(name COLLATE NOCASE);")

            cur.execute("SELECT count(*) FROM prioritized_groups;")
            if cur.fetchone()[0] == 0:
                default_groups = [
                    ("Yen Press", "Official English light novel publisher (Yen On)", 1),
                    ("J-Novel Club", "Digital-first light novel publisher & translations", 1),
                    ("Seven Seas", "Official digital light novels (Airship)", 1),
                    ("Stick", "High quality digital light novel releases", 1),
                    ("One Peace Books", "Official light novel releases", 1)
                ]
                for grp, note, prio in default_groups:
                    cur.execute("INSERT OR IGNORE INTO prioritized_groups (name, notes, priority) VALUES (?, ?, ?);", (grp, note, prio))
            conn.commit()

    def get_prioritized_groups(self) -> List[Dict[str, Any]]:
        """Returns all prioritized release groups with live release count."""
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT pg.*, 
                       (SELECT count(*) FROM releases r WHERE lower(trim(COALESCE(r.release_group, ''))) = lower(trim(pg.name))) as release_count
                FROM prioritized_groups pg
                ORDER BY pg.priority DESC, pg.name COLLATE NOCASE ASC;
            """)
            return [dict(r) for r in cur.fetchall()]

    def add_prioritized_group(self, name: str, notes: str = "", priority: int = 1) -> bool:
        """Adds or updates a prioritized release group."""
        clean = name.strip()
        if not clean:
            return False
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO prioritized_groups (name, notes, priority, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET notes = excluded.notes, priority = excluded.priority;
            """, (clean, notes, priority))
            conn.commit()
            return True

    def remove_prioritized_group(self, group_id: int) -> bool:
        """Removes a prioritized group by ID."""
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM prioritized_groups WHERE id = ?;", (group_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    def is_group_prioritized(self, group_name: str) -> bool:
        """Returns True if the group name matches an active prioritized group."""
        if not group_name:
            return False
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM prioritized_groups WHERE lower(trim(name)) = lower(?);", (group_name.strip(),))
            return cur.fetchone() is not None

    def get_prioritized_releases(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
        sort: str = "date_desc"
    ) -> List[Dict[str, Any]]:
        """Returns releases published by prioritized groups."""
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            where_clauses = [
                "lower(trim(COALESCE(r.release_group, ''))) IN (SELECT lower(trim(name)) FROM prioritized_groups)"
            ]
            params: List[Any] = []
            if status == "IGNORED":
                where_clauses.append("(r.status = 'IGNORED' OR r.ignored = 1)")
            elif status:
                where_clauses.append("r.status = ? AND (r.ignored = 0 OR r.ignored IS NULL)")
                params.append(status)
            else:
                where_clauses.append("r.status NOT IN ('BLACKLISTED', 'IGNORED') AND (r.ignored = 0 OR r.ignored IS NULL)")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(r.title) LIKE ? OR lower(COALESCE(r.series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            where_sql = f"WHERE {' AND '.join(where_clauses)}"
            sort_sql = "ORDER BY COALESCE(r.pub_date, r.updated_at) DESC, r.id DESC"
            if sort == "date_asc":
                sort_sql = "ORDER BY COALESCE(r.pub_date, r.updated_at) ASC, r.id ASC"
            elif sort in ("title_asc", "name_asc"):
                sort_sql = "ORDER BY r.title COLLATE NOCASE ASC, r.id ASC"
            elif sort in ("title_desc", "name_desc"):
                sort_sql = "ORDER BY r.title COLLATE NOCASE DESC, r.id DESC"
            elif sort == "status":
                sort_sql = "ORDER BY CASE r.status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END, r.updated_at DESC, r.id DESC"

            cur.execute(f"""
                SELECT r.* FROM releases r
                {where_sql}
                {sort_sql}
                LIMIT ? OFFSET ?;
            """, (*params, limit, offset))
            items = [dict(row) for row in cur.fetchall()]
            for item in items:
                item["is_prioritized"] = True
                item["ignored"] = bool(item.get("ignored", 0) or item.get("status") == "IGNORED")
                try:
                    item["volumes"] = json.loads(item.get("volumes_json") or "[]")
                except Exception:
                    item["volumes"] = [item["volume"]] if item.get("volume") is not None else []
                try:
                    item["missing_volumes"] = json.loads(item.get("missing_vols_json") or "[]")
                except Exception:
                    item["missing_volumes"] = []
            return items

    def count_prioritized_releases(self, status: Optional[str] = None, search: Optional[str] = None) -> int:
        """Returns total count of releases matching prioritized groups."""
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            where_clauses = [
                "lower(trim(COALESCE(r.release_group, ''))) IN (SELECT lower(trim(name)) FROM prioritized_groups)"
            ]
            params: List[Any] = []
            if status == "IGNORED":
                where_clauses.append("(r.status = 'IGNORED' OR r.ignored = 1)")
            elif status:
                where_clauses.append("r.status = ? AND (r.ignored = 0 OR r.ignored IS NULL)")
                params.append(status)
            else:
                where_clauses.append("r.status NOT IN ('BLACKLISTED', 'IGNORED') AND (r.ignored = 0 OR r.ignored IS NULL)")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(r.title) LIKE ? OR lower(COALESCE(r.series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            cur.execute(f"SELECT count(*) FROM releases r WHERE {' AND '.join(where_clauses)};", tuple(params))
            row = cur.fetchone()
            return row[0] if row else 0

    def update_download_status(self, release_id: int, status: str, torrent_hash: Optional[str] = None):
        clean_hash = str(torrent_hash).strip() if (isinstance(torrent_hash, str) and torrent_hash.strip()) else None
        with self.get_connection() as conn:
            cur = conn.cursor()
            if clean_hash:
                cur.execute("""
                    UPDATE releases 
                    SET download_status = ?, torrent_hash = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?;
                """, (status, clean_hash, release_id))
            else:
                cur.execute("""
                    UPDATE releases 
                    SET download_status = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?;
                """, (status, release_id))
            conn.commit()

    def get_release_by_id(self, release_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM releases WHERE id = ?;", (release_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_releases(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
        sort: str = "date_desc",
        include_ignored: bool = False
    ) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            where_clauses = []
            params = []
            if status == "IGNORED":
                where_clauses.append("(status = 'IGNORED' OR ignored = 1)")
            elif status:
                where_clauses.append("status = ? AND (ignored = 0 OR ignored IS NULL)")
                params.append(status)
            else:
                if not include_ignored:
                    where_clauses.append("status NOT IN ('BLACKLISTED', 'IGNORED') AND (ignored = 0 OR ignored IS NULL)")
                else:
                    where_clauses.append("status != 'BLACKLISTED'")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(title) LIKE ? OR lower(COALESCE(series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            # Sort mapping
            sort_sql = "ORDER BY updated_at DESC, id DESC"
            if sort == "date_asc":
                sort_sql = "ORDER BY COALESCE(pub_date, updated_at) ASC, id ASC"
            elif sort == "title_asc":
                sort_sql = "ORDER BY title COLLATE NOCASE ASC, id ASC"
            elif sort == "title_desc":
                sort_sql = "ORDER BY title COLLATE NOCASE DESC, id DESC"
            elif sort == "series_asc":
                sort_sql = "ORDER BY COALESCE(NULLIF(series_name, ''), title) COLLATE NOCASE ASC, id ASC"
            elif sort == "series_desc":
                sort_sql = "ORDER BY COALESCE(NULLIF(series_name, ''), title) COLLATE NOCASE DESC, id DESC"
            elif sort == "status":
                sort_sql = "ORDER BY CASE status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END, updated_at DESC, id DESC"
            elif sort == "date_desc":
                sort_sql = "ORDER BY COALESCE(pub_date, updated_at) DESC, id DESC"

            cur.execute(f"""
                SELECT * FROM releases 
                {where_sql}
                {sort_sql}
                LIMIT ? OFFSET ?;
            """, (*params, limit, offset))
            items = [dict(row) for row in cur.fetchall()]

            try:
                cur.execute("SELECT lower(trim(name)) FROM prioritized_groups;")
                p_groups = {row[0] for row in cur.fetchall()}
            except sqlite3.OperationalError:
                p_groups = set()

            for item in items:
                rg = (item.get("release_group") or "").strip().lower()
                item["is_prioritized"] = bool(rg and rg in p_groups)
                item["ignored"] = bool(item.get("ignored", 0) or item.get("status") == "IGNORED")
                try:
                    item["volumes"] = json.loads(item.get("volumes_json") or "[]")
                except Exception:
                    item["volumes"] = [item["volume"]] if item.get("volume") is not None else []
                try:
                    item["missing_volumes"] = json.loads(item.get("missing_vols_json") or "[]")
                except Exception:
                    item["missing_volumes"] = []
            return items

    def count_releases(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        include_ignored: bool = False
    ) -> int:
        """Returns total count of releases, optionally filtered by status and search query."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            where_clauses = []
            params = []
            if status == "IGNORED":
                where_clauses.append("(status = 'IGNORED' OR ignored = 1)")
            elif status:
                where_clauses.append("status = ? AND (ignored = 0 OR ignored IS NULL)")
                params.append(status)
            else:
                if not include_ignored:
                    where_clauses.append("status NOT IN ('BLACKLISTED', 'IGNORED') AND (ignored = 0 OR ignored IS NULL)")
                else:
                    where_clauses.append("status != 'BLACKLISTED'")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(title) LIKE ? OR lower(COALESCE(series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            cur.execute(f"SELECT count(*) FROM releases {where_sql};", tuple(params))
            row = cur.fetchone()
            return row[0] if row else 0

    def get_releases_grouped_by_series(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        search: Optional[str] = None,
        sort: str = "wanted_first",
        prioritized_only: bool = False,
        include_ignored: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Groups releases under canonical series, aggregating all bundle volumes,
        highlighting owned vs missing volumes against Calibre tracking.
        Respects track_official_only setting by filtering out unofficial fan volumes.
        Supports fast pagination with limit & offset and instant search & sort.
        """
        from app.parser import normalize_text, get_canonical_series_name

        track_official_only = self.get_setting("track_official_only", "true").lower() == "true"

        with self.get_connection() as conn:
            cur = conn.cursor()

            if status == "IGNORED":
                cur.execute("""
                    SELECT * FROM releases 
                    WHERE status = 'IGNORED' OR ignored = 1
                    ORDER BY updated_at DESC, id DESC;
                """)
            elif status:
                cur.execute("""
                    SELECT * FROM releases 
                    WHERE status = ? AND (ignored = 0 OR ignored IS NULL)
                    ORDER BY updated_at DESC, id DESC;
                """, (status,))
            else:
                if not include_ignored:
                    cur.execute("""
                        SELECT * FROM releases 
                        WHERE status NOT IN ('BLACKLISTED', 'IGNORED') AND (ignored = 0 OR ignored IS NULL)
                        ORDER BY 
                            CASE status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END,
                            updated_at DESC, id DESC;
                    """)
                else:
                    cur.execute("""
                        SELECT * FROM releases 
                        WHERE status != 'BLACKLISTED'
                        ORDER BY 
                            CASE status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END,
                            updated_at DESC, id DESC;
                    """)

            if prioritized_only:
                self.ensure_prioritized_groups_table()

            raw_releases = [dict(r) for r in cur.fetchall()]

            try:
                cur.execute("SELECT lower(trim(name)) FROM prioritized_groups;")
                p_groups = {row[0] for row in cur.fetchall()}
            except sqlite3.OperationalError:
                p_groups = set()

            for r in raw_releases:
                rg = (r.get("release_group") or "").strip().lower()
                r["is_prioritized"] = bool(rg and rg in p_groups)
                r["ignored"] = bool(r.get("ignored", 0) or r.get("status") == "IGNORED")
                try:
                    r["volumes"] = json.loads(r.get("volumes_json") or "[]")
                except Exception:
                    r["volumes"] = [r["volume"]] if r.get("volume") is not None else []
                try:
                    r["missing_volumes"] = json.loads(r.get("missing_vols_json") or "[]")
                except Exception:
                    r["missing_volumes"] = []

            cur.execute("SELECT * FROM series_tracking;")
            tracked_rows = [dict(r) for r in cur.fetchall()]
            # Pre-index tracked series by normalized name AND component sub-titles for instant O(1) lookups
            tracked_by_norm: Dict[str, Dict[str, Any]] = {}
            for t_row in tracked_rows:
                t_name = t_row["name"]
                t_norm = normalize_text(t_name)
                tracked_by_norm[t_norm] = t_row
                # Also index component sub-titles for pipe/slash separated titles
                if "|" in t_name or "/" in t_name:
                    for sub in re.split(r'\s*[|/]\s*', t_name):
                        sub_norm = normalize_text(sub)
                        if len(sub_norm) >= 3 and sub_norm not in tracked_by_norm:
                            tracked_by_norm[sub_norm] = t_row

            groups_dict: Dict[str, Dict[str, Any]] = {}
            link_to_group_key: Dict[str, str] = {}

            for rel in raw_releases:
                is_off = rel.get("is_official")
                if is_off is None:
                    is_off = 1
                is_official = (is_off != 0)

                # If track_official_only is active, mark unofficial releases as IGNORED
                if track_official_only and not is_official:
                    if rel.get("status") != "IGNORED":
                        rel["status"] = "IGNORED"

                s_name = (rel.get("series_name") or "Uncategorized").strip()
                canonical_s_name = get_canonical_series_name(s_name)
                s_key = normalize_text(canonical_s_name) or "uncategorized"
                rel_link = (rel.get("series_link") or "").strip().rstrip("/")

                # Instant O(1) group key resolution without quadratic fuzzy scanning
                matched_key = None
                if s_key in groups_dict:
                    matched_key = s_key
                elif rel_link and rel_link in link_to_group_key:
                    matched_key = link_to_group_key[rel_link]
                else:
                    if "|" in s_name or "/" in s_name:
                        for sub in re.split(r'\s*[|/]\s*', s_name):
                            sub_key = normalize_text(sub)
                            if sub_key in groups_dict:
                                matched_key = sub_key
                                break

                if not matched_key:
                    matched_key = s_key
                    # Instant O(1) Calibre match lookup
                    calibre_match = tracked_by_norm.get(s_key)
                    if not calibre_match and ("|" in s_name or "/" in s_name):
                        for sub in re.split(r'\s*[|/]\s*', s_name):
                            sub_norm = normalize_text(sub)
                            if sub_norm in tracked_by_norm:
                                calibre_match = tracked_by_norm[sub_norm]
                                break

                    canonical_title = (calibre_match.get("name") if calibre_match else None) or canonical_s_name
                    cover = rel.get("cover_url") or (calibre_match.get("cover_url") if calibre_match else "") or ""
                    synopsis = rel.get("description") or (calibre_match.get("description") if calibre_match else "") or ""
                    website_url = rel.get("series_link") or (calibre_match.get("website_url") if calibre_match else "") or ""

                    groups_dict[matched_key] = {
                        "series_key": matched_key,
                        "series_name": canonical_title,
                        "raw_name": s_name,
                        "cover_url": cover,
                        "in_calibre": calibre_match is not None,
                        "is_monitored": bool(calibre_match.get("is_monitored", 0)) if calibre_match else False,
                        "calibre_series_id": calibre_match.get("calibre_series_id") if calibre_match else None,
                        "calibre_book_count": calibre_match.get("book_count", 0) if calibre_match else 0,
                        "calibre_latest_index": calibre_match.get("latest_index", 0.0) if calibre_match else 0.0,
                        "synopsis": synopsis,
                        "website_url": website_url,
                        "releases": [],
                        "releases_count": 0,
                        "all_volumes_set": set(),
                        "missing_volumes_set": set(),
                        "has_wanted": False,
                        "has_discovery": False,
                        "has_in_calibre": False,
                        "has_prioritized": False
                    }

                group = groups_dict[matched_key]
                if rel_link and rel_link not in link_to_group_key:
                    link_to_group_key[rel_link] = matched_key

                group["releases"].append(rel)
                group["releases_count"] += 1
                if rel.get("is_prioritized"):
                    group["has_prioritized"] = True

                if not group["cover_url"] and rel.get("cover_url"):
                    group["cover_url"] = rel["cover_url"]
                if not group["synopsis"] and rel.get("description"):
                    group["synopsis"] = rel["description"]
                if not group["website_url"] and rel.get("series_link"):
                    group["website_url"] = rel["series_link"]

                # If unofficial release and track_official_only is True, skip aggregating volume into wanted coverage
                if track_official_only and not is_official:
                    continue

                # Aggregate volumes
                rel_vols = rel.get("volumes", [])
                if not rel_vols:
                    v = rel.get("volume")
                    group["all_volumes_set"].add(v if v is not None else 1.0)
                else:
                    for v in rel_vols:
                        group["all_volumes_set"].add(v)
                for mv in rel.get("missing_volumes", []):
                    group["missing_volumes_set"].add(mv)

                st = rel.get("status")
                if st == "REQUESTED":
                    group["has_wanted"] = True
                elif st == "NEW":
                    group["has_discovery"] = True
                elif st == "IN_CALIBRE":
                    group["has_in_calibre"] = True

            result_groups = []
            for g in groups_dict.values():
                all_vols = sorted(list(g["all_volumes_set"]))
                missing_vols = sorted(list(g["missing_volumes_set"]))
                g["all_volumes"] = all_vols
                g["missing_volumes"] = missing_vols
                latest_owned = g["calibre_latest_index"]

                # Determine if books from this series are present in Calibre
                has_calibre_match = (
                    g["in_calibre"] 
                    or g["has_in_calibre"]
                    or any(r.get("calibre_matched_book_id") is not None for r in g["releases"])
                    or any(r.get("status") == "IN_CALIBRE" for r in g["releases"])
                )

                if latest_owned > 0.0:
                    g["owned_volumes"] = [v for v in all_vols if v <= latest_owned and v not in missing_vols]
                elif has_calibre_match:
                    g["owned_volumes"] = [v for v in all_vols if v not in missing_vols]
                else:
                    g["owned_volumes"] = []

                # If Volume 1 or any book is owned in Calibre, mark the entire series group as In Calibre!
                if has_calibre_match or len(g["owned_volumes"]) > 0:
                    g["in_calibre"] = True
                    g["has_discovery"] = False

                del g["all_volumes_set"]
                del g["missing_volumes_set"]
                dates = [str(r.get("pub_date") or r.get("updated_at") or r.get("created_at") or "") for r in g["releases"]]
                valid_dates = [d for d in dates if d]
                g["latest_date"] = max(valid_dates) if valid_dates else ""
                g["releases_count"] = len(g["releases"])
                result_groups.append(g)

            # Filter prioritized only if requested
            if prioritized_only:
                result_groups = [g for g in result_groups if g.get("has_prioritized")]

            # Instant keyword search filter across series name, synopsis, and release titles
            if search and search.strip():
                clean_s = search.strip().lower()
                result_groups = [
                    g for g in result_groups
                    if clean_s in g["series_name"].lower()
                    or clean_s in (g.get("synopsis") or "").lower()
                    or any(clean_s in (r.get("title") or "").lower() for r in g["releases"])
                ]

            # Sort groups by requested sorting criteria
            if sort in ("wanted_first", "default") or not sort:
                result_groups.sort(key=lambda item: (
                    not (item["has_wanted"] and item.get("has_prioritized")),
                    not item["has_wanted"],
                    not item.get("has_prioritized"),
                    -len(item.get("missing_volumes", [])),
                    item["series_name"].lower()
                ))
            elif sort == "date_desc":
                result_groups.sort(key=lambda item: item["series_name"].lower())
                result_groups.sort(key=lambda item: item["latest_date"], reverse=True)
            elif sort == "date_asc":
                result_groups.sort(key=lambda item: item["series_name"].lower())
                result_groups.sort(key=lambda item: item["latest_date"] or "9999", reverse=False)
            elif sort in ("title_asc", "name_asc"):
                result_groups.sort(key=lambda item: item["series_name"].lower())
            elif sort in ("title_desc", "name_desc"):
                result_groups.sort(key=lambda item: item["series_name"].lower(), reverse=True)
            elif sort == "releases_desc":
                result_groups.sort(key=lambda item: (-item["releases_count"], item["series_name"].lower()))
            else:
                # Legacy 7-tier priority sort (for sort="priority" or sort="legacy")
                def sort_key(item):
                    if item["has_wanted"] and item.get("has_prioritized"):
                        prio = 1
                    elif item["has_wanted"]:
                        prio = 2
                    elif item.get("has_prioritized"):
                        prio = 3
                    elif item["is_monitored"]:
                        prio = 4
                    elif item["has_discovery"]:
                        prio = 5
                    elif item["in_calibre"]:
                        prio = 6
                    else:
                        prio = 7
                    return (prio, item["series_name"].lower())
                result_groups.sort(key=sort_key)

            if limit is not None:
                return result_groups[offset : offset + limit]
            return result_groups

    def count_releases_grouped_by_series(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        prioritized_only: bool = False,
        include_ignored: bool = False
    ) -> int:
        """Returns the total number of series groups matching status and search filters."""
        groups = self.get_releases_grouped_by_series(
            status=status,
            search=search,
            prioritized_only=prioritized_only,
            include_ignored=include_ignored
        )
        return len(groups)

    def get_release_status_counts(self) -> Dict[str, int]:
        """Returns total count and breakdowns for each release status."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    count(*) as total,
                    sum(CASE WHEN status = 'REQUESTED' THEN 1 ELSE 0 END) as requested,
                    sum(CASE WHEN status = 'IN_CALIBRE' THEN 1 ELSE 0 END) as in_calibre,
                    sum(CASE WHEN status = 'IGNORED' THEN 1 ELSE 0 END) as ignored
                FROM releases;
            """)
            row = cur.fetchone()
            return {
                "ALL": row["total"] or 0,
                "REQUESTED": row["requested"] or 0,
                "IN_CALIBRE": row["in_calibre"] or 0,
                "IGNORED": row["ignored"] or 0
            }

    def get_pending_requests(self) -> List[Dict[str, Any]]:
        """Returns all releases currently marked as REQUESTED."""
        return self.get_releases(status="REQUESTED", limit=1000)

    # --- Feed Operations ---
    def get_all_feeds(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM feeds ORDER BY id ASC;")
            return [dict(row) for row in cur.fetchall()]

    def get_feed(self, feed_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM feeds WHERE id = ?;", (feed_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def add_feed(self, name: str, feed_type: str, url: str, config_json: str = "{}", auto_request: bool = True, interval: int = 60) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO feeds (name, feed_type, url, config_json, enabled, auto_request, check_interval_minutes)
                VALUES (?, ?, ?, ?, 1, ?, ?);
            """, (name, feed_type, url, config_json, 1 if auto_request else 0, interval))
            conn.commit()
            return cur.lastrowid

    def update_feed(self, feed_id: int, name: str, feed_type: str, url: str, config_json: str, enabled: bool, auto_request: bool, interval: int):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE feeds SET
                    name = ?,
                    feed_type = ?,
                    url = ?,
                    config_json = ?,
                    enabled = ?,
                    auto_request = ?,
                    check_interval_minutes = ?
                WHERE id = ?;
            """, (name, feed_type, url, config_json, 1 if enabled else 0, 1 if auto_request else 0, interval, feed_id))
            conn.commit()

    def update_all_feed_intervals(self, interval: int):
        """Updates check_interval_minutes across all configured feeds."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE feeds SET check_interval_minutes = ?;", (interval,))
            conn.commit()

    def delete_feed(self, feed_id: int):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM feeds WHERE id = ?;", (feed_id,))
            conn.commit()

    def update_feed_status(self, feed_id: int, status: str, error: Optional[str] = None):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE feeds SET
                    last_checked = CURRENT_TIMESTAMP,
                    last_status = ?,
                    last_error = ?
                WHERE id = ?;
            """, (status, error, feed_id))
            conn.commit()

    # --- Activity Logs ---
    def log_activity(self, event_type: str, message: str, details: str = ""):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO activity_logs (event_type, message, details)
                VALUES (?, ?, ?);
            """, (event_type, message, details))
            conn.commit()

    def get_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM activity_logs ORDER BY id DESC LIMIT ?;", (limit,))
            return [dict(row) for row in cur.fetchall()]

    # --- Dashboard Summary Stats ---
    def get_dashboard_stats(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM series_tracking WHERE is_monitored = 1;")
            monitored_series = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM releases WHERE status = 'REQUESTED';")
            pending_requests = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM releases WHERE status = 'IN_CALIBRE';")
            acquired_books = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM feeds WHERE enabled = 1;")
            active_feeds = cur.fetchone()[0]

            discovered_count = self.count_discovery_releases()

            cur.execute("SELECT count(*) FROM series_blacklist;")
            blacklisted_count = cur.fetchone()[0]

            return {
                "monitored_series": monitored_series,
                "pending_requests": pending_requests,
                "acquired_books": acquired_books,
                "active_feeds": active_feeds,
                "discovered_count": discovered_count,
                "blacklisted_count": blacklisted_count
            }

    # --- Blacklist Operations ---
    def add_to_blacklist(self, series_name: str, reason: str = "") -> bool:
        """
        Adds a series to the blacklist and cancels/clears any existing pending requests for it.
        """
        clean_name = series_name.strip()
        if not clean_name:
            return False
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO series_blacklist (name, reason, created_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET reason = excluded.reason;
            """, (clean_name, reason))

            # Automatically cancel any pending requests for this series
            cur.execute("""
                UPDATE releases 
                SET status = 'BLACKLISTED', updated_at = CURRENT_TIMESTAMP
                WHERE lower(series_name) = lower(?) AND status = 'REQUESTED';
            """, (clean_name,))

            # Also disable tracking if present in series_tracking
            cur.execute("""
                UPDATE series_tracking
                SET is_monitored = 0
                WHERE lower(name) = lower(?);
            """, (clean_name,))

            conn.commit()
            return True

    def remove_from_blacklist(self, blacklist_id: Union[int, str]):
        """
        Removes a series from the blacklist.

        Accepts either the blacklist row id (int or digit-only string) or a
        series-name string, which is matched case-insensitively.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            if isinstance(blacklist_id, int) or (
                isinstance(blacklist_id, str) and blacklist_id.strip().isdigit()
            ):
                cur.execute("DELETE FROM series_blacklist WHERE id = ?;", (int(blacklist_id),))
            else:
                cur.execute(
                    "DELETE FROM series_blacklist WHERE lower(name) = lower(?);",
                    (str(blacklist_id or "").strip(),),
                )
            conn.commit()

    def get_blacklisted_series(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_blacklist ORDER BY name COLLATE NOCASE ASC;")
            return [dict(row) for row in cur.fetchall()]

    def is_series_blacklisted(self, series_name: str) -> bool:
        if not series_name:
            return False
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM series_blacklist WHERE lower(name) = lower(?);", (series_name.strip(),))
            return cur.fetchone() is not None

    # --- Discovery Operations ---
    def get_discovery_releases(
        self,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
        sort: str = "date_desc"
    ) -> List[Dict[str, Any]]:
        """
        Returns releases that are Volume 1 or unnumbered (standalone),
        which are NOT currently in Calibre, NOT in series_tracking, NOT blacklisted,
        and are still unreviewed (status='NEW'). Pending wishlist requests (status
        'REQUESTED') are intentionally not Discovery items.
        Deduplicates by series name so each series appears at most once.
        Strictly excludes PDF releases.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            search_clause = ""
            search_params = []
            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                search_clause = "AND (lower(r.title) LIKE ? OR lower(COALESCE(r.series_name, '')) LIKE ? OR lower(COALESCE(r.description, '')) LIKE ?)"
                search_params = [clean_s, clean_s, clean_s]

            # Sorting clause for discovery
            sort_sql = "ORDER BY updated_at DESC, id DESC"
            if sort == "date_asc":
                sort_sql = "ORDER BY COALESCE(pub_date, updated_at) ASC, id ASC"
            elif sort == "title_asc":
                sort_sql = "ORDER BY title COLLATE NOCASE ASC, id ASC"
            elif sort == "title_desc":
                sort_sql = "ORDER BY title COLLATE NOCASE DESC, id DESC"
            elif sort in ("series_asc", "name_asc"):
                sort_sql = "ORDER BY COALESCE(NULLIF(series_name, ''), title) COLLATE NOCASE ASC, id ASC"
            elif sort in ("series_desc", "name_desc"):
                sort_sql = "ORDER BY COALESCE(NULLIF(series_name, ''), title) COLLATE NOCASE DESC, id DESC"
            elif sort == "date_desc":
                sort_sql = "ORDER BY COALESCE(pub_date, updated_at) DESC, id DESC"


            query = f"""
                WITH ranked_discovery AS (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY lower(trim(
                                   REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(r.series_name, ' (Light Novel)', ''), ' [Light Novel]', ''), ' (Novel)', ''), ' (Web Novel)', ''), ' (LN)', '')
                               ))
                               ORDER BY 
                                   (CASE WHEN r.description IS NOT NULL AND length(trim(r.description)) > 0 THEN 1 ELSE 0 END) DESC,
                                   (CASE WHEN r.cover_url IS NOT NULL AND length(trim(r.cover_url)) > 0 THEN 1 ELSE 0 END) DESC,
                                   r.updated_at DESC,
                                   r.id DESC
                           ) as rn
                    FROM releases r
                    WHERE (r.volume IS NULL OR r.volume = 1.0)
                      AND r.status NOT IN ('IN_CALIBRE', 'BLACKLISTED', 'IGNORED', 'REQUESTED')
                      AND COALESCE(lower(r.title), '') NOT LIKE '%pdf%'
                      AND COALESCE(lower(r.link), '') NOT LIKE '%.pdf%'
                      AND COALESCE(lower(r.guid), '') NOT LIKE '%pdf%'
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking WHERE is_monitored = 1))
                      {search_clause}
                )
                SELECT * FROM ranked_discovery
                WHERE rn = 1
                {sort_sql}
                LIMIT ? OFFSET ?;
            """
            cur.execute(query, (*search_params, limit, offset))
            items = []
            for row in cur.fetchall():
                d = dict(row)
                from app.providers.metadata import NovelMetadataProvider
                if d.get("series_name"):
                    d["series_name"] = NovelMetadataProvider.clean_novel_title(d["series_name"])
                if d.get("title"):
                    d["title"] = NovelMetadataProvider.clean_novel_title(d["title"])
                items.append(d)
            return items

    def count_discovery_releases(self, search: Optional[str] = None) -> int:
        """
        Returns total count of unique discovered series (Volume 1 or unnumbered standalones)
        not in Calibre, tracking, or blacklist, which are still unreviewed (status='NEW').
        Pending wishlist requests (status='REQUESTED') are not Discovery items.
        Strictly excludes PDF releases.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            search_clause = ""
            search_params = []
            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                search_clause = "AND (lower(r.title) LIKE ? OR lower(COALESCE(r.series_name, '')) LIKE ? OR lower(COALESCE(r.description, '')) LIKE ?)"
                search_params = [clean_s, clean_s, clean_s]

            query = f"""
                WITH ranked_discovery AS (
                    SELECT r.id,
                           ROW_NUMBER() OVER (
                               PARTITION BY lower(trim(
                                   REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(r.series_name, ' (Light Novel)', ''), ' [Light Novel]', ''), ' (Novel)', ''), ' (Web Novel)', ''), ' (LN)', '')
                               ))
                               ORDER BY r.id DESC
                           ) as rn
                    FROM releases r
                    WHERE (r.volume IS NULL OR r.volume = 1.0)
                      AND r.status NOT IN ('IN_CALIBRE', 'BLACKLISTED', 'IGNORED', 'REQUESTED')
                      AND COALESCE(lower(r.title), '') NOT LIKE '%pdf%'
                      AND COALESCE(lower(r.link), '') NOT LIKE '%.pdf%'
                      AND COALESCE(lower(r.guid), '') NOT LIKE '%pdf%'
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking WHERE is_monitored = 1))
                      {search_clause}
                )
                SELECT count(*) FROM ranked_discovery WHERE rn = 1;
            """
            cur.execute(query, tuple(search_params))
            row = cur.fetchone()
            return row[0] if row else 0

    def deduplicate_and_clean_discovery(self):
        """
        Cleans existing release records in database:
        1. Purges all historical PDF releases.
        2. Fixes falsely classified standalones / unnumbered releases by re-parsing titles (ensures Volume 4 etc. are not marked as Standalone).
        3. Strips dangling '(Light Novel)', '(Novel)', '[Light Novel]', '(LN)' from series_name and title.
        4. Flags known unofficial/fan web novel translations (e.g. Cheat Ability Vol 9-13) as is_official = 0 and IGNORED.
        5. Normalizes alias series names to canonical titles.
        6. Merges and deduplicates 'NEW' discovery releases with identical, alias, shared series_link, or fuzzy-matching series_names.
        """
        # 1. Purge all PDF releases
        self.purge_pdf_releases()

        from app.parser import extract_series_and_volume, get_canonical_series_name, SERIES_ALIASES, normalize_text
        from app.providers.metadata import NovelMetadataProvider

        with self.get_connection() as conn:
            cur = conn.cursor()

            # 2. Flag unofficial fan translations
            cur.execute("""
                UPDATE releases 
                SET is_official = 0, status = 'IGNORED'
                WHERE (lower(series_name) LIKE '%cheat ability%' OR lower(series_name) LIKE '%cheat skill%')
                  AND volume > 8.0;
            """)

            # 3. Re-parse releases with volume IS NULL or status = 'NEW'
            cur.execute("""
                SELECT id, title, series_name, volume 
                FROM releases 
                WHERE volume IS NULL OR status = 'NEW';
            """)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r_id = r["id"]
                t = r.get("title") or ""
                p = extract_series_and_volume(t)
                clean_s = NovelMetadataProvider.clean_novel_title(p["series_name"] or r.get("series_name") or "")
                canonical_s = get_canonical_series_name(clean_s)

                if p["volume"] is not None and p["volume"] > 1.0:
                    cur.execute("UPDATE releases SET volume = ?, series_name = ? WHERE id = ?;", (p["volume"], canonical_s, r_id))
                elif canonical_s and canonical_s != r.get("series_name"):
                    cur.execute("UPDATE releases SET series_name = ? WHERE id = ?;", (canonical_s, r_id))

            # 4. Clean '(Light Novel)', '(Novel)', etc. from series_name and title in releases
            cur.execute("""
                UPDATE releases 
                SET series_name = trim(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(series_name, ' (Light Novel)', ''), ' [Light Novel]', ''), ' (Novel)', ''), ' (Web Novel)', ''), ' (LN)', '')),
                    title = trim(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(title, ' (Light Novel)', ''), ' [Light Novel]', ''), ' (Novel)', ''), ' (Web Novel)', ''), ' (LN)', ''))
                WHERE series_name LIKE '%(Light Novel)%' 
                   OR series_name LIKE '%[Light Novel]%'
                   OR series_name LIKE '%(Novel)%'
                   OR series_name LIKE '%(Web Novel)%'
                   OR series_name LIKE '%(LN)%'
                   OR title LIKE '%(Light Novel)%'
                   OR title LIKE '%[Light Novel]%'
                   OR title LIKE '%(Novel)%'
                   OR title LIKE '%(Web Novel)%'
                   OR title LIKE '%(LN)%';
            """)

            # 5. Apply canonical series names across all releases
            for group in SERIES_ALIASES:
                canon = group["canonical"]
                for alias in group["aliases"]:
                    cur.execute("""
                        UPDATE releases 
                        SET series_name = ?
                        WHERE lower(trim(series_name)) = lower(trim(?));
                    """, (canon, alias))

            # 6. Alias & series_link fast O(N) deduplication for 'NEW' status discovery releases
            cur.execute("""
                SELECT id, title, series_name, volume, description, cover_url, series_link, updated_at
                FROM releases
                WHERE status = 'NEW' AND (volume IS NULL OR volume = 1.0) AND series_name IS NOT NULL AND series_name != ''
                ORDER BY 
                    (CASE WHEN description IS NOT NULL AND length(trim(description)) > 0 THEN 1 ELSE 0 END) DESC,
                    (CASE WHEN cover_url IS NOT NULL AND length(trim(cover_url)) > 0 THEN 1 ELSE 0 END) DESC,
                    updated_at DESC, id DESC;
            """)
            disc_records = [dict(r) for r in cur.fetchall()]

            seen_canon_map: Dict[str, dict] = {}
            seen_link_map: Dict[str, str] = {}
            to_delete_ids = []
            master_updates: Dict[int, Tuple[Optional[str], Optional[str], Optional[str]]] = {}

            for rec in disc_records:
                s_name = rec["series_name"]
                canon_name = get_canonical_series_name(s_name)
                s_key = normalize_text(canon_name)
                s_link = (rec.get("series_link") or "").strip().rstrip("/")

                target_key = None
                if s_key in seen_canon_map:
                    target_key = s_key
                elif s_link and s_link in seen_link_map:
                    target_key = seen_link_map[s_link]
                elif "|" in s_name or "/" in s_name:
                    for sub in re.split(r'\s*[|/]\s*', s_name):
                        sub_key = normalize_text(sub)
                        if sub_key and sub_key in seen_canon_map:
                            target_key = sub_key
                            break

                if target_key:
                    master = seen_canon_map[target_key]
                    to_delete_ids.append(rec["id"])
                    m_desc = master["description"] or rec["description"]
                    m_cover = master["cover_url"] or rec["cover_url"]
                    m_link = master["series_link"] or rec["series_link"]
                    master["description"] = m_desc
                    master["cover_url"] = m_cover
                    master["series_link"] = m_link
                    master_updates[master["id"]] = (m_desc, m_cover, m_link)
                else:
                    seen_canon_map[s_key] = rec
                    if s_link:
                        seen_link_map[s_link] = s_key
                    if "|" in s_name or "/" in s_name:
                        for sub in re.split(r'\s*[|/]\s*', s_name):
                            sub_key = normalize_text(sub)
                            if sub_key and sub_key not in seen_canon_map:
                                seen_canon_map[sub_key] = rec

            if to_delete_ids:
                cur.executemany("DELETE FROM releases WHERE id = ?;", [(i,) for i in to_delete_ids])
            if master_updates:
                cur.executemany("""
                    UPDATE releases 
                    SET description = ?, cover_url = ?, series_link = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, [(desc, cover, link, mid) for mid, (desc, cover, link) in master_updates.items()])

            conn.commit()

    def update_release_details(self, release_id: int, description: Optional[str] = None, series_link: Optional[str] = None, cover_url: Optional[str] = None, title: Optional[str] = None, series_name: Optional[str] = None):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE releases 
                SET description = COALESCE(?, description),
                    series_link = COALESCE(?, series_link),
                    cover_url = COALESCE(?, cover_url),
                    title = COALESCE(?, title),
                    series_name = COALESCE(?, series_name),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (description, series_link, cover_url, title, series_name, release_id))
            conn.commit()

    def track_discovered_series(self, series_name: str, release_id: Optional[int] = None) -> bool:
        """
        Adds a discovered series to series_tracking (monitored = 1) and
        updates associated release status to REQUESTED.
        Preserves canonical series hub link, synopsis, and cover if present.
        """
        clean_name = series_name.strip()
        if not clean_name:
            return False
        from urllib.parse import quote_plus
        with self.get_connection() as conn:
            cur = conn.cursor()
            default_link = f"https://jnovels.com/?s={quote_plus(clean_name)}"
            desc = None
            cover = None

            # If release has a canonical series_link, description, or cover, carry them over
            if release_id:
                cur.execute("SELECT series_link, description, cover_url FROM releases WHERE id = ?;", (release_id,))
                row = cur.fetchone()
                if row:
                    if row["series_link"]:
                        default_link = row["series_link"]
                    desc = row["description"]
                    cover = row["cover_url"]

            cur.execute("""
                INSERT INTO series_tracking (name, book_count, latest_index, is_monitored, website_url, description, cover_url, last_sync)
                VALUES (?, 0, 0.0, 1, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET 
                    is_monitored = 1,
                    description = COALESCE(excluded.description, series_tracking.description),
                    cover_url = COALESCE(excluded.cover_url, series_tracking.cover_url),
                    website_url = CASE WHEN series_tracking.website_url = '' OR series_tracking.website_url IS NULL OR series_tracking.website_url LIKE '%?s=%' THEN excluded.website_url ELSE series_tracking.website_url END;
            """, (clean_name, default_link, desc, cover))

            if release_id:
                cur.execute("UPDATE releases SET status = 'REQUESTED', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (release_id,))
            else:
                cur.execute("UPDATE releases SET status = 'REQUESTED', updated_at = CURRENT_TIMESTAMP WHERE lower(series_name) = lower(?) AND status != 'IN_CALIBRE';", (clean_name,))

            conn.commit()
            return True

    # --- Debug / Maintenance Operations ---
    def clear_all_descriptions(self) -> int:
        """Clears all stored descriptions and synopses in releases and series_tracking."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE releases SET description = NULL;")
            rel_count = cur.rowcount
            cur.execute("UPDATE series_tracking SET description = '';")
            conn.commit()
            self.log_activity("DEBUG_CLEAR", "Cleared all novel descriptions", f"Reset descriptions across {rel_count} releases and tracked series.")
            return rel_count

    def clear_discovered_series(self) -> int:
        """
        Clears unreviewed series from the Discovery page.

        Deletes exactly the rows that get_discovery_releases() would show: volume 1 or
        unnumbered unreviewed releases (status='NEW') that are not PDFs, belong to a
        non-blacklisted series and are not part of a monitored tracked series. Pending
        wishlist requests (status='REQUESTED') are never touched.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM releases
                WHERE (volume IS NULL OR volume = 1.0)
                  AND status NOT IN ('IN_CALIBRE', 'BLACKLISTED', 'IGNORED', 'REQUESTED')
                  AND COALESCE(lower(title), '') NOT LIKE '%pdf%'
                  AND COALESCE(lower(link), '') NOT LIKE '%.pdf%'
                  AND COALESCE(lower(guid), '') NOT LIKE '%pdf%'
                  AND (series_name IS NULL OR lower(trim(series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                  AND (series_name IS NULL OR lower(trim(series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking WHERE is_monitored = 1));
            """)
            count = cur.rowcount
            conn.commit()
            self.log_activity("DEBUG_CLEAR", "Cleared discovered series", f"Removed {count} unreviewed discovery releases.")
            return count

    # --- Novel Metadata Cache Operations ---
    def ensure_metadata_cache_table(self):
        """Ensures the novel_metadata_cache table exists with required schema."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS novel_metadata_cache (
                    query_key TEXT PRIMARY KEY,
                    title TEXT,
                    synopsis TEXT,
                    cover_url TEXT,
                    source TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_novel_metadata_cache_key ON novel_metadata_cache(query_key);")
            conn.commit()

    def get_cached_novel_metadata(self, query_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached novel metadata by query key (case-insensitive)."""
        if not query_key:
            return None
        self.ensure_metadata_cache_table()
        clean_k = query_key.strip().lower()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(novel_metadata_cache);")
            cols = {r["name"] for r in cur.fetchall()}
            key_col = "query_key" if "query_key" in cols else "key"
            cur.execute(f"SELECT * FROM novel_metadata_cache WHERE lower({key_col}) = ?;", (clean_k,))
            row = cur.fetchone()
            if row:
                d = dict(row)
                if "query_key" not in d and "key" in d:
                    d["query_key"] = d["key"]
                return d
            return None

    def cache_novel_metadata(self, query_key: str, data: Dict[str, Any]):
        """Caches novel metadata (title, synopsis, cover_url, source)."""
        if not query_key or not data:
            return
        self.ensure_metadata_cache_table()
        clean_k = query_key.strip().lower()
        title = data.get("title") or query_key
        synopsis = data.get("synopsis") or ""
        cover_url = data.get("cover_url") or ""
        source = data.get("source") or "unknown"
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(novel_metadata_cache);")
            cols = {r["name"] for r in cur.fetchall()}
            key_col = "query_key" if "query_key" in cols else "key"
            if "synopsis" in cols and "cover_url" in cols:
                cur.execute(f"""
                    INSERT INTO novel_metadata_cache ({key_col}, title, synopsis, cover_url, source, cached_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT({key_col}) DO UPDATE SET
                        title = excluded.title,
                        synopsis = excluded.synopsis,
                        cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE novel_metadata_cache.cover_url END,
                        source = excluded.source,
                        cached_at = CURRENT_TIMESTAMP;
                """, (clean_k, title, synopsis, cover_url, source))
            else:
                cur.execute(f"INSERT OR REPLACE INTO novel_metadata_cache ({key_col}, data) VALUES (?, ?);", (clean_k, json.dumps(data)))
            conn.commit()

    def get_all_novel_metadata_cache(self) -> Dict[str, Dict[str, Any]]:
        """Returns all cached novel metadata mapped by lowercased query_key."""
        self.ensure_metadata_cache_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(novel_metadata_cache);")
            cols = {r["name"] for r in cur.fetchall()}
            key_col = "query_key" if "query_key" in cols else "key"
            cur.execute("SELECT * FROM novel_metadata_cache;")
            res = {}
            for r in cur.fetchall():
                d = dict(r)
                k = d.get(key_col, "")
                if k:
                    res[k.lower()] = d
            return res

    def clear_novel_metadata_cache(self) -> int:
        """Purges all entries from novel_metadata_cache."""
        self.ensure_metadata_cache_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM novel_metadata_cache;")
            count = cur.rowcount
            conn.commit()
            return count
