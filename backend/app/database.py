import os
import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

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
    def upsert_release(self, release_data: Dict[str, Any]) -> Tuple[bool, int]:
        """
        Inserts or updates a release.
        Returns (is_new, release_id).
        """
        guid = release_data["guid"]
        with self.get_connection() as conn:
            cur = conn.cursor()
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
                    rel_id
                ))
                conn.commit()
                return False, rel_id
            else:
                cur.execute("""
                    INSERT INTO releases (
                        guid, title, series_name, volume, link, pub_date,
                        cover_url, description, author, source_feed_id,
                        source_feed_name, series_link, status, calibre_matched_book_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                    release_data.get("calibre_matched_book_id")
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
            return [dict(row) for row in cur.fetchall()]

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

            cur.execute("""
                SELECT count(*) 
                FROM releases r
                WHERE (r.volume IS NULL OR r.volume = 1.0)
                  AND r.status NOT IN ('IN_CALIBRE', 'BLACKLISTED')
                  AND (r.series_name IS NULL OR lower(r.series_name) NOT IN (SELECT lower(name) FROM series_blacklist))
                  AND (r.series_name IS NULL OR lower(r.series_name) NOT IN (SELECT lower(name) FROM series_tracking WHERE is_monitored = 1));
            """)
            discovered_count = cur.fetchone()[0]

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
    def get_discovery_releases(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Returns releases that are Volume 1 or unnumbered (standalone),
        which are NOT currently in Calibre, NOT in series_tracking, and NOT blacklisted.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT r.* 
                FROM releases r
                WHERE (r.volume IS NULL OR r.volume = 1.0)
                  AND r.status NOT IN ('IN_CALIBRE', 'BLACKLISTED')
                  AND (r.series_name IS NULL OR lower(r.series_name) NOT IN (SELECT lower(name) FROM series_blacklist))
                  AND (r.series_name IS NULL OR lower(r.series_name) NOT IN (SELECT lower(name) FROM series_tracking WHERE is_monitored = 1))
                ORDER BY r.updated_at DESC, r.id DESC
                LIMIT ?;
            """, (limit,))
            return [dict(row) for row in cur.fetchall()]

    def update_release_details(self, release_id: int, description: Optional[str] = None, series_link: Optional[str] = None, cover_url: Optional[str] = None):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE releases 
                SET description = COALESCE(?, description),
                    series_link = COALESCE(?, series_link),
                    cover_url = COALESCE(?, cover_url),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (description, series_link, cover_url, release_id))
            conn.commit()

    def track_discovered_series(self, series_name: str, release_id: Optional[int] = None) -> bool:
        """
        Adds a discovered series to series_tracking (monitored = 1) and
        updates associated release status to REQUESTED.
        Preserves canonical series hub link if present.
        """
        clean_name = series_name.strip()
        if not clean_name:
            return False
        from urllib.parse import quote_plus
        with self.get_connection() as conn:
            cur = conn.cursor()
            default_link = f"https://jnovels.com/?s={quote_plus(clean_name)}"

            # If release has a canonical series_link, use it
            if release_id:
                cur.execute("SELECT series_link FROM releases WHERE id = ?;", (release_id,))
                row = cur.fetchone()
                if row and row["series_link"]:
                    default_link = row["series_link"]

            cur.execute("""
                INSERT INTO series_tracking (name, book_count, latest_index, is_monitored, website_url, last_sync)
                VALUES (?, 0, 0.0, 1, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET 
                    is_monitored = 1,
                    website_url = CASE WHEN series_tracking.website_url = '' OR series_tracking.website_url IS NULL OR series_tracking.website_url LIKE '%?s=%' THEN excluded.website_url ELSE series_tracking.website_url END;
            """, (clean_name, default_link))

            if release_id:
                cur.execute("UPDATE releases SET status = 'REQUESTED', updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (release_id,))
            else:
                cur.execute("UPDATE releases SET status = 'REQUESTED', updated_at = CURRENT_TIMESTAMP WHERE lower(series_name) = lower(?) AND status != 'IN_CALIBRE';", (clean_name,))

            conn.commit()
            return True
