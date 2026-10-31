import os
import sqlite3
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

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

            conn.commit()

        # Run startup cleanup: strip (Light Novel) noise and deduplicate unreviewed series
        self.deduplicate_and_clean_discovery()

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
    def sync_calibre_series(self, calibre_series_list: List[Dict[str, Any]]) -> int:
        """
        Updates series_tracking from Calibre series list.
        Inserts new series, updates book_count and latest_index for existing series.
        Preserves user's is_monitored status!
        """
        updated_count = 0
        from urllib.parse import quote_plus
        with self.get_connection() as conn:
            cur = conn.cursor()
            for s in calibre_series_list:
                name = s["series_name"]
                ser_id = s["series_id"]
                count = s["book_count"]
                latest = s["latest_index"]
                authors = s.get("authors", "")
                default_web_url = f"https://jnovels.com/?s={quote_plus(name)}"

                cur.execute("""
                    INSERT INTO series_tracking (calibre_series_id, name, authors, book_count, latest_index, is_monitored, website_url, last_sync)
                    VALUES (?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(name) DO UPDATE SET
                        calibre_series_id = excluded.calibre_series_id,
                        authors = excluded.authors,
                        book_count = excluded.book_count,
                        latest_index = excluded.latest_index,
                        website_url = CASE WHEN series_tracking.website_url = '' OR series_tracking.website_url IS NULL THEN excluded.website_url ELSE series_tracking.website_url END,
                        last_sync = CURRENT_TIMESTAMP;
                """, (ser_id, name, authors, count, latest, default_web_url))
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
        if release_data.get("title"):
            release_data["title"] = NovelMetadataProvider.clean_novel_title(release_data["title"])

        guid = release_data["guid"]
        with self.get_connection() as conn:
            cur = conn.cursor()

            # Deduplication for discovery (status == 'NEW') releases
            # Only merge if incoming release is a discovery candidate (volume is None or volume == 1.0)
            incoming_vol = release_data.get("volume")
            is_discovery_cand = (incoming_vol is None or incoming_vol == 1.0)
            if release_data.get("status") == "NEW" and release_data.get("series_name") and is_discovery_cand:
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
            vols_val = json.dumps(release_data.get("volumes") or ([release_data["volume"]] if release_data.get("volume") is not None else []))
            missing_val = json.dumps(release_data.get("missing_volumes") or [])

            cur.execute("SELECT id, status FROM releases WHERE guid = ?;", (guid,))
            existing = cur.fetchone()

            if existing:
                rel_id = existing["id"]
                # If already exists and is IN_CALIBRE or IGNORED, keep that status
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
                        is_bundle, volumes_json, missing_vols_json, is_official
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                    release_data.get("status", "REQUESTED"),
                    release_data.get("calibre_matched_book_id"),
                    is_bundle_val,
                    vols_val,
                    missing_val,
                    is_official_val
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
            cur.execute("""
                UPDATE releases 
                SET status = ?, calibre_matched_book_id = COALESCE(?, calibre_matched_book_id), updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (status, matched_book_id, release_id))
            conn.commit()

    def get_releases(self, status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if status:
                cur.execute("""
                    SELECT * FROM releases 
                    WHERE status = ? 
                    ORDER BY updated_at DESC, id DESC 
                    LIMIT ? OFFSET ?;
                """, (status, limit, offset))
            else:
                cur.execute("""
                    SELECT * FROM releases 
                    ORDER BY 
                        CASE status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END,
                        updated_at DESC 
                    LIMIT ? OFFSET ?;
                """, (limit, offset))
            items = [dict(row) for row in cur.fetchall()]
            for item in items:
                try:
                    item["volumes"] = json.loads(item.get("volumes_json") or "[]")
                except Exception:
                    item["volumes"] = [item["volume"]] if item.get("volume") is not None else []
                try:
                    item["missing_volumes"] = json.loads(item.get("missing_vols_json") or "[]")
                except Exception:
                    item["missing_volumes"] = []
            return items

    def get_releases_grouped_by_series(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Groups releases under canonical series, aggregating all bundle volumes,
        highlighting owned vs missing volumes against Calibre tracking.
        Respects track_official_only setting by filtering out unofficial fan volumes.
        """
        from app.parser import normalize_text, get_canonical_series_name, should_merge_series

        track_official_only = self.get_setting("track_official_only", "true").lower() == "true"

        with self.get_connection() as conn:
            cur = conn.cursor()

            if status:
                cur.execute("""
                    SELECT * FROM releases 
                    WHERE status = ? 
                    ORDER BY updated_at DESC, id DESC;
                """, (status,))
            else:
                cur.execute("""
                    SELECT * FROM releases 
                    ORDER BY 
                        CASE status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END,
                        updated_at DESC, id DESC;
                """)

            raw_releases = [dict(r) for r in cur.fetchall()]
            for r in raw_releases:
                try:
                    r["volumes"] = json.loads(r.get("volumes_json") or "[]")
                except Exception:
                    r["volumes"] = [r["volume"]] if r.get("volume") is not None else []
                try:
                    r["missing_volumes"] = json.loads(r.get("missing_vols_json") or "[]")
                except Exception:
                    r["missing_volumes"] = []

            cur.execute("SELECT * FROM series_tracking;")
            tracked_map = {row["name"].lower(): dict(row) for row in cur.fetchall()}

            groups_dict: Dict[str, Dict[str, Any]] = {}

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

                # Check if group already exists under s_key, alias, or shared series_link
                matched_key = None
                if s_key in groups_dict:
                    matched_key = s_key
                else:
                    rel_link = (rel.get("series_link") or "").strip().rstrip("/")
                    for existing_k, existing_g in groups_dict.items():
                        if should_merge_series(canonical_s_name, existing_g["series_name"]):
                            matched_key = existing_k
                            break
                        g_link = (existing_g.get("website_url") or "").strip().rstrip("/")
                        if rel_link and g_link and rel_link == g_link:
                            matched_key = existing_k
                            break

                if not matched_key:
                    matched_key = s_key
                    calibre_match = None
                    for t_name_lower, t_row in tracked_map.items():
                        if normalize_text(t_name_lower) == s_key or should_merge_series(canonical_s_name, t_row["name"]):
                            calibre_match = t_row
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
                        "has_in_calibre": False
                    }

                group = groups_dict[matched_key]
                group["releases"].append(rel)
                group["releases_count"] += 1

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
                g["owned_volumes"] = [v for v in all_vols if v <= latest_owned and v not in missing_vols]
                del g["all_volumes_set"]
                del g["missing_volumes_set"]
                result_groups.append(g)

            # Sort groups: series with wanted/requested releases first, then discovery, then tracked, then name
            def sort_key(item):
                prio = 4
                if item["has_wanted"]:
                    prio = 1
                elif item["has_discovery"]:
                    prio = 2
                elif item["in_calibre"]:
                    prio = 3
                return (prio, item["series_name"].lower())

            result_groups.sort(key=sort_key)
            return result_groups

    def count_releases(self, status: Optional[str] = None) -> int:
        """Returns total count of releases, optionally filtered by status."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            if status:
                cur.execute("SELECT count(*) FROM releases WHERE status = ?;", (status,))
            else:
                cur.execute("SELECT count(*) FROM releases;")
            row = cur.fetchone()
            return row[0] if row else 0

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

    def remove_from_blacklist(self, blacklist_id: int):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM series_blacklist WHERE id = ?;", (blacklist_id,))
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
    def get_discovery_releases(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        Returns releases that are Volume 1 or unnumbered (standalone),
        which are NOT currently in Calibre, NOT in series_tracking, and NOT blacklisted.
        Deduplicates by series name so each series appears at most once.
        Strictly excludes PDF releases.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
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
                      AND r.status NOT IN ('IN_CALIBRE', 'BLACKLISTED', 'IGNORED')
                      AND COALESCE(lower(r.title), '') NOT LIKE '%pdf%'
                      AND COALESCE(lower(r.link), '') NOT LIKE '%.pdf%'
                      AND COALESCE(lower(r.guid), '') NOT LIKE '%pdf%'
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking WHERE is_monitored = 1))
                )
                SELECT * FROM ranked_discovery
                WHERE rn = 1
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?;
            """, (limit, offset))
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

    def count_discovery_releases(self) -> int:
        """
        Returns total count of unique discovered series (Volume 1 or unnumbered standalones)
        not in Calibre, tracking, or blacklist.
        Strictly excludes PDF releases.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
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
                      AND r.status NOT IN ('IN_CALIBRE', 'BLACKLISTED', 'IGNORED')
                      AND COALESCE(lower(r.title), '') NOT LIKE '%pdf%'
                      AND COALESCE(lower(r.link), '') NOT LIKE '%.pdf%'
                      AND COALESCE(lower(r.guid), '') NOT LIKE '%pdf%'
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking WHERE is_monitored = 1))
                )
                SELECT count(*) FROM ranked_discovery WHERE rn = 1;
            """)
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

        from app.parser import extract_series_and_volume, should_merge_series, get_canonical_series_name, SERIES_ALIASES
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

            # 6. Alias, series_link & fuzzy deduplication for 'NEW' status discovery releases
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
            merged_ids = set()

            for i in range(len(disc_records)):
                rec_a = disc_records[i]
                if rec_a["id"] in merged_ids:
                    continue

                for j in range(i + 1, len(disc_records)):
                    rec_b = disc_records[j]
                    if rec_b["id"] in merged_ids:
                        continue

                    links_match = False
                    if rec_a.get("series_link") and rec_b.get("series_link"):
                        la = rec_a["series_link"].rstrip("/")
                        lb = rec_b["series_link"].rstrip("/")
                        if la == lb:
                            links_match = True

                    if links_match or should_merge_series(rec_a["series_name"], rec_b["series_name"]):
                        m_desc = rec_a["description"] or rec_b["description"]
                        m_cover = rec_a["cover_url"] or rec_b["cover_url"]
                        m_link = rec_a["series_link"] or rec_b["series_link"]

                        cur.execute("DELETE FROM releases WHERE id = ?;", (rec_b["id"],))
                        merged_ids.add(rec_b["id"])

                        cur.execute("""
                            UPDATE releases 
                            SET description = ?, cover_url = ?, series_link = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?;
                        """, (m_desc, m_cover, m_link, rec_a["id"]))

                        rec_a["description"] = m_desc
                        rec_a["cover_url"] = m_cover
                        rec_a["series_link"] = m_link

            conn.commit()

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
        """Clears unreviewed new series from the Discovery page (status='NEW')."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM releases WHERE status = 'NEW';")
            count = cur.rowcount
            conn.commit()
            self.log_activity("DEBUG_CLEAR", "Cleared discovered series", f"Removed {count} unreviewed discovery releases.")
            return count
