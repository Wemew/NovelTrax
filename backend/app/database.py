import os
import re
import math
import sqlite3
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, Set, Union, Sequence

from app.status_precedence import resolve_status
from app.download_state import release_download_state

logger = logging.getLogger("noveltrax.database")

# §12.11.2: a release has a *set* of release groups, so whole-string equality
# against prioritized_groups misses joint releases. The JSON set is authoritative
# and the legacy single `release_group` column is honoured for rows that predate
# the one-shot split (un-backfilled rows still carry an empty JSON array).
_PRIORITIZED_GROUP_MATCH_SQL = """(
    EXISTS (
        SELECT 1
        FROM json_each(
            CASE WHEN json_valid(r.release_groups_json)
                 THEN r.release_groups_json ELSE '[]' END
        ) AS jg
        JOIN prioritized_groups pg
          ON lower(trim(CAST(jg.value AS TEXT))) = lower(trim(pg.name))
    )
    OR (
        CASE WHEN json_valid(r.release_groups_json)
             THEN json_array_length(r.release_groups_json) ELSE 0 END = 0
        AND lower(trim(COALESCE(r.release_group, '')))
            IN (SELECT lower(trim(name)) FROM prioritized_groups)
    )
)"""

# Split a legacy/compound group tag into the set of groups it names (§12.11.1).
_GROUP_DELIMITER_RE = re.compile(r"\s*(?:,|&|/|\+|\band\b)\s*", re.IGNORECASE)


def _split_group_string(value: Optional[str]) -> List[str]:
    """Splits one legacy/compound group value on ``,``, ``&``, ``/``, ``+`` and ``and``."""
    if not value:
        return []
    text = str(value)
    # Known group/publisher names may themselves contain a delimiter
    # ("Square Enix Manga & Books"); never split a recognised single name.
    try:
        from app.parser import DEFAULT_KNOWN_GROUPS, KNOWN_PUBLISHERS
        known_names = {name.strip().lower() for name in DEFAULT_KNOWN_GROUPS}
        known_names |= {name.strip().lower() for name in KNOWN_PUBLISHERS}
        if text.strip().lower() in known_names:
            return [text.strip()]
    except Exception:
        pass
    groups: List[str] = []
    seen: Set[str] = set()
    for part in _GROUP_DELIMITER_RE.split(text):
        clean = part.strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        groups.append(clean)
    return groups


def _coerce_group_list(raw: Any) -> List[str]:
    """Normalises an explicit groups payload into a de-duplicated list of strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            raw = parsed if isinstance(parsed, list) else [text]
        else:
            raw = [text]
    elif not isinstance(raw, (list, tuple, set)):
        raw = [raw]

    groups: List[str] = []
    seen: Set[str] = set()
    for item in raw:
        if item is None:
            continue
        clean = str(item).strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        groups.append(clean)
    return groups

def _coerce_owned_volumes(raw: Any) -> List[float]:
    """Normalises an owned-volume payload (JSON string/list) into sorted floats."""
    if raw is None:
        return []
    values = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = re.findall(r"\d+(?:\.\d+)?", text)
        values = parsed
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, (list, tuple, set)):
        return []
    volumes: Set[float] = set()
    for value in values:
        try:
            volumes.add(float(value))
        except (TypeError, ValueError):
            continue
    return sorted(volumes)


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int conversion; ``None``/blank -> ``None``."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float conversion; unusable values -> *default*."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_text(value: Any) -> str:
    """Best-effort stripped text; ``None`` -> ``''``."""
    if value is None:
        return ""
    return str(value).strip()


def _utcnow_iso() -> str:
    """UTC ISO-8601 timestamp used for diagnostic pin/refresh stamps."""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


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

            # Series identity / AniList title sync (owner-A contract §2.1).
            # ``name`` stays the STABLE MATCHING KEY; ``display_title`` is the
            # cosmetic AniList title and is never fed back into matching.
            for identity_col in [
                ("display_title", "TEXT DEFAULT ''"),
                ("anilist_id", "INTEGER"),
                ("anilist_meta_json", "TEXT DEFAULT ''"),
                ("match_source", "TEXT DEFAULT ''"),
                # Contract v2 §2.2: a manually-created ("different series") tracker
                # is locked so automatic linking/enrichment/merges never rewrite it.
                ("identity_locked", "INTEGER DEFAULT 0"),
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE series_tracking ADD COLUMN {identity_col[0]} {identity_col[1]};"
                    )
                except sqlite3.OperationalError:
                    pass

            # Releases table downloader and prioritized group migrations
            for col_def in [
                ("download_url", "TEXT"),
                ("download_status", "TEXT DEFAULT 'IDLE'"),
                ("torrent_hash", "TEXT"),
                ("release_group", "TEXT DEFAULT ''"),
                ("ignored", "INTEGER DEFAULT 0"),
                ("media_type", "TEXT DEFAULT 'unknown'"),
                # Consolidated detection columns (release groups as a set,
                # publisher vs group, revision marker, chapter data).
                ("release_groups_json", "TEXT DEFAULT '[]'"),
                ("publisher", "TEXT DEFAULT ''"),
                ("revision", "INTEGER DEFAULT 0"),
                ("chapter_number", "REAL"),
                ("chapter_ranges_json", "TEXT DEFAULT '[]'"),
                # Download-engine bookkeeping (TorBox port §3.1).  The status
                # vocabulary stays IDLE/DOWNLOADING/COMPLETED/FAILED.
                ("downloader", "TEXT DEFAULT ''"),
                ("downloader_id", "TEXT DEFAULT ''"),
                ("downloader_started_at", "TEXT DEFAULT ''"),
                ("download_wanted_json", "TEXT DEFAULT '[]'"),
                ("download_error", "TEXT DEFAULT ''"),
                ("download_attempts", "INTEGER DEFAULT 0"),
                ("torbox_progress_json", "TEXT DEFAULT '{}'"),
                ("download_files_json", "TEXT DEFAULT '[]'"),
                ("downloaded_at", "TEXT DEFAULT ''"),
                # Contract v2 §2.1: explicit user re-bind pin. A pinned release keeps
                # its series_name verbatim until reset/unpinned.
                ("series_pinned", "INTEGER DEFAULT 0"),
                ("series_pinned_at", "TEXT DEFAULT ''"),
            ]:
                try:
                    cur.execute(f"ALTER TABLE releases ADD COLUMN {col_def[0]} {col_def[1]};")
                except sqlite3.OperationalError:
                    pass

            # Per-release cache of the media-type decision (never re-fetched once known)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS release_media_cache (
                    guid TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Performance indexes on releases table
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_release_group ON releases(release_group);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_ignored ON releases(ignored);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_media_type ON releases(media_type);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_releases_download_status ON releases(download_status);")
            # Contract v5 §5: expression index backing the normalised series-name
            # lookups used by the pickers/aggregates (guarded for very old SQLite).
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_releases_series_name_norm "
                    "ON releases(lower(trim(series_name)));"
                )
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

            # Persistent alias overrides: a parsed/display source spelling that
            # resolves to a tracked (stable) series name (owner-A contract §2.2).
            # Manual rows always win over rows learned from AniList/candidates.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS series_alias_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    source_norm TEXT NOT NULL UNIQUE,
                    target_name TEXT NOT NULL,
                    target_norm TEXT NOT NULL,
                    calibre_series_id INTEGER,
                    origin TEXT DEFAULT 'manual',
                    actor TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_series_alias_overrides_target "
                "ON series_alias_overrides(target_norm);"
            )

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

            # TorBox engine + local fetch target defaults (TorBox port §2).  All
            # values are strings; ``torbox_last_poll_at`` is written at runtime and
            # is deliberately not seeded here.
            torbox_defaults = [
                ("downloader_engine", "qbit"),
                ("torbox_enabled", os.getenv("TORBOX_ENABLED", "false")),
                ("torbox_api_key", os.getenv("TORBOX_API_KEY", "")),
                ("torbox_delete_after_download", "true"),
                ("torbox_strict_volume_selection", "true"),
                ("torbox_delete_on_failure", "true"),
                ("torbox_download_concurrency", "4"),
                ("torbox_parallel_chunks", "4"),
                ("torbox_max_download_gb", "64"),
                ("torbox_poll_seconds", "5"),
                ("torbox_max_concurrent", "2"),
                ("torbox_timeout_hours", "6"),
                ("downloads_dir", os.getenv("DOWNLOAD_DIR", "/downloads")),
            ]
            for k, v in torbox_defaults:
                cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?);", (k, v))

            # Contract v2 §6.1: generic display/logging/maintenance settings from
            # the MangaTrax settings inventory.  UI validation lives in main.py.
            maintenance_defaults = [
                ("app_timezone", ""),
                ("log_level", "INFO"),
                ("activity_retention_days", "90"),
                ("activity_max_rows", "50000"),
            ]
            for k, v in maintenance_defaults:
                cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?);", (k, v))

            # Contract v2 §6.5: webhook event toggles (scheduler guards read these).
            webhook_defaults = [
                ("webhook_new_request_notifications", "true"),
                ("webhook_acquired_notifications", "true"),
            ]
            for k, v in webhook_defaults:
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

        self._run_groups_backfill_once()
        self._run_chapter_purge_once()
        self._run_media_reclassify_once()
        self._run_series_key_repair_once()

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
            cur.execute("SELECT id, title FROM releases WHERE (status IN ('NEW', 'REQUESTED') OR status IS NULL) AND COALESCE(series_pinned, 0) = 0;")
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

    # --- Media type (novel vs manga vs audiobook) ---
    def get_media_type_cache(self, guid: str) -> Optional[str]:
        """Returns the cached media type for a release GUID, or None if unknown."""
        if not guid:
            return None
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT media_type FROM release_media_cache WHERE guid = ?;", (guid,))
                row = cur.fetchone()
                return row["media_type"] if row else None
        except sqlite3.OperationalError:
            return None

    def set_media_type_cache(self, guid: str, media_type: str, detail: str = ""):
        """Caches a media-type decision so it is never re-fetched."""
        if not guid or not media_type:
            return
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO release_media_cache (guid, media_type, detail, checked_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(guid) DO UPDATE SET
                        media_type = excluded.media_type,
                        detail = excluded.detail,
                        checked_at = CURRENT_TIMESTAMP;
                """, (guid, media_type, detail or ""))
                conn.commit()
        except sqlite3.OperationalError:
            pass

    def update_release_media_type(self, release_id: int, media_type: str):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE releases SET media_type = ? WHERE id = ?;", (media_type, release_id))
            conn.commit()

    def get_audiobook_releases(self, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        """Returns audiobook releases, which live outside the normal tracking views."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM releases
                WHERE media_type = 'audiobook'
                ORDER BY COALESCE(pub_date, updated_at) DESC, id DESC
                LIMIT ? OFFSET ?;
            """, (limit, offset))
            items = [dict(row) for row in cur.fetchall()]
            for item in items:
                try:
                    item["volumes"] = json.loads(item.get("volumes_json") or "[]")
                except Exception:
                    item["volumes"] = [item["volume"]] if item.get("volume") is not None else []
                self._attach_detection_fields(item)
            return items

    def count_audiobook_releases(self) -> int:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM releases WHERE media_type = 'audiobook';")
            return cur.fetchone()[0]

    def _run_media_reclassify_once(self):
        """
        One-shot pass that re-types releases stored before media typing existed.

        NON-DESTRUCTIVE: rows are only re-labelled, never deleted.

        * audiobook-tagged rows become ``media_type='audiobook'`` (hidden from the
          tracking views, shown in the Audiobooks section)
        * manga-tagged rows become ``media_type='manga'`` (hidden from the tracking
          views; a later maintenance purge can remove them explicitly)

        Only rows still typed ``unknown`` are touched, so any judgement already
        made (including in-Calibre matches) is preserved. The pass honours the
        ``filter_non_novels`` setting and runs once -- any non-empty marker value
        other than ``"0"`` counts as already applied.
        """
        try:
            marker = (self.get_setting("media_filter_applied", "") or "").strip()
            if marker not in ("", "0"):
                return
            if (self.get_setting("filter_non_novels", "true") or "").strip().lower() == "false":
                return

            from app.media_type import classify_title, AUDIOBOOK, MANGA

            typed_audio = 0
            typed_manga = 0
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, title, status, media_type FROM releases;")
                rows = cur.fetchall()
                for row in rows:
                    current = (row["media_type"] or "unknown").strip().lower()
                    if current != "unknown":
                        continue
                    title = (row["title"] or "").strip()
                    if not title:
                        continue
                    kind = classify_title(title)
                    if kind == AUDIOBOOK:
                        cur.execute("UPDATE releases SET media_type = 'audiobook' WHERE id = ?;", (row["id"],))
                        typed_audio += 1
                    elif kind == MANGA:
                        cur.execute("UPDATE releases SET media_type = 'manga' WHERE id = ?;", (row["id"],))
                        typed_manga += 1
                conn.commit()

            self.set_setting("media_filter_applied", "1")
            if typed_audio or typed_manga:
                self.log_activity(
                    "MEDIA_RECLASSIFY",
                    "Reclassified stored releases by media type",
                    f"Tagged {typed_audio} audiobook(s) and {typed_manga} manga release(s); no rows were deleted."
                )
                logger.info(f"Media reclassify: {typed_audio} audiobook(s), {typed_manga} manga tagged (nothing deleted).")
        except Exception as exc:
            logger.warning(f"Media reclassify skipped: {exc}")

    # --- Consolidated detection fields (groups, publisher, revision, chapters) ---
    @staticmethod
    def _attach_detection_fields(item: Dict[str, Any]) -> Dict[str, Any]:
        """Adds the parsed detection fields to a release row read from SQLite."""
        groups = _coerce_group_list(item.get("release_groups_json"))
        if not groups:
            # Legacy row that predates the set backfill: the single column is
            # still the authoritative value for display/matching fallback.
            legacy = (item.get("release_group") or "").strip()
            if legacy:
                groups = [legacy]
        item["release_groups"] = groups

        try:
            ranges = json.loads(item.get("chapter_ranges_json") or "[]")
        except (TypeError, ValueError):
            ranges = []
        item["chapter_ranges"] = ranges if isinstance(ranges, list) else []

        try:
            item["revision"] = int(item.get("revision") or 0)
        except (TypeError, ValueError):
            item["revision"] = 0
        if item.get("publisher") is None:
            item["publisher"] = ""
        return item

    @staticmethod
    def _stable_series_name(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
        """Returns the stored stable series key for an existing release (§12.1).

        ``releases.series_name`` is the Calibre/tracked matching key. A feed
        re-poll or metadata provider may enrich a row, but it must never re-key
        the series: once a non-empty name is stored it is preserved verbatim.
        """
        existing_clean = (existing or "").strip()
        incoming_clean = (incoming or "").strip()
        if existing_clean:
            return existing_clean
        return incoming_clean or None

    def backfill_release_groups(self) -> int:
        """Splits legacy ``release_group`` values into ``release_groups_json``.

        Idempotent: rows that already carry a non-empty JSON set are untouched.
        Returns the number of rows repaired.
        """
        repaired = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, release_group FROM releases
                WHERE release_group IS NOT NULL AND trim(release_group) != ''
                  AND (release_groups_json IS NULL OR trim(release_groups_json) IN ('', '[]'));
            """)
            for row in cur.fetchall():
                groups = _split_group_string(row["release_group"])
                if not groups:
                    continue
                # The first split part becomes the display/back-compat primary.
                cur.execute(
                    "UPDATE releases SET release_groups_json = ?, release_group = ? WHERE id = ?;",
                    (json.dumps(groups), groups[0], row["id"]),
                )
                repaired += 1
            conn.commit()
        return repaired

    def _run_groups_backfill_once(self):
        """One-shot split of legacy group strings, guarded by a setting."""
        try:
            marker = (self.get_setting("groups_backfill_applied", "") or "").strip()
            if marker not in ("", "0"):
                return
            repaired = self.backfill_release_groups()
            self.set_setting("groups_backfill_applied", "1")
            if repaired:
                logger.info(f"Release groups backfill: split {repaired} legacy group tag(s) into sets.")
        except Exception as exc:
            logger.warning(f"Release groups backfill skipped: {exc}")

    def repair_rekeyed_series_names(self) -> int:
        """Re-keys releases stored under a provider/canonical title (§12.1).

        The stable matching key for a series is the name tracked in
        ``series_tracking`` (normally the Calibre series name or the user-typed
        name). Releases ingested under a canonical/provider alias are re-keyed to
        that stable name. The pass is:

        * **idempotent** -- a re-keyed row normalises to the tracked name and is
          skipped on the next run;
        * **non-destructive** -- only ``series_name`` is updated, no row is
          deleted or merged;
        * **ambiguous-safe** -- when one canonical alias is claimed by more than
          one tracked series the row is left untouched and a warning is logged.

        Returns the number of rows re-keyed.
        """
        try:
            from app.parser import normalize_text, get_canonical_series_name
        except Exception:
            return 0

        rekeyed = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM series_tracking WHERE name IS NOT NULL AND trim(name) != '';")
            tracked_names = [row["name"].strip() for row in cur.fetchall()]
            if not tracked_names:
                return 0

            tracked_norms = {normalize_text(name) for name in tracked_names}
            # canonical key -> tracked stable names claiming it
            claims: Dict[str, Set[str]] = {}
            for name in tracked_names:
                try:
                    canonical = get_canonical_series_name(name) or name
                except Exception:
                    canonical = name
                claims.setdefault(normalize_text(canonical), set()).add(name)

            cur.execute("SELECT id, series_name FROM releases WHERE series_name IS NOT NULL AND trim(series_name) != '' AND COALESCE(series_pinned, 0) = 0;")
            for row in cur.fetchall():
                current = row["series_name"].strip()
                current_norm = normalize_text(current)
                if not current_norm or current_norm in tracked_norms:
                    continue
                try:
                    canonical = get_canonical_series_name(current) or current
                except Exception:
                    continue
                targets = claims.get(normalize_text(canonical), set())
                if len(targets) > 1:
                    logger.warning(
                        "Stable-key repair skipped ambiguous alias %r (claimed by %s)",
                        current, sorted(targets),
                    )
                    continue
                if len(targets) != 1:
                    continue
                target = next(iter(targets))
                if normalize_text(target) == current_norm:
                    continue
                cur.execute("UPDATE releases SET series_name = ? WHERE id = ?;", (target, row["id"]))
                rekeyed += 1
            conn.commit()
        if rekeyed:
            logger.info(f"Stable-key repair: re-keyed {rekeyed} release(s) to their tracked series name.")
        return rekeyed

    def _run_series_key_repair_once(self):
        """One-shot stable-key repair, guarded by a setting (safe to run twice)."""
        try:
            marker = (self.get_setting("stable_key_repair_applied", "") or "").strip()
            if marker not in ("", "0"):
                return
            repaired = self.repair_rekeyed_series_names()
            self.set_setting("stable_key_repair_applied", "1")
            if repaired:
                logger.info(f"Stable-key repair pass re-keyed {repaired} release(s).")
        except Exception as exc:
            logger.warning(f"Stable-key repair skipped: {exc}")

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
        """All tracked series with their latest non-empty release cover.

        Performance contract v5 §5: a single statement replaces the historical
        correlated subquery (one full ``releases`` scan per series row).  The
        ``cover_latest`` CTE aggregates ``MAX(id)`` per normalised series name
        once, then joins the cover row; Python only merges the ``cover_url``
        value.  Ordering, ``monitored_only`` semantics and output keys are
        unchanged.  A release cover wins; when no release has a cover the
        tracker's own (enrichment) cover is kept, and neither -> ``None``.
        """
        order_sql = (
            "ORDER BY st.name COLLATE NOCASE ASC"
            if monitored_only
            else "ORDER BY st.is_monitored DESC, st.name COLLATE NOCASE ASC"
        )
        where_sql = "WHERE st.is_monitored = 1" if monitored_only else ""
        query = f"""
            WITH cover_latest AS (
                SELECT lower(trim(series_name)) AS norm, MAX(id) AS latest_id
                FROM releases
                WHERE cover_url IS NOT NULL AND cover_url != ''
                GROUP BY lower(trim(series_name))
            )
            SELECT st.*, releases.cover_url AS release_cover_url
            FROM series_tracking st
            LEFT JOIN cover_latest ON cover_latest.norm = lower(trim(st.name))
            LEFT JOIN releases ON releases.id = cover_latest.latest_id
            {where_sql}
            {order_sql};
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query)
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            release_cover = _coerce_text(row.pop("release_cover_url", ""))
            tracker_cover = _coerce_text(row.get("cover_url"))
            row["cover_url"] = release_cover or tracker_cover or None
        return rows

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

    # --- Series identity: stable key, display title, AniList metadata, aliases ---
    #
    # Naming model (ported from MangaTrax, authoritative):
    #   * ``series_tracking.name`` / ``releases.series_name`` is the STABLE
    #     MATCHING KEY (the Calibre series name when linked).  Nothing may
    #     rewrite it from external metadata.
    #   * ``display_title`` is the cosmetic AniList title, used for rendering only.
    def get_series_identity_rows(self) -> List[Dict[str, Any]]:
        """All ``series_tracking`` rows, including the identity columns."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_tracking ORDER BY name COLLATE NOCASE ASC;")
            return [dict(row) for row in cur.fetchall()]

    def get_series_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Tracked series row by case/whitespace-insensitive stable name."""
        clean = _coerce_text(name)
        if not clean:
            return None
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?)) LIMIT 1;",
                (clean,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_series_by_calibre_id(self, calibre_series_id: int) -> Optional[Dict[str, Any]]:
        """Tracked series row linked to a Calibre series id (or ``None``)."""
        cid = _coerce_int(calibre_series_id)
        if cid is None:
            return None
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM series_tracking WHERE calibre_series_id = ? ORDER BY id LIMIT 1;",
                (cid,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_series_identity(self, series_id: int, display_title: Optional[str] = None,
                               anilist_id: Optional[int] = None,
                               anilist_meta_json: Optional[str] = None,
                               match_source: Optional[str] = None,
                               calibre_series_id: Optional[int] = None) -> None:
        """Update identity columns only; ``None`` leaves a column unchanged.

        The stable ``name`` column is never touched by this method.
        """
        sid = _coerce_int(series_id)
        if sid is None:
            return
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE series_tracking SET
                    display_title = COALESCE(?, display_title),
                    anilist_id = COALESCE(?, anilist_id),
                    anilist_meta_json = COALESCE(?, anilist_meta_json),
                    match_source = COALESCE(?, match_source),
                    calibre_series_id = COALESCE(?, calibre_series_id),
                    last_sync = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (
                None if display_title is None else _coerce_text(display_title),
                _coerce_int(anilist_id),
                None if anilist_meta_json is None else str(anilist_meta_json),
                None if match_source is None else _coerce_text(match_source),
                _coerce_int(calibre_series_id),
                sid,
            ))
            conn.commit()

    def get_series_alias_overrides(self) -> List[Dict[str, Any]]:
        """All persistent alias overrides, newest first."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_alias_overrides ORDER BY created_at DESC, id DESC;")
            return [dict(row) for row in cur.fetchall()]

    def add_series_alias_override(self, source_name: str, target_name: str,
                                  calibre_series_id: Optional[int] = None,
                                  origin: str = "manual", actor: str = "") -> Dict[str, Any]:
        """Upsert an alias override (source spelling -> tracked stable name).

        Manual rows always win over rows learned from AniList/candidates; an
        explicit ``origin='manual'`` call overwrites any previous row.  Deleting
        an override removes the mapping only - it never undoes a re-key.
        """
        from app.parser import normalize_text

        source = _coerce_text(source_name)
        target = _coerce_text(target_name)
        source_norm = normalize_text(source)
        if not source_norm or not target:
            raise ValueError("source_name and target_name are required")
        target_norm = normalize_text(target)
        origin = _coerce_text(origin) or "manual"
        actor = _coerce_text(actor)
        cid = _coerce_int(calibre_series_id)
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_alias_overrides WHERE source_norm = ?;", (source_norm,))
            existing = cur.fetchone()
            if (
                existing is not None
                and (_coerce_text(existing["origin"]) or "manual") == "manual"
                and origin != "manual"
            ):
                # Manual rows always win over learned rows for the same source.
                return dict(existing)
            if existing is None:
                cur.execute("""
                    INSERT INTO series_alias_overrides
                        (source_name, source_norm, target_name, target_norm,
                         calibre_series_id, origin, actor, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                """, (source, source_norm, target, target_norm, cid, origin, actor))
            else:
                cur.execute("""
                    UPDATE series_alias_overrides SET
                        source_name = ?, target_name = ?, target_norm = ?,
                        calibre_series_id = COALESCE(?, calibre_series_id),
                        origin = ?, actor = ?, created_at = CURRENT_TIMESTAMP
                    WHERE source_norm = ?;
                """, (source, target, target_norm, cid, origin, actor, source_norm))
            conn.commit()
            cur.execute("SELECT * FROM series_alias_overrides WHERE source_norm = ?;", (source_norm,))
            row = cur.fetchone()
            return dict(row) if row else {}

    def delete_series_alias_override(self, override_id: int) -> bool:
        """Delete one alias override.

        This only stops future resolutions from using the mapping: releases that
        were already re-keyed to the stable name stay re-keyed (deleting an
        override never undoes a re-key).
        """
        oid = _coerce_int(override_id)
        if oid is None:
            return False
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM series_alias_overrides WHERE id = ?;", (oid,))
            conn.commit()
            return bool(cur.rowcount)

    def _get_series_identity_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Tracked series a spelling belongs to (stable name, display, alias)."""
        clean = _coerce_text(title)
        if not clean:
            return None
        from app.parser import normalize_text

        row = self.get_series_by_name(clean)
        if row is not None:
            return row
        norm = normalize_text(clean)
        if not norm:
            return None
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM series_tracking "
                "WHERE lower(trim(COALESCE(display_title, ''))) = lower(trim(?)) LIMIT 1;",
                (clean,),
            )
            row = cur.fetchone()
            if row is not None:
                return dict(row)
            cur.execute("SELECT * FROM series_alias_overrides WHERE source_norm = ?;", (norm,))
            override = cur.fetchone()
            if override is not None:
                target = _coerce_text(override["target_name"])
                cur.execute(
                    "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?)) LIMIT 1;",
                    (target,),
                )
                target_row = cur.fetchone()
                if target_row is not None:
                    return dict(target_row)
                return {
                    "id": None,
                    "name": target,
                    "calibre_series_id": override["calibre_series_id"],
                }
        return None

    def _resolve_updated_series_name(self, existing_name: Optional[str],
                                     incoming_name: Optional[str]) -> str:
        """Decide ``releases.series_name`` for an explicit update (M1 fix).

        ``releases.series_name`` is a STABLE KEY: a metadata/AniList display
        title must never replace it.  Rules, in order:

        1. blank incoming keeps the historical write-as-supplied behaviour;
        2. same normalised spelling or same ``SERIES_ALIASES`` canonical group
           keeps the stored spelling;
        3. both names resolve to tracked rows -> keep the stored spelling (two
           different tracked rows are a conflict, never crossed by metadata);
        4. the stored name is tracked -> keep it (the incoming is a display or
           AniList spelling of the same series);
        5. the incoming name is tracked and the stored one is not -> adopt the
           stable tracked spelling (a real re-key, not a metadata rewrite);
        6. neither is tracked -> an explicitly different name is written.
        """
        from app.parser import normalize_text, get_canonical_series_name

        existing = _coerce_text(existing_name)
        incoming = _coerce_text(incoming_name)
        if not incoming:
            return incoming
        if not existing:
            return incoming
        if normalize_text(existing) == normalize_text(incoming):
            return existing
        try:
            if get_canonical_series_name(existing) == get_canonical_series_name(incoming):
                logger.info(
                    "SERIES_NAME_PRESERVED stored=%r incoming=%r reason=canonical_alias",
                    existing, incoming,
                )
                return existing
        except Exception:
            pass
        existing_row = self._get_series_identity_by_title(existing)
        incoming_row = self._get_series_identity_by_title(incoming)
        if incoming_row is not None:
            incoming_stable = _coerce_text(incoming_row.get("name")) or incoming
            if existing_row is None:
                # The stored spelling is not tracked but the incoming one names a
                # tracked series: adopt the tracked stable spelling.
                return incoming_stable
            existing_stable = _coerce_text(existing_row.get("name"))
            same_row = (
                existing_row.get("id") is not None
                and incoming_row.get("id") is not None
                and existing_row.get("id") == incoming_row.get("id")
            )
            if not same_row and existing_stable and incoming_stable:
                same_row = (
                    normalize_text(existing_stable) == normalize_text(incoming_stable)
                )
            if not same_row:
                logger.info(
                    "SERIES_NAME_PRESERVED stored=%r incoming=%r reason=tracked_conflict",
                    existing, incoming,
                )
                return existing
            # Both spellings belong to the same tracked series.  Prefer the
            # tracked STABLE spelling over a display/AniList spelling.
            if normalize_text(existing) == normalize_text(existing_stable):
                logger.debug(
                    "SERIES_NAME_PRESERVED stored=%r incoming=%r reason=stored_is_stable_key",
                    existing, incoming,
                )
                return existing
            if normalize_text(incoming_stable) == normalize_text(incoming):
                logger.info(
                    "SERIES_NAME_PRESERVED stored=%r incoming=%r reason=adopt_stable_spelling",
                    existing, incoming,
                )
                return incoming
            logger.info(
                "SERIES_NAME_PRESERVED stored=%r incoming=%r reason=stored_is_stable_key",
                existing, incoming,
            )
            return existing
        if existing_row is not None:
            logger.info(
                "SERIES_NAME_PRESERVED stored=%r incoming=%r reason=stored_belongs_to_tracked_series",
                existing, incoming,
            )
            return existing
        return incoming

    def store_series_anilist_metadata(self, series_id: int, meta: Dict[str, Any]) -> None:
        """Persist AniList metadata for a tracked series.

        Writes the cosmetic ``display_title``, ``anilist_id`` and
        ``anilist_meta_json`` and registers alias overrides for every AniList
        spelling, but **never changes ``name``** (the stable matching key).
        Sets ``match_source='anilist'`` only when it is still blank.
        """
        sid = _coerce_int(series_id)
        if sid is None or not isinstance(meta, dict):
            return
        from app.parser import normalize_text
        from app.series_identity import anilist_title_list

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_tracking WHERE id = ?;", (sid,))
            found = cur.fetchone()
        if found is None:
            return
        row = dict(found)
        # Contract v2 §2.2: a manually-created ("different series") tracker is
        # identity-locked; automatic AniList enrichment must not rewrite it.
        if _coerce_int(row.get("identity_locked")) == 1:
            logger.info(
                "Skipping AniList metadata store for identity-locked series %r",
                _coerce_text(row.get("name")),
            )
            return
        name = _coerce_text(row.get("name"))
        titles = anilist_title_list(meta)
        display_source = (
            meta.get("english_title") or meta.get("title") or meta.get("canonical_title")
        )
        display_value: Optional[str] = None
        if _coerce_text(display_source):
            display = _coerce_text(display_source)
            # Cosmetic only: identical to the stable key means "fall back to name".
            display_value = display if normalize_text(display) != normalize_text(name) else ""
        synonyms_raw = meta.get("synonyms") or []
        if isinstance(synonyms_raw, (list, tuple, set)):
            synonyms = [
                _coerce_text(value) for value in synonyms_raw if _coerce_text(value)
            ]
        else:
            synonyms = [_coerce_text(synonyms_raw)] if _coerce_text(synonyms_raw) else []
        stored = {
            "title": _coerce_text(meta.get("title") or display_source),
            "english_title": _coerce_text(meta.get("english_title") or display_source),
            "romaji_title": _coerce_text(meta.get("romaji_title")),
            "native_title": _coerce_text(meta.get("native_title")),
            "synonyms": synonyms,
            "source": _coerce_text(meta.get("source")) or "AniList",
            "fetched_at": _coerce_text(meta.get("fetched_at"))
            or datetime.utcnow().isoformat() + "Z",
        }
        # Contract v3 §3: the stored JSON is the store of record for the AniList
        # publication fields (format / status / volumes / chapters).
        try:
            from app.providers.metadata import NovelMetadataProvider
            stored.update(NovelMetadataProvider.publication_from_meta(meta))
        except Exception as exc:
            logger.debug("Could not extend stored AniList metadata: %s", exc)
        anilist_id = _coerce_int(meta.get("anilist_id"))
        if anilist_id is None:
            anilist_id = _coerce_int(meta.get("id"))
        match_source = None if _coerce_text(row.get("match_source")) else "anilist"
        self.update_series_identity(
            sid,
            display_title=display_value,
            anilist_id=anilist_id,
            anilist_meta_json=json.dumps(stored, ensure_ascii=False),
            match_source=match_source,
        )
        calibre_id = _coerce_int(row.get("calibre_series_id"))
        for candidate in titles:
            if not candidate or normalize_text(candidate) == normalize_text(name):
                continue
            try:
                self.add_series_alias_override(
                    candidate, name,
                    calibre_series_id=calibre_id,
                    origin="anilist",
                    actor="anilist",
                )
            except ValueError:
                continue

    def store_series_publication_metadata(self, series_id: int, meta: Dict[str, Any]) -> None:
        """Merge only the AniList publication keys into a series' ``anilist_meta_json``.

        Used by the bundle-volume inference recovery path (scheduler §4).  The
        merge preserves every other stored key and never touches ``name``,
        ``display_title``, ``calibre_series_id`` or the alias overrides.  It is a
        safe no-op for unknown ids, invalid/empty metadata and ``identity_locked``
        rows, and it never raises.
        """
        try:
            sid = _coerce_int(series_id)
            if sid is None:
                return
            if not isinstance(meta, dict):
                return
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM series_tracking WHERE id = ?;", (sid,))
                found = cur.fetchone()
            if found is None:
                return
            row = dict(found)
            if _coerce_int(row.get("identity_locked")) == 1:
                logger.debug(
                    "Skipping publication metadata store for identity-locked series %r",
                    _coerce_text(row.get("name")),
                )
                return
            from app.providers.metadata import NovelMetadataProvider
            publication = NovelMetadataProvider.publication_from_meta(meta)
            # "invalid meta" (nothing known) is a no-op.
            if not any(
                value not in (None, "", False) for value in publication.values()
            ):
                return
            existing: Dict[str, Any] = {}
            meta_raw = row.get("anilist_meta_json")
            if isinstance(meta_raw, dict):
                existing = dict(meta_raw)
            elif isinstance(meta_raw, str) and meta_raw.strip():
                try:
                    parsed = json.loads(meta_raw)
                except (TypeError, ValueError):
                    parsed = {}
                if isinstance(parsed, dict):
                    existing = parsed
            existing.update(publication)
            self.update_series_identity(
                sid,
                anilist_meta_json=json.dumps(existing, ensure_ascii=False),
            )
        except Exception as exc:  # pragma: no cover - defensive: never raises
            logger.debug("store_series_publication_metadata failed: %s", exc)

    def _series_release_spellings(self, source_name: str) -> List[str]:
        """Every spelling under which releases may store *source_name*.

        Covers the exact name, the conservative cleaned variant, ALL
        ``series_identity.title_candidates`` (splits, colon parts/prefixes,
        parentheticals), the parser's parse-shape ``series_name`` and alias
        override sources that target the old name.
        """
        from app.parser import normalize_text
        from app.series_identity import clean_series_for_matching, title_candidates

        source = _coerce_text(source_name)
        spellings: List[str] = []
        seen: Set[str] = set()

        def add(value: Any) -> None:
            text = _coerce_text(value)
            if not text:
                return
            norm = normalize_text(text)
            if not norm or norm in seen:
                return
            seen.add(norm)
            spellings.append(text)

        add(source)
        try:
            add(clean_series_for_matching(source))
        except Exception:
            pass
        try:
            for candidate in title_candidates(source):
                add(candidate)
        except Exception:
            pass
        try:
            from app.parser import extract_series_and_volume
            add(extract_series_and_volume(source).get("series_name"))
        except Exception:
            pass
        try:
            source_norm = normalize_text(source)
            for override in self.get_series_alias_overrides():
                target_norm = _coerce_text(override.get("target_norm")) or normalize_text(
                    override.get("target_name")
                )
                if target_norm == source_norm:
                    add(override.get("source_name"))
        except Exception:
            pass
        return spellings

    def _rekey_releases_to_target(self, cur, spellings: Sequence[str],
                                  target_name: str) -> int:
        """Re-key releases stored under any of *spellings* to *target_name*.

        A spelling that only differs from the canonical target by case or
        punctuation still canonicalizes its releases (its normalised form equals
        ``target_norm``; discarding that norm would silently skip exactly those
        rows).  Only the verbatim target spelling is skipped.
        """
        from app.parser import normalize_text

        target = _coerce_text(target_name)
        target_norm = normalize_text(target)
        norms: Set[str] = set()
        for value in spellings:
            text = _coerce_text(value)
            if not text:
                continue
            norm = normalize_text(text)
            if not norm:
                continue
            if norm == target_norm and text == target:
                continue
            norms.add(norm)
        if not norms:
            return 0
        # Contract v2 §2.4: an explicit user re-bind pin must survive a series
        # merge, so pinned releases are never re-keyed.
        cur.execute(
            "SELECT id, series_name FROM releases "
            "WHERE series_name IS NOT NULL AND trim(series_name) != '' "
            "AND COALESCE(series_pinned, 0) = 0;"
        )
        updates = []
        for row in cur.fetchall():
            stored = _coerce_text(row["series_name"])
            if not stored or stored == target:
                continue
            if normalize_text(stored) in norms:
                updates.append((target, row["id"]))
        if updates:
            cur.executemany(
                "UPDATE releases SET series_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                updates,
            )
        return len(updates)

    def _retarget_alias_overrides(self, retired_norm: str, target_name: str,
                                  calibre_series_id: Optional[int] = None) -> int:
        """Re-point alias overrides away from a merged-away old spelling.

        After ``link_series_to_calibre`` deletes the duplicate tracker row, any
        override whose ``target_norm`` still points at the retired spelling is a
        DEAD target: ``resolve_series_title`` would return it before the live
        canonical row and shadow the Calibre match.  Those rows are retargeted to
        the new stable name (or deleted when they would become a self-map).  An
        override whose SOURCE is the new canonical spelling is also removed: it
        would otherwise shadow the canonical name itself.
        """
        from app.parser import normalize_text

        retired = normalize_text(retired_norm)
        target = _coerce_text(target_name)
        target_norm = normalize_text(target)
        if not retired or not target:
            return 0
        cid = _coerce_int(calibre_series_id)
        changed = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, source_norm FROM series_alias_overrides WHERE target_norm = ?;",
                (retired,),
            )
            stale_rows = cur.fetchall()
            for row in stale_rows:
                if _coerce_text(row["source_norm"]) == target_norm:
                    cur.execute(
                        "DELETE FROM series_alias_overrides WHERE id = ?;", (row["id"],)
                    )
                else:
                    cur.execute(
                        "UPDATE series_alias_overrides SET target_name = ?, target_norm = ?, "
                        "calibre_series_id = COALESCE(?, calibre_series_id) WHERE id = ?;",
                        (target, target_norm, cid, row["id"]),
                    )
                changed += 1
            # A source spelling identical to the canonical name would shadow it.
            # Inert self-maps and learned rows are removed; an explicit MANUAL
            # mapping of the canonical spelling to another series is user intent
            # and is preserved (logged) rather than silently deleted.
            cur.execute(
                "SELECT id, target_norm, origin FROM series_alias_overrides "
                "WHERE source_norm = ?;",
                (target_norm,),
            )
            for row in cur.fetchall():
                row_target_norm = _coerce_text(row["target_norm"])
                manual = (_coerce_text(row["origin"]) or "manual") == "manual"
                if row_target_norm == target_norm or not manual:
                    cur.execute(
                        "DELETE FROM series_alias_overrides WHERE id = ?;", (row["id"],)
                    )
                    changed += 1
                else:
                    logger.warning(
                        "Manual alias override for canonical spelling %r targets %r; "
                        "keeping the explicit user mapping.",
                        target, row_target_norm,
                    )
            conn.commit()
        return changed

    def link_series_to_calibre(self, source_name: str, calibre_series: Dict[str, Any],
                               actor: str = "user", owned_volumes: Optional[List[float]] = None,
                               origin: str = "manual") -> Dict[str, Any]:
        """Link/merge one discovered or tracked series to a Calibre series.

        The Calibre name becomes the STABLE key: the target ``series_tracking``
        row is created/merged, every release stored under a source spelling is
        re-keyed to it, the old tracker row is deleted and alias overrides are
        registered so future releases inherit the mapping.  Release statuses are
        deliberately NOT touched (owner B reconciles them); ``origin='candidate'``
        marks an automatic Calibre match (``match_source='calibre'``).
        """
        from urllib.parse import quote_plus
        from app.parser import normalize_text

        if not isinstance(calibre_series, dict):
            raise ValueError("calibre_series must be a dict")
        target_name = _coerce_text(
            calibre_series.get("series_name") or calibre_series.get("name")
        )
        if not target_name:
            raise ValueError("Calibre series name is required")
        source = _coerce_text(source_name)
        if not source:
            raise ValueError("source series name is required")
        calibre_id = _coerce_int(calibre_series.get("series_id"))
        if calibre_id is None:
            calibre_id = _coerce_int(calibre_series.get("id"))
        if calibre_id is None:
            calibre_id = _coerce_int(calibre_series.get("calibre_series_id"))
        book_count = _coerce_int(calibre_series.get("book_count")) or 0
        latest_index = _coerce_float(calibre_series.get("latest_index"))
        source_norm = normalize_text(source)
        target_norm = normalize_text(target_name)
        origin = _coerce_text(origin) or "manual"
        match_source = "calibre" if origin == "candidate" else "manual"
        incoming_volumes: Set[float] = set(_coerce_owned_volumes(owned_volumes))
        if not incoming_volumes:
            incoming_volumes.update(_coerce_owned_volumes(
                calibre_series.get("owned_volumes_json")
                if calibre_series.get("owned_volumes_json") is not None
                else calibre_series.get("owned_volumes")
            ))
        if not incoming_volumes and latest_index > 0:
            incoming_volumes.add(latest_index)
        # Contract v2 §2.2/§2.4: automatic linking never folds an identity-locked
        # ("different series") tracker into another series.  Explicit user links
        # (origin != 'candidate') are still allowed.
        if origin == "candidate":
            candidate_source_row = self.get_series_by_name(source)
            candidate_target_row = self.get_series_by_name(target_name)
            if (
                candidate_source_row is not None
                and _coerce_int(candidate_source_row.get("identity_locked")) == 1
            ) or (
                candidate_target_row is not None
                and _coerce_int(candidate_target_row.get("identity_locked")) == 1
            ):
                logger.info(
                    "Skipping auto-link of identity-locked series (source=%r target=%r)",
                    source, target_name,
                )
                return {
                    "status": "skipped",
                    "reason": "identity_locked",
                    "source": source,
                    "target": target_name,
                    "calibre_series_id": calibre_id,
                    "merged": False,
                    "rekeyed_releases": 0,
                    "owned_volumes": [],
                    "book_count": 0,
                }
        spellings = self._series_release_spellings(source)
        merged = False
        rekeyed = 0
        target_id: Optional[int] = None
        combined_volumes: Set[float] = set(incoming_volumes)
        combined_book_count = book_count
        with self.get_connection() as conn:
            cur = conn.cursor()
            source_row: Optional[Dict[str, Any]] = None
            cur.execute(
                "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?)) LIMIT 1;",
                (source,),
            )
            found = cur.fetchone()
            if found is not None:
                source_row = dict(found)
            if source_row is None:
                # "alias override source targeting it": a spelling that used to
                # map to the source now being linked is merged as well.
                cur.execute(
                    "SELECT source_name FROM series_alias_overrides WHERE target_norm = ? LIMIT 1;",
                    (source_norm,),
                )
                override_row = cur.fetchone()
                if override_row is not None:
                    alias_source = _coerce_text(override_row["source_name"])
                    cur.execute(
                        "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?)) LIMIT 1;",
                        (alias_source,),
                    )
                    found = cur.fetchone()
                    if found is not None:
                        source_row = dict(found)
            cur.execute(
                "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?)) LIMIT 1;",
                (target_name,),
            )
            found = cur.fetchone()
            target_row = dict(found) if found is not None else None

            target_volumes: Set[float] = (
                set(_coerce_owned_volumes(target_row.get("owned_volumes_json")))
                if target_row is not None else set()
            )
            source_volumes: Set[float] = (
                set(_coerce_owned_volumes(source_row.get("owned_volumes_json")))
                if source_row is not None else set()
            )
            combined_volumes = target_volumes | incoming_volumes | source_volumes
            target_book_count = (
                _coerce_int(target_row.get("book_count")) or 0
            ) if target_row is not None else 0
            target_latest = (
                _coerce_float(target_row.get("latest_index"))
            ) if target_row is not None else 0.0
            target_monitored = (
                _coerce_int(target_row.get("is_monitored")) or 0
            ) if target_row is not None else 0
            source_monitored = (
                _coerce_int(source_row.get("is_monitored")) or 0
            ) if source_row is not None else 0
            combined_book_count = max(target_book_count, book_count)
            combined_latest = max(target_latest, latest_index)

            def source_value(key: str) -> str:
                return _coerce_text(source_row.get(key)) if source_row is not None else ""

            authors_value = _coerce_text(calibre_series.get("authors")) or source_value("authors")
            description_value = (
                _coerce_text(calibre_series.get("description")) or source_value("description")
            )
            cover_value = _coerce_text(calibre_series.get("cover_url")) or source_value("cover_url")
            display_value = (
                _coerce_text(calibre_series.get("display_title")) or source_value("display_title")
            )
            anilist_id_value = _coerce_int(calibre_series.get("anilist_id"))
            if anilist_id_value is None and source_row is not None:
                anilist_id_value = _coerce_int(source_row.get("anilist_id"))
            anilist_meta_value = (
                _coerce_text(calibre_series.get("anilist_meta_json"))
                or source_value("anilist_meta_json")
            )
            # The display title is cosmetic: when a merged source row carries only
            # AniList metadata (e.g. a discovery tracker created under the English
            # title), derive the display title from that metadata now that the
            # stable tracking key has become the Calibre spelling.
            if not display_value and anilist_meta_value:
                try:
                    from app.parser import normalize_text as _normalize_title
                    parsed_meta = json.loads(anilist_meta_value)
                except (TypeError, ValueError):
                    parsed_meta = None
                except Exception:
                    parsed_meta = None
                if isinstance(parsed_meta, dict):
                    candidate_display = (
                        _coerce_text(parsed_meta.get("english_title"))
                        or _coerce_text(parsed_meta.get("canonical_title"))
                        or _coerce_text(parsed_meta.get("title"))
                    )
                    try:
                        if (
                            candidate_display
                            and _normalize_title(candidate_display)
                            != _normalize_title(target_name)
                        ):
                            display_value = candidate_display
                    except Exception:
                        if candidate_display and candidate_display != target_name:
                            display_value = candidate_display
            combined_monitored = max(target_monitored, source_monitored)
            owned_json = json.dumps(sorted(combined_volumes))

            if target_row is None:
                cur.execute("""
                    INSERT INTO series_tracking (
                        calibre_series_id, name, authors, book_count, latest_index,
                        is_monitored, website_url, owned_volumes_json, description,
                        cover_url, display_title, anilist_id, anilist_meta_json,
                        match_source, last_sync
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                """, (
                    calibre_id, target_name, authors_value, combined_book_count,
                    combined_latest,
                    f"https://jnovels.com/?s={quote_plus(target_name)}",
                    owned_json, description_value, cover_value, display_value,
                    anilist_id_value, anilist_meta_value, match_source,
                ))
                target_id = cur.lastrowid
            else:
                cur.execute("""
                    UPDATE series_tracking SET
                        calibre_series_id = COALESCE(?, calibre_series_id),
                        book_count = MAX(COALESCE(book_count, 0), ?),
                        latest_index = MAX(COALESCE(latest_index, 0.0), ?),
                        is_monitored = MAX(COALESCE(is_monitored, 0), ?),
                        authors = CASE WHEN COALESCE(authors, '') = '' THEN ? ELSE authors END,
                        description = CASE WHEN COALESCE(description, '') = '' THEN ? ELSE description END,
                        cover_url = CASE WHEN COALESCE(cover_url, '') = '' THEN ? ELSE cover_url END,
                        display_title = CASE WHEN COALESCE(display_title, '') = '' THEN ? ELSE display_title END,
                        anilist_id = COALESCE(anilist_id, ?),
                        anilist_meta_json = CASE WHEN COALESCE(anilist_meta_json, '') = '' THEN ? ELSE anilist_meta_json END,
                        match_source = COALESCE(?, match_source),
                        owned_volumes_json = ?,
                        last_sync = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (
                    calibre_id, combined_book_count, combined_latest,
                    combined_monitored, authors_value, description_value,
                    cover_value, display_value, anilist_id_value,
                    anilist_meta_value, match_source, owned_json,
                    int(target_row["id"]),
                ))
                target_id = int(target_row["id"])

            # Every release stored under a source spelling is re-keyed to the
            # stable (Calibre) name, even when no tracker row existed for it.
            rekeyed = self._rekey_releases_to_target(cur, spellings, target_name)
            if (
                source_row is not None
                and target_id is not None
                and int(source_row["id"]) != int(target_id)
            ):
                cur.execute(
                    "DELETE FROM series_tracking WHERE id = ?;", (int(source_row["id"]),)
                )
                merged = True
            conn.commit()

        # Register alias overrides outside the write transaction so future feed
        # releases resolve to the stable name before matching.
        for spelling in spellings:
            if normalize_text(spelling) == target_norm:
                continue
            try:
                self.add_series_alias_override(
                    spelling, target_name,
                    calibre_series_id=calibre_id,
                    origin=origin,
                    actor=actor,
                )
            except ValueError:
                continue
        # Owner-B reconciliation fix: alias overrides that still target the
        # merged-away source spelling are dead targets and would shadow the new
        # canonical Calibre name in resolve_series_title; retarget them.
        try:
            self._retarget_alias_overrides(source_norm, target_name, calibre_id)
        except Exception as exc:
            logger.debug(
                "Could not retarget alias overrides for %r: %s", source, exc
            )
        return {
            "status": "success",
            "source": source,
            "target": target_name,
            "calibre_series_id": calibre_id,
            "merged": merged,
            "rekeyed_releases": rekeyed,
            "owned_volumes": sorted(combined_volumes),
            "book_count": combined_book_count,
        }

    def match_discovered_series_to_calibre(self, source_name: str,
                                           calibre_series: Dict[str, Any],
                                           actor: str = "user") -> Dict[str, Any]:
        """Manual match for a discovery series that may have no tracker row.

        Creates a temporary tracker row for the discovered spelling, delegates to
        :meth:`link_series_to_calibre` (merge + re-key + alias overrides), then
        marks the target monitored so the series leaves Discovery.
        """
        from urllib.parse import quote_plus
        from app.parser import normalize_text

        source = _coerce_text(source_name)
        if not source:
            raise ValueError("discovery series name is required")
        if not isinstance(calibre_series, dict):
            raise ValueError("calibre_series must be a dict")
        target_name = _coerce_text(
            calibre_series.get("series_name") or calibre_series.get("name")
        )
        if not target_name:
            raise ValueError("Calibre series name is required")
        source_norm = normalize_text(source)
        target_norm = normalize_text(target_name)
        if source_norm != target_norm and self.get_series_by_name(source) is None:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT OR IGNORE INTO series_tracking
                        (name, is_monitored, website_url, last_sync)
                    VALUES (?, 1, ?, CURRENT_TIMESTAMP);
                """, (source, f"https://jnovels.com/?s={quote_plus(source)}"))
                conn.commit()
        result = dict(
            self.link_series_to_calibre(source, calibre_series, actor=actor)
        )
        target_row = self.get_series_by_name(target_name)
        if target_row is not None:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE series_tracking SET is_monitored = 1 WHERE id = ?;",
                    (int(target_row["id"]),),
                )
                conn.commit()
        result["discovered"] = True
        return result

    def _series_candidate_norms(self, row: Dict[str, Any]) -> Set[str]:
        """Normalised AniList/candidate spellings of a tracked series row."""
        from app.parser import normalize_text
        from app.series_identity import anilist_title_list, clean_series_for_matching

        values: List[Any] = [row.get("name"), row.get("display_title")]
        meta_raw = row.get("anilist_meta_json")
        meta: Dict[str, Any] = {}
        if isinstance(meta_raw, dict):
            meta = meta_raw
        elif isinstance(meta_raw, str) and meta_raw.strip():
            try:
                parsed = json.loads(meta_raw)
                meta = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                meta = {}
        values.extend(anilist_title_list(meta))
        norms = {
            normalize_text(value)
            for value in values
            if isinstance(value, str) and value.strip()
        }
        try:
            norms.add(normalize_text(clean_series_for_matching(row.get("name"))))
        except Exception:
            pass
        try:
            row_norm = normalize_text(row.get("name"))
            for override in self.get_series_alias_overrides():
                target_norm = _coerce_text(override.get("target_norm")) or normalize_text(
                    override.get("target_name")
                )
                if target_norm == row_norm:
                    norms.add(normalize_text(override.get("source_name")))
        except Exception:
            pass
        norms.discard("")
        return norms

    def _series_are_duplicates(self, keep: Dict[str, Any], drop: Dict[str, Any]) -> bool:
        """True when two tracked rows are the same series under different spellings."""
        from app.parser import normalize_text

        keep_norm = normalize_text(keep.get("name"))
        drop_norm = normalize_text(drop.get("name"))
        if not keep_norm or not drop_norm:
            return False
        if keep_norm == drop_norm:
            return True
        try:
            if drop_norm in self._series_candidate_norms(keep):
                return True
            if keep_norm in self._series_candidate_norms(drop):
                return True
        except Exception:
            return False
        return False

    def merge_duplicate_series(self, keep_name: str, drop_name: str,
                               actor: str = "scheduler") -> Dict[str, Any]:
        """Merge a duplicate tracker row into *keep_name* and re-key its releases.

        De-duplication is limited to normalised-title equality or AniList-candidate
        equality (never a bare colon prefix), so a genuine spin-off is not merged.
        """
        from app.parser import normalize_text

        keep = self.get_series_by_name(keep_name)
        drop = self.get_series_by_name(drop_name)
        skipped: Dict[str, Any] = {
            "status": "skipped",
            "keep": _coerce_text(keep_name),
            "drop": _coerce_text(drop_name),
            "merged": False,
            "rekeyed_releases": 0,
        }
        if keep is None or drop is None:
            skipped["reason"] = "missing"
            return skipped
        if keep.get("id") == drop.get("id"):
            skipped["reason"] = "same_row"
            return skipped
        # Contract v2 §2.2: an identity-locked manual tracker is never folded into
        # (or allowed to absorb) another tracked series by automatic de-duplication.
        if _coerce_int(keep.get("identity_locked")) == 1 or _coerce_int(drop.get("identity_locked")) == 1:
            skipped["reason"] = "identity_locked"
            return skipped
        if not self._series_are_duplicates(keep, drop):
            skipped["reason"] = "not_duplicates"
            return skipped
        target_name = _coerce_text(keep.get("name"))
        drop_volumes = set(_coerce_owned_volumes(drop.get("owned_volumes_json")))
        keep_volumes = set(_coerce_owned_volumes(keep.get("owned_volumes_json")))
        combined_volumes = sorted(keep_volumes | drop_volumes)
        spellings = self._series_release_spellings(drop.get("name"))
        rekeyed = 0
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE series_tracking SET
                    book_count = MAX(COALESCE(book_count, 0), ?),
                    latest_index = MAX(COALESCE(latest_index, 0.0), ?),
                    is_monitored = MAX(COALESCE(is_monitored, 0), ?),
                    authors = CASE WHEN COALESCE(authors, '') = '' THEN ? ELSE authors END,
                    description = CASE WHEN COALESCE(description, '') = '' THEN ? ELSE description END,
                    cover_url = CASE WHEN COALESCE(cover_url, '') = '' THEN ? ELSE cover_url END,
                    display_title = CASE WHEN COALESCE(display_title, '') = '' THEN ? ELSE display_title END,
                    anilist_id = COALESCE(anilist_id, ?),
                    anilist_meta_json = CASE WHEN COALESCE(anilist_meta_json, '') = '' THEN ? ELSE anilist_meta_json END,
                    owned_volumes_json = ?,
                    last_sync = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (
                _coerce_int(drop.get("book_count")) or 0,
                _coerce_float(drop.get("latest_index")),
                _coerce_int(drop.get("is_monitored")) or 0,
                _coerce_text(drop.get("authors")),
                _coerce_text(drop.get("description")),
                _coerce_text(drop.get("cover_url")),
                _coerce_text(drop.get("display_title")),
                _coerce_int(drop.get("anilist_id")),
                _coerce_text(drop.get("anilist_meta_json")),
                json.dumps(combined_volumes),
                int(keep["id"]),
            ))
            rekeyed = self._rekey_releases_to_target(cur, spellings, target_name)
            cur.execute("DELETE FROM series_tracking WHERE id = ?;", (int(drop["id"]),))
            conn.commit()
        try:
            self.add_series_alias_override(
                drop.get("name"), target_name,
                calibre_series_id=_coerce_int(keep.get("calibre_series_id")),
                origin="candidate",
                actor=actor,
            )
        except ValueError:
            pass
        return {
            "status": "success",
            "keep": target_name,
            "drop": _coerce_text(drop.get("name")),
            "merged": True,
            "rekeyed_releases": rekeyed,
        }

    def _merge_duplicate_trackers_for(self, target_name: str, actor: str = "scheduler") -> int:
        """Merge every unlinked tracker row that duplicates *target_name*."""
        from app.parser import normalize_text

        keep = self.get_series_by_name(target_name)
        if keep is None:
            return 0
        if _coerce_int(keep.get("identity_locked")) == 1:
            return 0
        keep_norm = normalize_text(keep.get("name"))
        keep_candidates = self._series_candidate_norms(keep)
        merged = 0
        for other in self.get_series_identity_rows():
            try:
                if other.get("calibre_series_id") is not None:
                    continue
                if other.get("id") == keep.get("id"):
                    continue
                if _coerce_int(other.get("identity_locked")) == 1:
                    continue
                other_norm = normalize_text(other.get("name"))
                if not other_norm or other_norm == keep_norm:
                    continue
                other_candidates = self._series_candidate_norms(other)
                if other_norm in keep_candidates or keep_norm in other_candidates:
                    result = self.merge_duplicate_series(
                        keep.get("name"), other.get("name"), actor=actor
                    )
                    if result.get("merged"):
                        merged += 1
            except Exception as exc:
                logger.debug(
                    "Could not merge duplicate tracker %r: %s", other.get("name"), exc
                )
        return merged

    def auto_link_series_to_calibre(self, calibre_series_list: List[Dict[str, Any]],
                                    actor: str = "scheduler") -> List[Dict[str, Any]]:
        """Link tracked rows without a ``calibre_series_id`` to Calibre series.

        Candidates come from the stable name, its AniList titles/synonyms
        (``anilist_meta_json``), the display title and alias-override sources;
        exact matches win, then conservative fuzzy via
        ``CalibreSeriesIndex.match(threshold=AUTO_LINK_FUZZY_THRESHOLD)``.
        Ambiguous/unsure rows are skipped (manual match), and one bad row never
        aborts the pass.  Returns the list of link summaries.
        """
        from app.parser import normalize_text
        from app.series_identity import (
            AUTO_LINK_FUZZY_THRESHOLD,
            CalibreSeriesIndex,
            anilist_title_list,
        )

        results: List[Dict[str, Any]] = []
        series_list = [s for s in (calibre_series_list or []) if isinstance(s, dict)]
        if not series_list:
            return results
        index = CalibreSeriesIndex(series_list)
        alias_sources_by_target: Dict[str, List[str]] = {}
        alias_target_by_source: Dict[str, str] = {}
        try:
            for override in self.get_series_alias_overrides():
                source_norm = _coerce_text(override.get("source_norm")) or normalize_text(
                    override.get("source_name")
                )
                target_name = _coerce_text(override.get("target_name"))
                target_norm = _coerce_text(override.get("target_norm")) or normalize_text(
                    target_name
                )
                if not source_norm or not target_name:
                    continue
                alias_sources_by_target.setdefault(target_norm, []).append(
                    _coerce_text(override.get("source_name"))
                )
                alias_target_by_source[source_norm] = target_name
        except Exception:
            alias_sources_by_target = {}
            alias_target_by_source = {}
        linked_targets: List[str] = []
        for row in self.get_series_identity_rows():
            try:
                if row.get("calibre_series_id") is not None:
                    continue
                # Contract v2 §2.2: identity-locked manual series are never
                # auto-linked/enriched into an existing series.
                if _coerce_int(row.get("identity_locked")) == 1:
                    continue
                name = _coerce_text(row.get("name"))
                if not name:
                    continue
                row_norm = normalize_text(name)
                candidates: List[str] = [name]
                # Candidates are the stable name, its STORED AniList
                # titles/synonyms and alias-override spellings only (never bare
                # colon-prefix fragments, which would link a spin-off to its
                # parent franchise).
                meta_raw = row.get("anilist_meta_json")
                row_meta: Dict[str, Any] = {}
                if isinstance(meta_raw, dict):
                    row_meta = meta_raw
                elif isinstance(meta_raw, str) and meta_raw.strip():
                    try:
                        parsed_meta = json.loads(meta_raw)
                        row_meta = parsed_meta if isinstance(parsed_meta, dict) else {}
                    except (TypeError, ValueError):
                        row_meta = {}
                try:
                    candidates.extend(anilist_title_list(row_meta))
                except Exception:
                    pass
                display = _coerce_text(row.get("display_title"))
                if display:
                    candidates.append(display)
                candidates.extend(alias_sources_by_target.get(row_norm, []))
                # The row's own spelling may itself be a known alias source: its
                # canonical target is the stable name to match against.
                alias_target = alias_target_by_source.get(row_norm)
                if alias_target:
                    candidates.append(alias_target)
                matched = index.match(
                    name,
                    alt_names=candidates,
                    threshold=AUTO_LINK_FUZZY_THRESHOLD,
                )
                if matched is None:
                    continue
                target_name = _coerce_text(
                    matched.get("series_name") or matched.get("name")
                )
                if not target_name:
                    continue
                volumes = _coerce_owned_volumes(row.get("owned_volumes_json"))
                result = self.link_series_to_calibre(
                    name, matched, actor=actor, owned_volumes=volumes, origin="candidate"
                )
                results.append(result)
                linked_targets.append(_coerce_text(result.get("target")) or target_name)
            except Exception as exc:
                logger.debug(
                    "Could not auto-link tracked series %r: %s", row.get("name"), exc
                )
        seen_targets: Set[str] = set()
        for target_name in linked_targets:
            norm = normalize_text(target_name)
            if not norm or norm in seen_targets:
                continue
            seen_targets.add(norm)
            try:
                self._merge_duplicate_trackers_for(target_name, actor=actor)
            except Exception as exc:
                logger.debug("Duplicate tracker cleanup failed for %r: %s", target_name, exc)
        return results

    # --- Release re-bind / "different series" + per-series identity reset (v2 §2) ---
    def _known_series_title_norms(self) -> Dict[str, str]:
        """Normalised spellings already owned by a tracked series or alias.

        Used to reject a "genuinely new title" that is actually the current series,
        an existing tracked name/display title/AniList title, or an alias
        source/target spelling.
        """
        from app.parser import normalize_text
        from app.series_identity import anilist_title_list

        known: Dict[str, str] = {}
        for row in self.get_series_identity_rows():
            name = _coerce_text(row.get("name"))
            if name:
                known.setdefault(normalize_text(name), "tracked name")
            display = _coerce_text(row.get("display_title"))
            if display:
                known.setdefault(normalize_text(display), "display title")
            meta_raw = row.get("anilist_meta_json")
            meta: Dict[str, Any] = {}
            if isinstance(meta_raw, dict):
                meta = meta_raw
            elif isinstance(meta_raw, str) and meta_raw.strip():
                try:
                    parsed = json.loads(meta_raw)
                    meta = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    meta = {}
            for title in anilist_title_list(meta):
                norm = normalize_text(title)
                if norm:
                    known.setdefault(norm, "anilist title")
        for override in self.get_series_alias_overrides():
            for field, label in (
                ("source_name", "alias source"),
                ("target_name", "alias target"),
            ):
                norm = normalize_text(_coerce_text(override.get(field)))
                if norm:
                    known.setdefault(norm, label)
        known.pop("", None)
        return known

    def _validate_new_series_title(self, new_title: Optional[str],
                                   previous_series: Optional[str]) -> str:
        """Validate a "different series"/detach title (contract v2 §2.3).

        The new title must be non-empty, 2..300 characters, and must not resolve
        to the release's current series or to any tracked name/display title/
        AniList title/alias source/target.
        """
        from app.parser import normalize_text

        text = _coerce_text(new_title)
        if not text:
            raise ValueError("a new series title is required")
        if len(text) < 2 or len(text) > 300:
            raise ValueError("new series title must be between 2 and 300 characters")
        new_norm = normalize_text(text)
        if not new_norm:
            raise ValueError("new series title must contain letters or digits")
        conflict_message = (
            "that title already belongs to an existing series; "
            "pick a genuinely new title"
        )
        if new_norm == normalize_text(previous_series):
            raise ValueError(conflict_message)
        if new_norm in self._known_series_title_norms():
            raise ValueError(conflict_message)
        return text

    def _create_locked_series(self, target_name: str,
                              actor: str = "user") -> Dict[str, Any]:
        """Create a brand-new manual tracker (``identity_locked=1``, no alias)."""
        from urllib.parse import quote_plus

        name = _coerce_text(target_name)
        conflict_message = (
            "that title already belongs to an existing series; "
            "pick a genuinely new title"
        )
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO series_tracking (
                        name, book_count, latest_index, is_monitored, website_url,
                        owned_volumes_json, identity_locked, match_source, last_sync
                    ) VALUES (?, 0, 0.0, 1, ?, '[]', 1, 'manual-new', CURRENT_TIMESTAMP);
                """, (name, f"https://jnovels.com/?s={quote_plus(name)}"))
                conn.commit()
                cur.execute(
                    "SELECT * FROM series_tracking WHERE lower(trim(name)) = lower(trim(?));",
                    (name,),
                )
                row = cur.fetchone()
        except sqlite3.IntegrityError:
            raise ValueError(conflict_message)
        logger.info("Created manually-locked series %r (actor=%s)", name, actor)
        return dict(row) if row is not None else {}

    @staticmethod
    def _set_release_pin(cur, release_id: int, series_name: str) -> None:
        """Pin one release to *series_name* (explicit user re-bind)."""
        cur.execute("""
            UPDATE releases SET
                series_name = ?,
                series_pinned = 1,
                series_pinned_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
        """, (_coerce_text(series_name), _utcnow_iso(), int(release_id)))

    def _resolve_bind_target(self, target_name: Optional[str],
                             calibre_series_id: Optional[int],
                             actor: str = "user"
                             ) -> Tuple[str, Optional[int], bool]:
        """Resolve (and ensure) the tracker for ``mode='bind'``.

        Returns ``(canonical_name, calibre_series_id, created_series)``.  An
        existing tracker is found by Calibre id (preferred) or exact name; a
        missing tracker is created through :meth:`link_series_to_calibre` (the
        same create/merge helper the discovery match uses).
        """
        from app.parser import normalize_text

        cid = _coerce_int(calibre_series_id)
        requested = _coerce_text(target_name)
        if cid is None and not requested:
            raise ValueError("target_name or calibre_series_id is required for mode 'bind'")

        row: Optional[Dict[str, Any]] = None
        if cid is not None:
            row = self.get_series_by_calibre_id(cid)
        if row is None and requested:
            row = self.get_series_by_name(requested)
            if row is None:
                identity = self._get_series_identity_by_title(requested)
                if identity is not None and identity.get("id") is not None:
                    row = identity
        if row is not None:
            canonical = _coerce_text(row.get("name"))
            if not canonical:
                raise LookupError(f"could not resolve tracked series {requested!r}")
            row_cid = cid if cid is not None else _coerce_int(row.get("calibre_series_id"))
            try:
                self._merge_duplicate_trackers_for(canonical, actor=actor)
            except Exception as exc:
                logger.debug("Bind duplicate-tracker cleanup failed for %r: %s", canonical, exc)
            return canonical, row_cid, False

        # No tracker yet: create/merge through the existing link helper.  Source
        # and target are the same spelling, so only the target row is created and
        # releases already carrying that spelling are canonicalised.
        if not requested:
            raise LookupError(f"unknown Calibre series id {cid}")
        calibre_series: Dict[str, Any] = {"series_name": requested}
        if cid is not None:
            calibre_series["series_id"] = cid
        summary = self.link_series_to_calibre(
            requested, calibre_series, actor=actor, origin="manual"
        )
        canonical = _coerce_text(summary.get("target")) or requested
        target_row = self.get_series_by_name(canonical)
        if target_row is None:
            raise LookupError(f"could not create tracked series {requested!r}")
        created_cid = _coerce_int(summary.get("calibre_series_id"))
        logger.info(
            "Ensured tracked series %r for release re-bind (calibre_id=%r)",
            canonical, created_cid,
        )
        return canonical, created_cid, True

    def rebind_release_series(self, release_id: int, mode: str,
                              target_name: Optional[str] = None,
                              calibre_series_id: Optional[int] = None,
                              remember: bool = False,
                              actor: str = "user") -> Dict[str, Any]:
        """Re-bind one release to an existing series or a genuinely new title.

        Modes (contract v2 §2.3):

        * ``bind``: move the release to an existing tracked/Calibre series (by
          ``calibre_series_id`` or exact ``target_name``), creating the tracker via
          the link helper when missing.  An alias override is only registered when
          ``remember=True``.
        * ``new_title`` / ``different_series``: create a brand-new identity-locked
          tracker under ``target_name`` and pin the release to it.  The title must
          not be the current series nor any known tracked/alias spelling.
        * ``detach``: like ``new_title`` but defaults the new name to the release's
          own parsed series; if that collides with the current/known spelling the
          caller must supply a genuinely new title.

        Any mode pins the release (``series_pinned=1``), never touches the release
        title/volume and logs ``RELEASE_SERIES_REBOUND``.
        """
        from app.parser import extract_series_and_volume, normalize_text
        from app.series_identity import clean_series_for_matching

        rid = _coerce_int(release_id)
        if rid is None:
            raise ValueError("release_id is required")
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM releases WHERE id = ?;", (rid,))
            found = cur.fetchone()
        if found is None:
            raise KeyError(f"release {rid} not found")
        release = dict(found)
        release_title = _coerce_text(release.get("title"))
        previous_series = _coerce_text(release.get("series_name"))

        requested_mode = _coerce_text(mode).lower().replace("-", "_")
        if requested_mode not in ("bind", "new_title", "different_series", "detach"):
            raise ValueError(f"unknown rebind mode {mode!r}")
        mode_norm = "new_title" if requested_mode == "different_series" else requested_mode

        created_series = False
        alias_recorded = False
        new_calibre_id: Optional[int] = None
        if mode_norm == "bind":
            new_series_name, new_calibre_id, created_series = self._resolve_bind_target(
                target_name, calibre_series_id, actor=actor
            )
            with self.get_connection() as conn:
                cur = conn.cursor()
                self._set_release_pin(cur, rid, new_series_name)
                conn.commit()
            if remember and previous_series and (
                normalize_text(previous_series) != normalize_text(new_series_name)
            ):
                try:
                    self.add_series_alias_override(
                        previous_series, new_series_name,
                        calibre_series_id=new_calibre_id,
                        origin="manual",
                        actor=actor,
                    )
                    alias_recorded = True
                except ValueError as exc:
                    logger.debug("Could not record rebind alias override: %s", exc)
                    alias_recorded = False
        else:
            if mode_norm == "new_title":
                new_series_name = self._validate_new_series_title(
                    target_name, previous_series
                )
            else:  # detach
                requested = _coerce_text(target_name)
                if requested:
                    new_series_name = self._validate_new_series_title(
                        requested, previous_series
                    )
                else:
                    derived = ""
                    try:
                        parsed = extract_series_and_volume(release_title) or {}
                        derived = _coerce_text(parsed.get("series_name"))
                    except Exception:
                        derived = ""
                    if not derived:
                        derived = _coerce_text(clean_series_for_matching(release_title))
                    if not derived:
                        raise ValueError(
                            "could not derive a new series title; provide target_name"
                        )
                    new_series_name = self._validate_new_series_title(
                        derived, previous_series
                    )
            self._create_locked_series(new_series_name, actor=actor)
            created_series = True
            with self.get_connection() as conn:
                cur = conn.cursor()
                self._set_release_pin(cur, rid, new_series_name)
                conn.commit()

        details = json.dumps({
            "mode": mode_norm,
            "previous_series": previous_series,
            "new_series": new_series_name,
            "calibre_series_id": new_calibre_id,
            "remember": bool(remember),
            "pinned": True,
        })
        self.log_activity(
            "RELEASE_SERIES_REBOUND",
            f"Re-bound release #{rid}: {previous_series or '(none)'} -> {new_series_name} ({mode_norm})",
            details,
        )
        return {
            "status": "success",
            "mode": mode_norm,
            "release_id": rid,
            "series_name": new_series_name,
            "calibre_series_id": new_calibre_id,
            "pinned": True,
            "created_series": created_series,
            "alias_recorded": alias_recorded,
            "previous_series": previous_series,
        }

    @staticmethod
    def _parsed_release_series_name(title: Any) -> str:
        """Title-parsed series spelling used to undo a manual re-index.

        Uses ``app.parser.clean_feed_title`` + ``extract_series_and_volume``
        (contract v2 §2.3 ``rekey_to_parsed``).  Returns ``''`` when the title
        cannot be parsed.
        """
        from app.parser import clean_feed_title, extract_series_and_volume

        raw = _coerce_text(title)
        if not raw:
            return ""
        candidates: List[str] = []
        try:
            cleaned = _coerce_text(clean_feed_title(raw))
        except Exception:
            cleaned = ""
        if cleaned and cleaned != raw:
            candidates.append(cleaned)
        candidates.append(raw)
        for candidate in candidates:
            try:
                parsed = extract_series_and_volume(candidate) or {}
            except Exception:
                continue
            series = _coerce_text(parsed.get("series_name"))
            if series:
                return series
        return ""

    def reset_series_identity(self, series_id: int, clear_aliases: bool = True,
                              clear_display: bool = True,
                              unpin_releases: bool = True,
                              rekey_to_parsed: bool = True,
                              actor: str = "user") -> Dict[str, Any]:
        """Forget the learned/manual identity state of one tracked series.

        Clears alias overrides pointing at (or sourced from) the series, the
        cosmetic AniList display data, the ``identity_locked`` flag and the
        per-release pins of that series, so automatic matching can resume.  The
        tracker itself and its ``calibre_series_id`` are kept (Calibre linkage is
        a library fact, not an identity override).

        ``rekey_to_parsed`` (default True) undoes a manual ``new_title``/``detach``
        re-index: for a manually-created series (``identity_locked=1`` or
        ``match_source='manual-new'``) releases stored under the manual series name
        are moved back to the title-parsed series spelling
        (``clean_feed_title`` + ``extract_series_and_volume``).  Releases that do
        not normalize to the series name are never touched.
        """
        from app.parser import normalize_text

        sid = _coerce_int(series_id)
        if sid is None:
            raise ValueError("series_id is required")
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_tracking WHERE id = ?;", (sid,))
            found = cur.fetchone()
        if found is None:
            raise KeyError(f"series {sid} not found")
        row = dict(found)
        name = _coerce_text(row.get("name"))
        name_norm = normalize_text(name)
        display = _coerce_text(row.get("display_title"))
        target_norms = {name_norm}
        if display:
            target_norms.add(normalize_text(display))
        target_norms.discard("")

        # Capture BEFORE clear_display wipes match_source: a manual new-title /
        # detached tracker is the only kind whose releases are re-keyed to parsed.
        previous_locked = _coerce_int(row.get("identity_locked")) or 0
        was_manual = (
            previous_locked == 1
            or _coerce_text(row.get("match_source")) == "manual-new"
        )

        aliases_removed = 0
        if clear_aliases:
            for override in self.get_series_alias_overrides():
                source_norm = _coerce_text(override.get("source_norm")) or normalize_text(
                    override.get("source_name")
                )
                target_norm = _coerce_text(override.get("target_norm")) or normalize_text(
                    override.get("target_name")
                )
                if target_norm in target_norms or (name_norm and source_norm == name_norm):
                    try:
                        if self.delete_series_alias_override(override.get("id")):
                            aliases_removed += 1
                    except Exception as exc:
                        logger.debug("Could not remove alias override during reset: %s", exc)

        releases_unpinned = 0
        releases_rekeyed = 0
        display_cleared = False

        # Read phase (contract v5 §5): collect + normalise the series' releases on
        # a plain read connection BEFORE the write transaction, so the SQLite write
        # lock is never held across an O(releases) Python scan.
        release_rows: List[Dict[str, Any]] = []
        if name_norm:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, title, series_name, series_pinned FROM releases
                    WHERE series_name IS NOT NULL AND trim(series_name) != '';
                """)
                release_rows = [
                    dict(release)
                    for release in cur.fetchall()
                    if normalize_text(release["series_name"]) == name_norm
                ]
        pinned_ids: List[int] = []
        if unpin_releases and release_rows:
            pinned_ids = [
                release["id"] for release in release_rows
                if _coerce_int(release.get("series_pinned")) == 1
            ]
        rekey_updates: List[Tuple[str, int]] = []
        if rekey_to_parsed and was_manual and release_rows:
            for release in release_rows:
                stored_name = _coerce_text(release.get("series_name"))
                parsed_name = self._parsed_release_series_name(release.get("title"))
                if not parsed_name:
                    continue
                if normalize_text(parsed_name) == normalize_text(stored_name):
                    continue
                if normalize_text(parsed_name) == name_norm:
                    continue
                rekey_updates.append((parsed_name, int(release["id"])))
            releases_rekeyed = len(rekey_updates)

        # Short write transaction: display/lock updates then the precomputed
        # unpin/re-key statements.
        with self.get_connection() as conn:
            cur = conn.cursor()
            if clear_display:
                cur.execute("""
                    UPDATE series_tracking SET
                        display_title = '', anilist_id = NULL, anilist_meta_json = '',
                        match_source = '', last_sync = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (sid,))
                display_cleared = True
            cur.execute(
                "UPDATE series_tracking SET identity_locked = 0, last_sync = CURRENT_TIMESTAMP WHERE id = ?;",
                (sid,),
            )
            if pinned_ids:
                cur.executemany("""
                    UPDATE releases SET series_pinned = 0, series_pinned_at = '',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, [(pinned_id,) for pinned_id in pinned_ids])
                releases_unpinned = len(pinned_ids)
            if rekey_updates:
                cur.executemany("""
                    UPDATE releases SET series_name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, rekey_updates)
            conn.commit()

        self.log_activity(
            "SERIES_IDENTITY_RESET",
            f"Reset identity state for series {name!r}",
            json.dumps({
                "series_id": sid,
                "series_name": name,
                "aliases_removed": aliases_removed,
                "releases_unpinned": releases_unpinned,
                "releases_rekeyed": releases_rekeyed,
                "display_cleared": display_cleared,
                "locked_cleared": bool(previous_locked),
                "rekey_to_parsed": bool(rekey_to_parsed),
                "actor": _coerce_text(actor),
            }),
        )
        return {
            "status": "success",
            "series_id": sid,
            "series_name": name,
            "aliases_removed": aliases_removed,
            "releases_unpinned": releases_unpinned,
            "releases_rekeyed": releases_rekeyed,
            "display_cleared": display_cleared,
            "locked_cleared": bool(previous_locked),
        }

    def get_series_identity_overview(self, search: Optional[str] = None,
                                     limit: int = 100) -> List[Dict[str, Any]]:
        """Per-tracked-series identity summary used by the Settings reset card."""
        from app.parser import normalize_text

        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            limit_int = 100
        limit_int = max(1, min(500, limit_int))

        alias_counts: Dict[str, int] = {}
        try:
            for override in self.get_series_alias_overrides():
                target_norm = _coerce_text(override.get("target_norm")) or normalize_text(
                    override.get("target_name")
                )
                if target_norm:
                    alias_counts[target_norm] = alias_counts.get(target_norm, 0) + 1
        except Exception:
            alias_counts = {}

        release_stats: Dict[str, Dict[str, int]] = {}
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT series_name, series_pinned FROM releases
                    WHERE series_name IS NOT NULL AND trim(series_name) != '';
                """)
                for rel in cur.fetchall():
                    rel_norm = normalize_text(rel["series_name"])
                    if not rel_norm:
                        continue
                    stats = release_stats.setdefault(rel_norm, {"releases": 0, "pinned": 0})
                    stats["releases"] += 1
                    if _coerce_int(rel["series_pinned"]) == 1:
                        stats["pinned"] += 1
        except Exception as exc:
            logger.debug("Series identity overview release scan failed: %s", exc)

        search_norm = normalize_text(search) if search else ""
        overview: List[Dict[str, Any]] = []
        for row in self.get_series_identity_rows():
            name = _coerce_text(row.get("name"))
            if not name:
                continue
            name_norm = normalize_text(name)
            display = _coerce_text(row.get("display_title"))
            authors = _coerce_text(row.get("authors"))
            if search_norm:
                haystacks = (
                    normalize_text(name),
                    normalize_text(display),
                    normalize_text(authors),
                )
                if not any(search_norm in haystack for haystack in haystacks if haystack):
                    continue
            stats = release_stats.get(name_norm, {"releases": 0, "pinned": 0})
            overview.append({
                "series_id": row.get("id"),
                "name": name,
                "display_title": display,
                "calibre_series_id": row.get("calibre_series_id"),
                "identity_locked": bool(_coerce_int(row.get("identity_locked"))),
                "match_source": _coerce_text(row.get("match_source")),
                "anilist_id": row.get("anilist_id"),
                "aliases_count": alias_counts.get(name_norm, 0),
                "pinned_releases": stats.get("pinned", 0),
                "releases_count": stats.get("releases", 0),
                "owned_volumes": _coerce_owned_volumes(row.get("owned_volumes_json")),
            })
            if len(overview) >= limit_int:
                break
        return overview

    def get_release_series_identity(self, release_id: int) -> Optional[Dict[str, Any]]:
        """Release + current tracked-series identity, for the re-bind modal."""
        rid = _coerce_int(release_id)
        if rid is None:
            return None
        release = self.get_release_by_id(rid)
        if release is None:
            return None
        series_name = _coerce_text(release.get("series_name"))
        tracking_series: Optional[Dict[str, Any]] = None
        if series_name:
            row = self.get_series_by_name(series_name)
            if row is None:
                row = self._get_series_identity_by_title(series_name)
            if row is not None:
                tracking_series = {
                    "name": _coerce_text(row.get("name")),
                    "display_title": _coerce_text(row.get("display_title")),
                    "calibre_series_id": row.get("calibre_series_id"),
                    "identity_locked": bool(_coerce_int(row.get("identity_locked"))),
                }
        return {
            "release_id": int(release.get("id") or rid),
            "title": release.get("title"),
            "series_name": series_name,
            "volume": release.get("volume"),
            "status": release.get("status"),
            "calibre_matched_book_id": release.get("calibre_matched_book_id"),
            "series_pinned": bool(_coerce_int(release.get("series_pinned"))),
            "tracking_series": tracking_series,
        }

    def unpin_release_series(self, release_id: int) -> bool:
        """Clear the explicit re-bind pin of one release (contract v2 §2.3)."""
        rid = _coerce_int(release_id)
        if rid is None:
            return False
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE releases SET series_pinned = 0, series_pinned_at = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND COALESCE(series_pinned, 0) = 1;
            """, (rid,))
            changed = max(0, int(cur.rowcount or 0))
            conn.commit()
        if not changed:
            return False
        self.log_activity(
            "RELEASE_SERIES_UNPINNED",
            f"Unpinned release #{rid} series re-bind",
            json.dumps({"release_id": rid}),
        )
        return True

    # --- Series <-> Calibre repair (v4 §3) ---
    def unlink_series_from_calibre(self, series_id: int, actor: str = "user") -> Dict[str, Any]:
        """Remove a (possibly false) Calibre link and reset its In-Calibre releases.

        Repairs the screenshot case where a series was linked to the wrong Calibre
        entry and therefore "looked" owned: the tracker's ``calibre_series_id``,
        ``owned_volumes_json`` and the link-derived ``book_count`` /
        ``latest_index`` are cleared and the series' releases move from
        ``IN_CALIBRE`` back to a sane lifecycle status (``REQUESTED`` for volume
        > 1, ``NEW`` for volume None/1.0) with ``calibre_matched_book_id`` cleared.
        ``IGNORED``/``BLACKLISTED`` rows are explicit user decisions and are left
        untouched.  ``name``, ``display_title``, ``anilist_meta_json``, alias
        overrides and ``identity_locked`` are preserved (Calibre linkage is a
        library fact, not identity state); a later Calibre sync restores the
        library-derived counts.

        Raises :class:`KeyError` for an unknown series id; an already-unlinked
        series is a safe no-op returning ``already_unlinked=True``.
        """
        from app.parser import normalize_text

        sid = _coerce_int(series_id)
        if sid is None:
            raise ValueError("series_id is required")
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM series_tracking WHERE id = ?;", (sid,))
            found = cur.fetchone()
        if found is None:
            raise KeyError(f"series {sid} not found")
        row = dict(found)
        name = _coerce_text(row.get("name"))
        name_norm = normalize_text(name)
        old_calibre_id = _coerce_int(row.get("calibre_series_id"))
        if old_calibre_id is None:
            # Already unlinked: do not touch ownership/statuses/activity.
            return {
                "status": "success",
                "series_id": sid,
                "series_name": name,
                "old_calibre_series_id": None,
                "releases_reset": 0,
                "already_unlinked": True,
            }

        # Read phase (contract v5 §5): collect + normalise the series' releases on
        # a plain read connection BEFORE any write transaction, so the SQLite
        # write lock is never held across an O(releases) Python scan.
        matched_releases: List[Dict[str, Any]] = []
        if name_norm:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, series_name, volume, status, calibre_matched_book_id
                    FROM releases
                    WHERE series_name IS NOT NULL AND trim(series_name) != '';
                """)
                matched_releases = [
                    dict(release)
                    for release in cur.fetchall()
                    if normalize_text(release["series_name"]) == name_norm
                ]
        releases_reset = 0
        release_updates: List[Tuple[str, int]] = []
        for release in matched_releases:
            status = _coerce_text(release.get("status")).upper()
            if status in ("IGNORED", "BLACKLISTED"):
                # Explicit user veto: leave status AND matched-book id alone.
                continue
            matched_book_id = release.get("calibre_matched_book_id")
            new_status = status or "NEW"
            if status == "IN_CALIBRE":
                try:
                    volume_value = (
                        float(release["volume"])
                        if release.get("volume") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    volume_value = None
                new_status = (
                    "REQUESTED"
                    if (volume_value is not None and volume_value > 1.0)
                    else "NEW"
                )
                releases_reset += 1
            if new_status != status or matched_book_id is not None:
                release_updates.append((new_status, int(release["id"])))

        # Short write transaction: tracker link/ownership clear + release resets.
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE series_tracking SET
                    calibre_series_id = NULL,
                    owned_volumes_json = '[]',
                    book_count = 0,
                    latest_index = 0.0,
                    last_sync = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (sid,))
            if release_updates:
                cur.executemany("""
                    UPDATE releases SET
                        status = ?,
                        calibre_matched_book_id = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, release_updates)
            conn.commit()

        self.log_activity(
            "SERIES_CALIBRE_UNLINKED",
            f"Unlinked series {name!r} from Calibre #{old_calibre_id}",
            json.dumps({
                "series_id": sid,
                "series_name": name,
                "old_calibre_series_id": old_calibre_id,
                "releases_reset": releases_reset,
                "actor": _coerce_text(actor),
            }),
        )
        return {
            "status": "success",
            "series_id": sid,
            "series_name": name,
            "old_calibre_series_id": old_calibre_id,
            "releases_reset": releases_reset,
            "already_unlinked": False,
        }

    def set_series_owned_volumes(self, series_id: int, volumes: List[float]) -> bool:
        """Store a tracker's owned volumes as sorted/deduped finite, non-negative JSON.

        Used by the Calibre recheck repair path to recompute ownership from the real
        library.  No other ``series_tracking`` column is touched.  Returns ``False``
        for an unknown series id (invalid entries inside *volumes* are dropped).
        """
        sid = _coerce_int(series_id)
        if sid is None:
            return False
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM series_tracking WHERE id = ?;", (sid,))
            if cur.fetchone() is None:
                return False
        cleaned: Set[float] = set()
        if isinstance(volumes, (list, tuple, set)):
            for value in volumes:
                if isinstance(value, bool):
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(number) or number < 0:
                    continue
                cleaned.add(number)
        owned_json = json.dumps(sorted(cleaned))
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE series_tracking SET
                    owned_volumes_json = ?,
                    last_sync = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (owned_json, sid))
            conn.commit()
        return True

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

            # --- Consolidated detection fields (groups, publisher, revision,
            # chapter data) — §12.11, §12.13, §12.13.2. The optional parser
            # helpers may not be deployed yet; every import degrades to the
            # legacy single-value paths so the DB keeps working either way.
            title_val = release_data.get("title") or ""
            explicit_primary = (release_data.get("release_group") or "").strip()
            groups = _coerce_group_list(release_data.get("release_groups"))
            if not groups and explicit_primary:
                groups = [explicit_primary]
            if not groups and title_val:
                derived_groups: List[str] = []
                try:
                    from app.parser import extract_release_groups
                    derived_groups = _coerce_group_list(extract_release_groups(title_val))
                except Exception:
                    derived_groups = []
                if not derived_groups:
                    try:
                        from app.parser import extract_release_group
                        single_group = extract_release_group(title_val)
                        derived_groups = [str(single_group).strip()] if single_group else []
                    except Exception:
                        derived_groups = []
                groups = [group for group in derived_groups if group]
            rel_group_val = groups[0] if groups else explicit_primary
            groups_val = json.dumps(groups)

            publisher_val = str(release_data.get("publisher") or "").strip()
            if not publisher_val and title_val:
                try:
                    from app.parser import extract_publisher
                    publisher_val = str(extract_publisher(title_val) or "").strip()
                except Exception:
                    publisher_val = ""

            revision_val = 0
            if release_data.get("revision") is not None:
                try:
                    revision_val = int(release_data.get("revision") or 0)
                except (TypeError, ValueError):
                    revision_val = 0
            elif title_val:
                try:
                    from app.parser import parse_revision_marker
                    revision_val = int((parse_revision_marker(title_val) or {}).get("revision") or 0)
                except Exception:
                    revision_val = 0

            chapter_number_val = release_data.get("chapter_number")
            chapter_ranges_val = release_data.get("chapter_ranges")
            if "chapter_number" not in release_data or chapter_ranges_val is None:
                parsed_detection: Dict[str, Any] = {}
                try:
                    from app.parser import extract_series_and_volume
                    parsed_detection = extract_series_and_volume(title_val) if title_val else {}
                except Exception:
                    parsed_detection = {}
                if "chapter_number" not in release_data:
                    chapter_number_val = parsed_detection.get("chapter_number")
                if chapter_ranges_val is None:
                    chapter_ranges_val = parsed_detection.get("chapter_ranges")
            if chapter_number_val is not None:
                try:
                    chapter_number_val = float(chapter_number_val)
                except (TypeError, ValueError):
                    chapter_number_val = None
            if not isinstance(chapter_ranges_val, list):
                chapter_ranges_val = []
            try:
                chapter_ranges_json_val = json.dumps(chapter_ranges_val)
            except (TypeError, ValueError):
                chapter_ranges_json_val = "[]"

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
                            media_type = CASE
                                WHEN COALESCE(?, 'unknown') != 'unknown' THEN ?
                                ELSE media_type
                            END,
                            release_group = COALESCE(NULLIF(?, ''), release_group),
                            release_groups_json = CASE WHEN ? != '[]' THEN ? ELSE release_groups_json END,
                            publisher = COALESCE(NULLIF(?, ''), publisher),
                            revision = MAX(COALESCE(revision, 0), ?),
                            chapter_number = COALESCE(?, chapter_number),
                            chapter_ranges_json = CASE WHEN ? != '[]' THEN ? ELSE chapter_ranges_json END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?;
                    """, (
                        release_data.get("description"),
                        release_data.get("cover_url"),
                        release_data.get("series_link"),
                        use_title,
                        release_data.get("media_type") or "unknown",
                        release_data.get("media_type") or "unknown",
                        rel_group_val,
                        groups_val,
                        groups_val,
                        publisher_val,
                        revision_val,
                        chapter_number_val,
                        chapter_ranges_json_val,
                        chapter_ranges_json_val,
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

            dl_url_val = release_data.get("download_url") or release_data.get("link") or ""
            status_supplied = (
                ("status" in release_data and release_data.get("status") is not None)
                or bool(release_data.get("ignored"))
            )
            status_in = (release_data.get("status") or "REQUESTED").strip().upper()
            is_ignored = (status_in == "IGNORED") or bool(release_data.get("ignored"))
            ignored_val = 1 if is_ignored else 0
            status_val = "IGNORED" if is_ignored else status_in
            # A blacklisted series may never be stored as pending/discoverable.
            if blacklisted and status_val in ("NEW", "REQUESTED"):
                status_val = "BLACKLISTED"

            cur.execute("SELECT id, status, ignored, series_name, series_pinned FROM releases WHERE guid = ?;", (guid,))
            existing = cur.fetchone()

            if existing:
                rel_id = existing["id"]
                # Re-ingest updates the payload only. When the caller supplies a
                # lifecycle status it is resolved through the §12.6 precedence
                # rules, so a feed scan can recompute NEW/DISCOVERY but can never
                # resurrect or downgrade a user decision (REQUESTED / IN_CALIBRE /
                # IGNORED / BLACKLISTED). Payload-only upserts keep the status.
                if status_supplied:
                    resolved_status = resolve_status(existing["status"], status_val)
                    resolved_ignored = 1 if resolved_status == "IGNORED" else 0
                else:
                    resolved_status = existing["status"]
                    resolved_ignored = existing["ignored"] if existing["ignored"] is not None else 0
                # §12.1: a metadata/provider title must never rewrite the stored
                # stable series key once it exists.  Contract v2 §2.4: a release the
                # user explicitly re-bound (series_pinned=1) keeps its series_name
                # verbatim through every re-ingestion.
                if _coerce_int(existing["series_pinned"]) == 1:
                    stable_series_val = _coerce_text(existing["series_name"]) or release_data.get("series_name")
                else:
                    stable_series_val = self._stable_series_name(
                        existing["series_name"], release_data.get("series_name")
                    )
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
                        release_groups_json = CASE WHEN ? != '[]' THEN ? ELSE release_groups_json END,
                        publisher = COALESCE(NULLIF(?, ''), publisher),
                        revision = MAX(COALESCE(revision, 0), ?),
                        chapter_number = COALESCE(?, chapter_number),
                        chapter_ranges_json = CASE WHEN ? != '[]' THEN ? ELSE chapter_ranges_json END,
                        media_type = CASE WHEN COALESCE(?, 'unknown') != 'unknown' THEN ? ELSE media_type END,
                        status = ?,
                        ignored = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (
                    release_data["title"],
                    stable_series_val,
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
                    groups_val,
                    groups_val,
                    publisher_val,
                    revision_val,
                    chapter_number_val,
                    chapter_ranges_json_val,
                    chapter_ranges_json_val,
                    release_data.get("media_type") or "unknown",
                    release_data.get("media_type") or "unknown",
                    resolved_status,
                    resolved_ignored,
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
                        download_url, release_group, ignored, media_type,
                        release_groups_json, publisher, revision, chapter_number, chapter_ranges_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                    ignored_val,
                    release_data.get("media_type") or "unknown",
                    groups_val,
                    publisher_val,
                    revision_val,
                    chapter_number_val,
                    chapter_ranges_json_val
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

    def update_release_status(
        self,
        release_id: int,
        status: str,
        matched_book_id: Optional[int] = None,
        enforce_precedence: bool = False,
    ):
        """Updates a release status.

        The default is the deliberate UI/API contract: the supplied status is
        written verbatim (an explicit action may lift a user veto). Automated
        callers pass ``enforce_precedence=True`` to route the write through the
        §12.6 precedence rules instead.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            if enforce_precedence:
                cur.execute("SELECT status FROM releases WHERE id = ?;", (release_id,))
                current = cur.fetchone()
                clean_status = resolve_status(
                    current["status"] if current else "",
                    status,
                    enforce_precedence=True,
                )
            else:
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
                       (SELECT count(*) FROM releases r WHERE
                           EXISTS (
                               SELECT 1 FROM json_each(
                                   CASE WHEN json_valid(r.release_groups_json)
                                        THEN r.release_groups_json ELSE '[]' END
                               ) AS jg
                               WHERE lower(trim(CAST(jg.value AS TEXT))) = lower(trim(pg.name))
                           )
                           OR (
                               CASE WHEN json_valid(r.release_groups_json)
                                    THEN json_array_length(r.release_groups_json) ELSE 0 END = 0
                               AND lower(trim(COALESCE(r.release_group, ''))) = lower(trim(pg.name))
                           )
                       ) as release_count
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
                _PRIORITIZED_GROUP_MATCH_SQL
            ]
            params: List[Any] = []
            if status == "IGNORED":
                where_clauses.append("(r.status = 'IGNORED' OR r.ignored = 1)")
            elif status:
                where_clauses.append("r.status = ? AND (r.ignored = 0 OR r.ignored IS NULL)")
                params.append(status)
            else:
                where_clauses.append("r.status NOT IN ('BLACKLISTED', 'IGNORED') AND (r.ignored = 0 OR r.ignored IS NULL) AND COALESCE(r.media_type, 'unknown') NOT IN ('audiobook', 'manga')")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(r.title) LIKE ? OR lower(COALESCE(r.series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            # Audiobooks live in their own section and never enter the tracking views.
            where_clauses.append("COALESCE(r.media_type, 'unknown') NOT IN ('audiobook', 'manga')")
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
                self._attach_detection_fields(item)
                item["download_state"] = release_download_state(item)
            return items

    def count_prioritized_releases(self, status: Optional[str] = None, search: Optional[str] = None) -> int:
        """Returns total count of releases matching prioritized groups."""
        self.ensure_prioritized_groups_table()
        with self.get_connection() as conn:
            cur = conn.cursor()
            where_clauses = [
                _PRIORITIZED_GROUP_MATCH_SQL
            ]
            params: List[Any] = []
            if status == "IGNORED":
                where_clauses.append("(r.status = 'IGNORED' OR r.ignored = 1)")
            elif status:
                where_clauses.append("r.status = ? AND (r.ignored = 0 OR r.ignored IS NULL)")
                params.append(status)
            else:
                where_clauses.append("r.status NOT IN ('BLACKLISTED', 'IGNORED') AND (r.ignored = 0 OR r.ignored IS NULL) AND COALESCE(r.media_type, 'unknown') NOT IN ('audiobook', 'manga')")

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

    # --- Download engine bookkeeping (TorBox port §3.2) ----------------------
    def update_download_handle(
        self,
        release_id: int,
        engine: str,
        downloader_id: Optional[str] = None,
        torrent_hash: Optional[str] = None,
        wanted_volumes: Optional[List[float]] = None,
    ) -> None:
        """Persist the engine provenance and effective wanted volumes for a release."""
        clean_engine = str(engine or "").strip().lower()
        clean_id = None if downloader_id is None else str(downloader_id).strip()
        clean_hash = str(torrent_hash).strip() if (
            isinstance(torrent_hash, str) and torrent_hash.strip()
        ) else None
        clean_wanted = None
        if wanted_volumes is not None:
            clean_wanted = json.dumps(sorted({round(float(v), 2) for v in wanted_volumes}))
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE releases
                SET download_status = 'DOWNLOADING',
                    downloader = ?,
                    downloader_id = COALESCE(?, downloader_id),
                    torrent_hash = COALESCE(?, torrent_hash),
                    download_wanted_json = COALESCE(?, download_wanted_json),
                    download_error = '',
                    download_attempts = 0,
                    downloader_started_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (clean_engine, clean_id, clean_hash, clean_wanted, release_id))
            conn.commit()

    def update_downloader_state(
        self, release_id: int, download_status: str, engine: Optional[str] = None
    ) -> None:
        """Only mutate download_status (and optionally the engine) — not hashes."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            if engine:
                cur.execute(
                    "UPDATE releases SET download_status = ?, downloader = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                    (download_status, str(engine).strip().lower(), release_id),
                )
            else:
                cur.execute(
                    "UPDATE releases SET download_status = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?;",
                    (download_status, release_id),
                )
            conn.commit()

    def set_downloader_id(self, release_id: int, downloader_id: str) -> None:
        """Replace the engine handle without resetting the submission timestamp.

        Used when a queued TorBox job is promoted to an active torrent; the overall
        timeout window must keep counting from the original submission.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET downloader_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?;",
                (str(downloader_id or ""), release_id),
            )
            conn.commit()

    def set_download_files(self, release_id: int, paths: List[str]) -> None:
        """Persist the files fetched for a release and mark when they arrived."""
        clean = [str(path) for path in (paths or [])]
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET download_files_json = ?, "
                "downloaded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (json.dumps(clean), release_id),
            )
            conn.commit()

    def update_torbox_progress(self, release_id: int, **fields: Any) -> Dict[str, Any]:
        """Merge the TorBox phase/progress/timing fields into the release row."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT torbox_progress_json FROM releases WHERE id = ?;", (release_id,))
            row = cur.fetchone()
            try:
                data = json.loads((row["torbox_progress_json"] if row else "") or "{}")
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.update({key: value for key, value in fields.items() if value is not None})
            data["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                "UPDATE releases SET torbox_progress_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?;",
                (json.dumps(data), release_id),
            )
            conn.commit()
            return data

    def get_torbox_progress(self, release_id: int) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT torbox_progress_json FROM releases WHERE id = ?;", (release_id,))
            row = cur.fetchone()
        try:
            data = json.loads((row["torbox_progress_json"] if row else "") or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def increment_download_attempt(self, release_id: int) -> int:
        """Bump the failure attempt counter and return the new value."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET download_attempts = COALESCE(download_attempts, 0) + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (release_id,),
            )
            conn.commit()
            cur.execute(
                "SELECT COALESCE(download_attempts, 0) AS attempts FROM releases WHERE id = ?;",
                (release_id,),
            )
            row = cur.fetchone()
            return int(row["attempts"]) if row else 0

    def set_download_error(self, release_id: int, message: str) -> None:
        """Persist a user-visible failure reason (attempt count is bumped separately)."""
        clean = str(message or "").strip()
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET download_error = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?;",
                (clean, release_id),
            )
            conn.commit()

    def clear_download_error(self, release_id: int) -> None:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET download_error = '', download_attempts = 0, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (release_id,),
            )
            conn.commit()

    def clear_downloader_submission(self, release_id: int, engine: Optional[str] = None) -> None:
        """Fully detach a release from a downloader queue (user "Remove" action).

        Clearing only ``download_status`` leaves the engine handle, torrent hash,
        fetched progress and error behind; the row then still looks like engine work
        to retries/status and can be re-rendered as a ghost queue item.  Reset every
        submission field so the release is cleanly back to "not queued" while keeping
        the release itself (and any files already fetched on disk).
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE releases SET "
                "download_status = 'IDLE', download_error = '', download_attempts = 0, "
                "downloader_id = NULL, torrent_hash = NULL, downloader_started_at = NULL, "
                "download_wanted_json = '[]', torbox_progress_json = '{}', "
                "download_files_json = '[]', downloaded_at = NULL, "
                + ("downloader = ?, " if engine else "")
                + "updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                ((str(engine).strip().lower(), release_id) if engine else (release_id,)),
            )
            conn.commit()

    def get_active_torbox_downloads(self) -> List[Dict[str, Any]]:
        """Releases submitted through TorBox that are still DOWNLOADING."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM releases WHERE download_status = 'DOWNLOADING' "
                "AND downloader = 'torbox' ORDER BY downloader_started_at ASC, id ASC;"
            )
            return [dict(row) for row in cur.fetchall()]

    def get_downloader_releases(self, engine: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Releases currently representing work for *engine* (active first)."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM releases
                WHERE downloader = ?
                  AND download_status IN ('DOWNLOADING', 'FAILED')
                ORDER BY CASE download_status WHEN 'DOWNLOADING' THEN 0 ELSE 1 END,
                         updated_at DESC
                LIMIT ?;
                """,
                (str(engine or "").strip().lower(), max(1, int(limit))),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_release_by_id(self, release_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM releases WHERE id = ?;", (release_id,))
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            self._attach_detection_fields(item)
            item["download_state"] = release_download_state(item)
            return item

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
                    where_clauses.append("status NOT IN ('BLACKLISTED', 'IGNORED') AND (ignored = 0 OR ignored IS NULL) AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')")
                else:
                    where_clauses.append("status != 'BLACKLISTED' AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(title) LIKE ? OR lower(COALESCE(series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            # Audiobooks live in their own section and never enter the tracking views.
            where_clauses.append("COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')")
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
                self._attach_detection_fields(item)
                group_keys = {group.strip().lower() for group in item["release_groups"] if group}
                item["is_prioritized"] = bool(group_keys & p_groups)
                item["ignored"] = bool(item.get("ignored", 0) or item.get("status") == "IGNORED")
                try:
                    item["volumes"] = json.loads(item.get("volumes_json") or "[]")
                except Exception:
                    item["volumes"] = [item["volume"]] if item.get("volume") is not None else []
                try:
                    item["missing_volumes"] = json.loads(item.get("missing_vols_json") or "[]")
                except Exception:
                    item["missing_volumes"] = []
                item["download_state"] = release_download_state(item)
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
                    where_clauses.append("status NOT IN ('BLACKLISTED', 'IGNORED') AND (ignored = 0 OR ignored IS NULL) AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')")
                else:
                    where_clauses.append("status != 'BLACKLISTED' AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')")

            if search and search.strip():
                clean_s = f"%{search.strip().lower()}%"
                where_clauses.append("(lower(title) LIKE ? OR lower(COALESCE(series_name, '')) LIKE ?)")
                params.extend([clean_s, clean_s])

            # Audiobooks live in their own section and never enter the tracking views.
            where_clauses.append("COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')")
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
                    WHERE (status = 'IGNORED' OR ignored = 1) AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')
                    ORDER BY updated_at DESC, id DESC;
                """)
            elif status:
                cur.execute("""
                    SELECT * FROM releases 
                    WHERE status = ? AND (ignored = 0 OR ignored IS NULL) AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')
                    ORDER BY updated_at DESC, id DESC;
                """, (status,))
            else:
                if not include_ignored:
                    cur.execute("""
                        SELECT * FROM releases 
                        WHERE status NOT IN ('BLACKLISTED', 'IGNORED') AND (ignored = 0 OR ignored IS NULL) AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')
                        ORDER BY 
                            CASE status WHEN 'REQUESTED' THEN 1 WHEN 'NEW' THEN 2 WHEN 'IN_CALIBRE' THEN 3 ELSE 4 END,
                            updated_at DESC, id DESC;
                    """)
                else:
                    cur.execute("""
                        SELECT * FROM releases 
                        WHERE status != 'BLACKLISTED' AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')
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
                self._attach_detection_fields(r)
                group_keys = {group.strip().lower() for group in r["release_groups"] if group}
                r["is_prioritized"] = bool(group_keys & p_groups)
                r["ignored"] = bool(r.get("ignored", 0) or r.get("status") == "IGNORED")
                try:
                    r["volumes"] = json.loads(r.get("volumes_json") or "[]")
                except Exception:
                    r["volumes"] = [r["volume"]] if r.get("volume") is not None else []
                try:
                    r["missing_volumes"] = json.loads(r.get("missing_vols_json") or "[]")
                except Exception:
                    r["missing_volumes"] = []
                r["download_state"] = release_download_state(r)

            cur.execute("SELECT * FROM series_tracking;")
            tracked_rows = [dict(r) for r in cur.fetchall()]
            try:
                cur.execute(
                    "SELECT source_norm, source_name, target_name, target_norm, calibre_series_id "
                    "FROM series_alias_overrides;"
                )
                alias_overrides = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                alias_overrides = []
            # Identity-aware index: the stable name, display title, stored AniList
            # titles/synonyms and alias-override sources all resolve a release row
            # to its tracked series.  Grouping always keys off the STABLE name.
            tracked_by_norm: Dict[str, Dict[str, Any]] = {}
            alias_source_to_target: Dict[str, str] = {}
            from app.series_identity import anilist_title_list, clean_series_for_matching
            for t_row in tracked_rows:
                t_name = _coerce_text(t_row.get("name"))
                if not t_name:
                    continue
                variants: List[str] = [t_name]
                variants.append(_coerce_text(t_row.get("display_title")))
                meta_raw = t_row.get("anilist_meta_json")
                meta: Dict[str, Any] = {}
                if isinstance(meta_raw, dict):
                    meta = meta_raw
                elif isinstance(meta_raw, str) and meta_raw.strip():
                    try:
                        parsed = json.loads(meta_raw)
                        meta = parsed if isinstance(parsed, dict) else {}
                    except (TypeError, ValueError):
                        meta = {}
                variants.extend(anilist_title_list(meta))
                try:
                    variants.append(clean_series_for_matching(t_name))
                except Exception:
                    pass
                for value in variants:
                    if not value:
                        continue
                    norm = normalize_text(value)
                    if norm and norm not in tracked_by_norm:
                        tracked_by_norm[norm] = t_row
                # Component sub-titles for pipe/slash separated titles.
                if "|" in t_name or "/" in t_name:
                    for sub in re.split(r'\s*[|/]\s*', t_name):
                        sub_norm = normalize_text(sub)
                        if len(sub_norm) >= 3 and sub_norm not in tracked_by_norm:
                            tracked_by_norm[sub_norm] = t_row
            for override in alias_overrides:
                source_norm = _coerce_text(override.get("source_norm")) or normalize_text(
                    override.get("source_name")
                )
                target_name = _coerce_text(override.get("target_name"))
                if not source_norm or not target_name:
                    continue
                alias_source_to_target[source_norm] = target_name
                if source_norm not in tracked_by_norm:
                    target_row = tracked_by_norm.get(normalize_text(target_name))
                    if target_row is not None:
                        tracked_by_norm[source_norm] = target_row

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
                raw_norm = normalize_text(s_name)
                rel_link = (rel.get("series_link") or "").strip().rstrip("/")

                # Identity-aware resolution: aliases, display and AniList titles
                # all resolve to their tracked row; grouping keys off the STABLE
                # name so a display spelling can never split the group.
                calibre_match = tracked_by_norm.get(raw_norm) or tracked_by_norm.get(s_key)
                if calibre_match is None:
                    alias_target = (
                        alias_source_to_target.get(raw_norm)
                        or alias_source_to_target.get(s_key)
                    )
                    if alias_target:
                        calibre_match = tracked_by_norm.get(normalize_text(alias_target))
                if calibre_match is None and ("|" in s_name or "/" in s_name):
                    for sub in re.split(r'\s*[|/]\s*', s_name):
                        sub_norm = normalize_text(sub)
                        if sub_norm in tracked_by_norm:
                            calibre_match = tracked_by_norm[sub_norm]
                            break

                if calibre_match is not None:
                    stable_title = _coerce_text(calibre_match.get("name")) or canonical_s_name
                    display_title_value = (
                        _coerce_text(calibre_match.get("display_title"))
                        or canonical_s_name
                        or stable_title
                    )
                else:
                    stable_title = canonical_s_name
                    display_title_value = canonical_s_name or s_name
                group_key = normalize_text(stable_title) or s_key

                # Instant O(1) group key resolution without quadratic fuzzy scanning
                matched_key = None
                if group_key in groups_dict:
                    matched_key = group_key
                elif s_key in groups_dict:
                    matched_key = s_key
                elif rel_link and rel_link in link_to_group_key:
                    matched_key = link_to_group_key[rel_link]
                elif "|" in s_name or "/" in s_name:
                    for sub in re.split(r'\s*[|/]\s*', s_name):
                        sub_key = normalize_text(sub)
                        if sub_key in groups_dict:
                            matched_key = sub_key
                            break

                if not matched_key:
                    matched_key = group_key
                    cover = rel.get("cover_url") or (calibre_match.get("cover_url") if calibre_match else "") or ""
                    synopsis = rel.get("description") or (calibre_match.get("description") if calibre_match else "") or ""
                    website_url = rel.get("series_link") or (calibre_match.get("website_url") if calibre_match else "") or ""
                    # "In Calibre" means the tracker is actually LINKED to a
                    # Calibre series (or carries explicitly owned volumes); a
                    # tracked-but-unlinked series must not claim ownership after
                    # an explicit unlink (contract v4).
                    calibre_id_value = (
                        _coerce_int(calibre_match.get("calibre_series_id"))
                        if calibre_match is not None else None
                    )
                    calibre_owned_volumes = (
                        _coerce_owned_volumes(calibre_match.get("owned_volumes_json"))
                        if calibre_match is not None else []
                    )
                    calibre_linked = calibre_id_value is not None
                    in_calibre_flag = bool(calibre_linked or calibre_owned_volumes)

                    groups_dict[matched_key] = {
                        "series_key": matched_key,
                        # `series_name`/`display_name` are the cosmetic display
                        # title (AniList when known); `stable_name`/`series_key`
                        # are the STABLE matching key used for grouping (§12.1).
                        "series_name": display_title_value,
                        "display_name": display_title_value,
                        "display_title": display_title_value,
                        "stable_name": stable_title,
                        "raw_name": s_name,
                        "cover_url": cover,
                        "in_calibre": in_calibre_flag,
                        "is_monitored": bool(calibre_match.get("is_monitored", 0)) if calibre_match else False,
                        "calibre_series_id": calibre_id_value,
                        "calibre_book_count": calibre_match.get("book_count", 0) if calibre_linked else 0,
                        "calibre_latest_index": calibre_match.get("latest_index", 0.0) if calibre_linked else 0.0,
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

            cur.execute("SELECT count(*) FROM releases WHERE status = 'REQUESTED' AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga');")
            pending_requests = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM releases WHERE status = 'IN_CALIBRE' AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga');")
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
                  AND COALESCE(r.media_type, 'unknown') NOT IN ('audiobook', 'manga')
                      AND COALESCE(lower(r.title), '') NOT LIKE '%pdf%'
                      AND COALESCE(lower(r.link), '') NOT LIKE '%.pdf%'
                      AND COALESCE(lower(r.guid), '') NOT LIKE '%pdf%'
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (
                          SELECT lower(trim(sao.source_name))
                          FROM series_alias_overrides sao
                          JOIN series_tracking st ON lower(trim(st.name)) = lower(trim(sao.target_name))
                      ))
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
                self._attach_detection_fields(d)
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
                  AND COALESCE(r.media_type, 'unknown') NOT IN ('audiobook', 'manga')
                      AND COALESCE(lower(r.title), '') NOT LIKE '%pdf%'
                      AND COALESCE(lower(r.link), '') NOT LIKE '%.pdf%'
                      AND COALESCE(lower(r.guid), '') NOT LIKE '%pdf%'
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking))
                      AND (r.series_name IS NULL OR lower(trim(r.series_name)) NOT IN (
                          SELECT lower(trim(sao.source_name))
                          FROM series_alias_overrides sao
                          JOIN series_tracking st ON lower(trim(st.name)) = lower(trim(sao.target_name))
                      ))
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

            # 3. Re-parse releases with volume IS NULL or status = 'NEW'.
            #    Pinned releases are skipped: the user's re-bind owns their series.
            cur.execute("""
                SELECT id, title, series_name, volume 
                FROM releases 
                WHERE (volume IS NULL OR status = 'NEW')
                  AND COALESCE(series_pinned, 0) = 0;
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
                WHERE (series_name LIKE '%(Light Novel)%' 
                   OR series_name LIKE '%[Light Novel]%'
                   OR series_name LIKE '%(Novel)%'
                   OR series_name LIKE '%(Web Novel)%'
                   OR series_name LIKE '%(LN)%'
                   OR title LIKE '%(Light Novel)%'
                   OR title LIKE '%[Light Novel]%'
                   OR title LIKE '%(Novel)%'
                   OR title LIKE '%(Web Novel)%'
                   OR title LIKE '%(LN)%')
                  AND COALESCE(series_pinned, 0) = 0;
            """)

            # 5. Alias -> canonical for display, but never overwrite a tracked
            #    stable matching key (§12.1): series_tracking holds the name the
            #    Calibre library / user uses, and re-keying it would break matching.
            #    Pinned releases are excluded: an explicit user re-bind wins.
            cur.execute("SELECT name FROM series_tracking;")
            tracked_norms = {
                normalize_text(row["name"])
                for row in cur.fetchall()
                if row["name"]
            }
            cur.execute("""
                SELECT id, series_name FROM releases
                WHERE series_name IS NOT NULL AND trim(series_name) != ''
                  AND COALESCE(series_pinned, 0) = 0;
            """)
            for row in cur.fetchall():
                current_name = row["series_name"].strip()
                if normalize_text(current_name) in tracked_norms:
                    continue
                canonical_name = get_canonical_series_name(current_name)
                if canonical_name and canonical_name != current_name:
                    cur.execute(
                        "UPDATE releases SET series_name = ? WHERE id = ?;",
                        (canonical_name, row["id"]),
                    )

            # 6. Alias & series_link fast O(N) deduplication for 'NEW' status discovery releases
            cur.execute("""
                SELECT id, title, series_name, volume, description, cover_url, series_link, updated_at
                FROM releases
                WHERE status = 'NEW' AND (volume IS NULL OR volume = 1.0) AND series_name IS NOT NULL AND series_name != ''
                  AND COALESCE(series_pinned, 0) = 0
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
            series_val = series_name
            if series_name is not None:
                # M1 fix (§12.1 + contract §2.1): a provider's canonical English /
                # AniList title is display data, not identity.  The stored
                # ``releases.series_name`` is the STABLE matching key and must not
                # be re-keyed to a display spelling while the same tracked series
                # is already known under the stored name.  A genuinely different
                # series name supplied explicitly is still written.  Contract v2
                # §2.4: an explicitly re-bound (pinned) release is never moved.
                cur.execute("SELECT series_name, series_pinned FROM releases WHERE id = ?;", (release_id,))
                existing_row = cur.fetchone()
                existing_name = (existing_row["series_name"] or "").strip() if existing_row else ""
                if existing_row is not None and _coerce_int(existing_row["series_pinned"]) == 1:
                    series_val = existing_name
                else:
                    series_val = self._resolve_updated_series_name(existing_name, series_name)
            cur.execute("""
                UPDATE releases 
                SET description = COALESCE(?, description),
                    series_link = COALESCE(?, series_link),
                    cover_url = COALESCE(?, cover_url),
                    title = COALESCE(?, title),
                    series_name = COALESCE(?, series_name),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (description, series_link, cover_url, title, series_val, release_id))
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
        non-blacklisted series, do not resolve (by stable name OR alias override) to
        a tracked series, and are not part of a monitored tracked series. Pending
        wishlist requests (status='REQUESTED') are never touched.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM releases
                WHERE (volume IS NULL OR volume = 1.0)
                  AND status NOT IN ('IN_CALIBRE', 'BLACKLISTED', 'IGNORED', 'REQUESTED')
                  AND COALESCE(media_type, 'unknown') NOT IN ('audiobook', 'manga')
                  AND COALESCE(lower(title), '') NOT LIKE '%pdf%'
                  AND COALESCE(lower(link), '') NOT LIKE '%.pdf%'
                  AND COALESCE(lower(guid), '') NOT LIKE '%pdf%'
                  AND (series_name IS NULL OR lower(trim(series_name)) NOT IN (SELECT lower(trim(name)) FROM series_blacklist))
                  AND (series_name IS NULL OR lower(trim(series_name)) NOT IN (SELECT lower(trim(name)) FROM series_tracking))
                  AND (series_name IS NULL OR lower(trim(series_name)) NOT IN (
                      SELECT lower(trim(sao.source_name))
                      FROM series_alias_overrides sao
                      JOIN series_tracking st ON lower(trim(st.name)) = lower(trim(sao.target_name))
                  ))
                  AND COALESCE(series_pinned, 0) = 0;
            """)
            count = cur.rowcount
            conn.commit()
            self.log_activity("DEBUG_CLEAR", "Cleared discovered series", f"Removed {count} unreviewed discovery releases.")
            return count

    # --- Maintenance: activity log retention, DB stats/vacuum/backup, attribution ---
    def prune_activity_logs(self, retention_days: int = 90,
                            max_rows: int = 50000) -> Dict[str, Any]:
        """Prune ``activity_logs`` by age and by a newest-N row cap (v2 §6).

        Both rules are clamped and ``0`` disables that rule.  Runs in a single
        connection/commit and returns
        ``{"removed_by_age","removed_by_rows","removed","remaining"}``.
        """
        try:
            days = int(retention_days)
        except (TypeError, ValueError):
            days = 90
        try:
            row_cap = int(max_rows)
        except (TypeError, ValueError):
            row_cap = 50000
        days = max(0, min(3650, days))
        row_cap = max(0, min(1000000, row_cap))
        removed_by_age = 0
        removed_by_rows = 0
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                if days > 0:
                    # created_at is written by CURRENT_TIMESTAMP (UTC text), so a
                    # SQLite datetime modifier compares consistently.
                    cur.execute(
                        "DELETE FROM activity_logs WHERE created_at < datetime('now', ?);",
                        (f"-{days} days",),
                    )
                    removed_by_age = max(0, int(cur.rowcount or 0))
                if row_cap > 0:
                    cur.execute("SELECT COUNT(*) AS count FROM activity_logs;")
                    row = cur.fetchone()
                    remaining_rows = int(row["count"] or 0) if row else 0
                    if remaining_rows > row_cap:
                        excess = remaining_rows - row_cap
                        cur.execute("""
                            DELETE FROM activity_logs WHERE id IN (
                                SELECT id FROM activity_logs ORDER BY id ASC LIMIT ?
                            );
                        """, (excess,))
                        removed_by_rows = max(0, int(cur.rowcount or 0))
                cur.execute("SELECT COUNT(*) AS count FROM activity_logs;")
                row = cur.fetchone()
                remaining = int(row["count"] or 0) if row else 0
                conn.commit()
        except Exception as exc:
            logger.warning("Activity log prune failed: %s", exc)
            return {
                "removed_by_age": 0,
                "removed_by_rows": 0,
                "removed": 0,
                "remaining": 0,
            }
        return {
            "removed_by_age": removed_by_age,
            "removed_by_rows": removed_by_rows,
            "removed": removed_by_age + removed_by_rows,
            "remaining": remaining,
        }

    def database_stats(self) -> Dict[str, Any]:
        """Size/page/row-count snapshot of the SQLite database (never raises)."""
        stats: Dict[str, Any] = {
            "path": self.db_path,
            "size_bytes": 0,
            "wal_size_bytes": 0,
            "page_count": 0,
            "page_size": 0,
            "freelist_count": 0,
            "row_counts": {},
        }
        try:
            stats["size_bytes"] = int(os.path.getsize(self.db_path))
        except OSError:
            stats["size_bytes"] = 0
        for wal_candidate in (self.db_path + "-wal", self.db_path + ".wal"):
            try:
                stats["wal_size_bytes"] = int(os.path.getsize(wal_candidate))
                break
            except OSError:
                continue
        try:
            with self.get_connection() as conn:
                cur = conn.cursor()
                for key, pragma in (
                    ("page_count", "PRAGMA page_count;"),
                    ("page_size", "PRAGMA page_size;"),
                    ("freelist_count", "PRAGMA freelist_count;"),
                ):
                    try:
                        cur.execute(pragma)
                        row = cur.fetchone()
                        stats[key] = int(row[0] or 0) if row is not None else 0
                    except Exception:
                        stats[key] = 0
                row_counts: Dict[str, int] = {}
                for table in (
                    "releases",
                    "series_tracking",
                    "activity_logs",
                    "novel_metadata_cache",
                    "feeds",
                    "series_alias_overrides",
                ):
                    try:
                        cur.execute(f"SELECT COUNT(*) AS count FROM {table};")
                        row = cur.fetchone()
                        row_counts[table] = int(row["count"] or 0) if row is not None else 0
                    except Exception:
                        row_counts[table] = 0
                stats["row_counts"] = row_counts
        except Exception as exc:
            logger.debug("Database stats read failed: %s", exc)
        # Convenience aliases for route payloads (kept additive).
        stats["db_size_bytes"] = stats["size_bytes"]
        stats["wal_bytes"] = stats["wal_size_bytes"]
        stats["counts"] = dict(stats["row_counts"])
        return stats

    def vacuum_database(self) -> Dict[str, Any]:
        """``integrity_check`` + ``VACUUM`` + ``ANALYZE`` (guarded, never raises)."""
        before_bytes = 0
        try:
            before_bytes = int(os.path.getsize(self.db_path))
        except OSError:
            before_bytes = 0
        result: Dict[str, Any] = {
            "ok": False,
            "integrity": "",
            "before_bytes": before_bytes,
            "after_bytes": before_bytes,
            "message": "",
        }
        try:
            # A plain connection (not the request/with-helper context) so the
            # connection can be closed explicitly after maintenance.
            conn = self.get_connection()
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                row = cur.fetchone()
                result["integrity"] = _coerce_text(row[0] if row is not None else "")
                conn.commit()
                cur.execute("VACUUM;")
                conn.commit()
                cur.execute("ANALYZE;")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Database vacuum failed: %s", exc)
            result["message"] = f"Database maintenance failed: {exc}"
            try:
                result["after_bytes"] = int(os.path.getsize(self.db_path))
            except OSError:
                pass
            return result
        try:
            result["after_bytes"] = int(os.path.getsize(self.db_path))
        except OSError:
            pass
        integrity = result["integrity"].lower() or ""
        result["ok"] = integrity == "ok" or integrity == ""
        if result["ok"]:
            result["message"] = "Database optimized (integrity check: ok)."
        else:
            result["message"] = f"Integrity check reported: {result['integrity']}"
        return result

    def backup_database(self, dest_path: str) -> str:
        """Write a consistent SQLite snapshot of this database to *dest_path*.

        Uses the sqlite3 online backup API from a fresh connection so WAL readers
        are not disturbed.  Returns the destination path; OSError/sqlite3.Error
        propagate to the caller (the route maps them to a 500 JSON response).
        """
        destination = _coerce_text(dest_path)
        if not destination:
            raise ValueError("destination path is required")
        destination = os.path.abspath(destination)
        destination_dir = os.path.dirname(destination)
        if destination_dir:
            os.makedirs(destination_dir, exist_ok=True)
        source_conn = self.get_connection()
        try:
            destination_conn = sqlite3.connect(destination)
            try:
                source_conn.backup(destination_conn)
                destination_conn.commit()
            finally:
                destination_conn.close()
        finally:
            source_conn.close()
        return destination

    def get_series_attribution_rows(self, scan_limit: int = 50000
                                    ) -> List[Dict[str, Any]]:
        """Read-only release rows for the series-attribution diagnostics audit.

        Attribution resolution itself lives in ``app.diagnostics`` (owner C); this
        helper is persistence-only.  Rows that are IGNORED/BLACKLISTED, explicitly
        ignored or user-pinned are excluded.
        """
        try:
            limit_int = int(scan_limit)
        except (TypeError, ValueError):
            limit_int = 50000
        limit_int = max(1, min(200000, limit_int))
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, series_name, volume, status, series_pinned,
                       ignored, media_type
                FROM releases
                WHERE COALESCE(status, '') NOT IN ('IGNORED', 'BLACKLISTED')
                  AND COALESCE(ignored, 0) = 0
                  AND COALESCE(series_pinned, 0) = 0
                ORDER BY id ASC
                LIMIT ?;
            """, (limit_int,))
            return [dict(row) for row in cur.fetchall()]

    # --- Novel Metadata Cache Operations ---
    def ensure_metadata_cache_table(self):
        """Ensures the novel_metadata_cache table exists with required schema.

        Contract v3 §3: ``publication_json`` is added by a guarded migration so
        databases created before the AniList Format/Status/Volumes work keep
        working unchanged.
        """
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS novel_metadata_cache (
                    query_key TEXT PRIMARY KEY,
                    title TEXT,
                    synopsis TEXT,
                    cover_url TEXT,
                    source TEXT,
                    publication_json TEXT DEFAULT '{}',
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_novel_metadata_cache_key ON novel_metadata_cache(query_key);")
            cur.execute("PRAGMA table_info(novel_metadata_cache);")
            cache_columns = {row["name"] for row in cur.fetchall()}
            if "publication_json" not in cache_columns:
                try:
                    cur.execute(
                        "ALTER TABLE novel_metadata_cache "
                        "ADD COLUMN publication_json TEXT DEFAULT '{}';"
                    )
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    @staticmethod
    def _publication_from_cached_row(row_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the cache row's ``publication_json`` into publication keys."""
        try:
            from app.providers.metadata import NovelMetadataProvider
        except Exception:
            return {}
        publication_raw = row_dict.get("publication_json")
        if isinstance(publication_raw, dict):
            parsed = publication_raw
        elif isinstance(publication_raw, str) and publication_raw.strip():
            try:
                parsed = json.loads(publication_raw)
            except (TypeError, ValueError):
                parsed = {}
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            return {}
        try:
            return NovelMetadataProvider.publication_from_meta(parsed)
        except Exception:
            return {}

    def get_cached_novel_metadata(self, query_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached novel metadata by query key (case-insensitive).

        The stored ``publication_json`` is parsed back into the normalized
        publication keys (format/status/volumes/...) and merged into the row.
        """
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
                publication = self._publication_from_cached_row(d)
                if publication:
                    d.update(publication)
                return d
            return None

    def cache_novel_metadata(self, query_key: str, data: Dict[str, Any]):
        """Caches novel metadata (title, synopsis, cover_url, source + publication)."""
        if not query_key or not data:
            return
        self.ensure_metadata_cache_table()
        clean_k = query_key.strip().lower()
        title = data.get("title") or query_key
        synopsis = data.get("synopsis") or ""
        cover_url = data.get("cover_url") or ""
        source = data.get("source") or "unknown"
        # Contract v3 §3: persist the normalized publication dict next to the
        # legacy cache fields so Format/Status/Volumes survive cache hits.
        publication_json = "{}"
        try:
            from app.providers.metadata import NovelMetadataProvider
            publication = NovelMetadataProvider.publication_from_meta(data)
            if publication:
                publication_json = json.dumps(publication, ensure_ascii=False)
        except Exception as exc:
            logger.debug("Could not build cache publication metadata: %s", exc)
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(novel_metadata_cache);")
            cols = {r["name"] for r in cur.fetchall()}
            key_col = "query_key" if "query_key" in cols else "key"
            if "synopsis" in cols and "cover_url" in cols:
                if "publication_json" in cols:
                    cur.execute(f"""
                        INSERT INTO novel_metadata_cache
                            ({key_col}, title, synopsis, cover_url, source,
                             publication_json, cached_at)
                        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT({key_col}) DO UPDATE SET
                            title = excluded.title,
                            synopsis = excluded.synopsis,
                            cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE novel_metadata_cache.cover_url END,
                            source = excluded.source,
                            publication_json = CASE WHEN excluded.publication_json != '{{}}' THEN excluded.publication_json ELSE novel_metadata_cache.publication_json END,
                            cached_at = CURRENT_TIMESTAMP;
                    """, (clean_k, title, synopsis, cover_url, source, publication_json))
                else:
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
