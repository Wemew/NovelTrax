import os
import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

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
                    status TEXT DEFAULT 'REQUESTED',
                    calibre_matched_book_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    json.dumps({"description": "Monitors J-Novels for newly posted light novel and web novel EPUB releases."})
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
        with self.get_connection() as conn:
            cur = conn.cursor()
            for s in calibre_series_list:
                name = s["series_name"]
                ser_id = s["series_id"]
                count = s["book_count"]
                latest = s["latest_index"]
                authors = s.get("authors", "")

                cur.execute("""
                    INSERT INTO series_tracking (calibre_series_id, name, authors, book_count, latest_index, is_monitored, last_sync)
                    VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(name) DO UPDATE SET
                        calibre_series_id = excluded.calibre_series_id,
                        authors = excluded.authors,
                        book_count = excluded.book_count,
                        latest_index = excluded.latest_index,
                        last_sync = CURRENT_TIMESTAMP;
                """, (ser_id, name, authors, count, latest))
                updated_count += 1
            conn.commit()
        return updated_count

    def get_all_series(self, monitored_only: bool = False) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            if monitored_only:
                cur.execute("SELECT * FROM series_tracking WHERE is_monitored = 1 ORDER BY name COLLATE NOCASE ASC;")
            else:
                cur.execute("SELECT * FROM series_tracking ORDER BY is_monitored DESC, name COLLATE NOCASE ASC;")
            return [dict(row) for row in cur.fetchall()]

    def toggle_series_monitored(self, series_id: int) -> bool:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE series_tracking SET is_monitored = 1 - is_monitored WHERE id = ?;", (series_id,))
            conn.commit()
            cur.execute("SELECT is_monitored FROM series_tracking WHERE id = ?;", (series_id,))
            row = cur.fetchone()
            return bool(row["is_monitored"]) if row else False

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
                    rel_id
                ))
                conn.commit()
                return False, rel_id
            else:
                cur.execute("""
                    INSERT INTO releases (
                        guid, title, series_name, volume, link, pub_date,
                        cover_url, description, author, source_feed_id,
                        source_feed_name, status, calibre_matched_book_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                    release_data.get("status", "REQUESTED"),
                    release_data.get("calibre_matched_book_id")
                ))
                rel_id = cur.lastrowid
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

            return {
                "monitored_series": monitored_series,
                "pending_requests": pending_requests,
                "acquired_books": acquired_books,
                "active_feeds": active_feeds
            }
