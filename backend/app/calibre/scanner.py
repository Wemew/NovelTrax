import re
import os
import json
import sqlite3
import logging
from typing import List, Dict, Any, Optional, Set, Tuple

logger = logging.getLogger("noveltrax.calibre")

def parse_calibre_volumes_and_series(
    title: str,
    series_name: Optional[str] = None,
    series_index: Optional[float] = None
) -> Tuple[Optional[str], Set[float]]:
    """
    Extracts all owned volume indices and canonical series name from a Calibre book record.
    Handles omnibus bundle titles (e.g. 'Overlord Vol 1-3', 'Mushoku Tensei v01-02',
    'Sword Art Online [1-4]', '86 - 1-4') where Calibre's series_index only stores 1.0,
    ensuring all contained volumes (e.g. {1.0, 2.0, 3.0, 4.0}) are recognized as owned.
    """
    vols: Set[float] = set()
    detected_series: Optional[str] = series_name.strip() if series_name else None

    # 1. Start with Calibre's series_index if present (allow Volume 0.0)
    if series_index is not None and series_index >= 0:
        vols.add(round(float(series_index), 2))

    if not title:
        return detected_series, vols

    # Pre-clean title to detect series name if not explicitly provided
    if not detected_series:
        clean_name = re.sub(
            r"(?i)\s*(?:[-–—:]\s*)?(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*\d+.*|\[.*?\]|\(.*?\))\s*$",
            "",
            title
        ).strip(" -–—:,|.")
        # Trailing bare range check: ensure start <= end to prevent truncating titles like 'Mob Psycho 100 - 1'
        m_bare_trail = re.search(
            r"(?i)(?:\s*[-–—:]\s*|\s+)(?P<s_bare>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<e_bare>\d+(?:\.\d+)?)\s*$",
            clean_name
        )
        if m_bare_trail:
            s_b = float(m_bare_trail.group("s_bare"))
            e_b = float(m_bare_trail.group("e_bare"))
            if s_b <= e_b:
                clean_name = clean_name[:m_bare_trail.start()].strip(" -–—:,|.")
        if clean_name:
            detected_series = clean_name

    # Prioritize clean_t before raw title to prevent numbers in series names
    # from prematurely matching volume patterns
    candidates = []
    if detected_series:
        clean_t = re.sub(r"^" + re.escape(detected_series), "", title, flags=re.IGNORECASE).strip(" -–—:,|.")
        if clean_t:
            candidates.append(clean_t)
    candidates.append(title)

    # 2. Extract volumes from title into isolated local set
    extracted_vols: Set[float] = set()
    found_range = False

    for c in candidates:
        if not c:
            continue
        # Bracketed ranges like [1-4], [Vol 1-3], (v01-04), [0-2]
        m_bracket = re.search(
            r"[\[\(]\s*(?:(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<start>\d+(?:\.\d+)?)"
            r"\s*[-–—~to/]+\s*"
            r"(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end>\d+(?:\.\d+)?)\s*[\]\)]",
            c,
            re.IGNORECASE
        )
        if m_bracket:
            p1 = m_bracket.group("p1")
            p2 = m_bracket.group("p2")
            if not p1 and p2:
                # Syntactic guard: title delimiter preceding volume tag inside brackets (e.g. '[100 - Vol 1]')
                pass
            else:
                s_val = float(m_bracket.group("start"))
                e_val = float(m_bracket.group("end"))
                has_marker = bool(p1 or p2)
                if s_val > e_val:
                    if not p1:
                        # Logic guard: reversed ranges require explicit volume prefix
                        pass
                    else:
                        s_val, e_val = e_val, s_val
                if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000 and (e_val - s_val) <= 100:
                    if (s_val < 1000 and e_val < 1000) or has_marker:
                        curr = int(s_val)
                        while curr <= int(e_val):
                            if curr >= s_val:
                                extracted_vols.add(float(curr))
                            curr += 1
                        if e_val != int(e_val):
                            extracted_vols.add(round(float(e_val), 2))
                        if s_val != int(s_val):
                            extracted_vols.add(round(float(s_val), 2))
                        found_range = True
                        break

        # Standard range: Vol 1-3, v01-02, Volume 1 to 4, 01-03, Vol 1-2.5, bare 1-4
        # Dual-branch regex ensures p1 is required whenever a prefix is present, or range is purely unadorned
        # Negative lookahead (?!\s*[-–—~to/]+\s*\d) prevents greedy false matching on titles like '86 - 1-4'
        m_range = re.search(
            r"(?i)(?:"
            r"(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*(?P<start1>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end1>\d+(?:\.\d+)?)\b"
            r"|"
            r"(?<![-–—/])\b(?P<start2>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<end2>\d+(?:\.\d+)?)\b(?!\s*[-–—~to/]+\s*\d)"
            r")",
            c
        )
        if m_range:
            p1 = m_range.group("p1")
            p2 = m_range.group("p2")
            s_val = float(m_range.group("start1") or m_range.group("start2"))
            e_val = float(m_range.group("end1") or m_range.group("end2"))
            has_marker = bool(p1 or p2)
            if s_val > e_val:
                # Logic guard: reversed ranges (e.g. Vol 3-1) ONLY allowed when p1 explicitly matched
                if not p1:
                    pass
                else:
                    s_val, e_val = e_val, s_val
            if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000 and (e_val - s_val) <= 100:
                if (s_val < 1000 and e_val < 1000) or has_marker:
                    curr = int(s_val)
                    while curr <= int(e_val):
                        if curr >= s_val:
                            extracted_vols.add(float(curr))
                        curr += 1
                    if e_val != int(e_val):
                        extracted_vols.add(round(float(e_val), 2))
                    if s_val != int(s_val):
                        extracted_vols.add(round(float(s_val), 2))
                    found_range = True
                    break

    if not found_range:
        for c in candidates:
            # Pattern list: v1, 2, 3 or Vol 1 + 2 or v1 & v2 or Book 1 & 2, Oxford comma: Vol 1, 2, and 3
            m_list = re.search(
                r"(?i)(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)"
                r"(?P<list>\d+(?:\.\d+)?(?:\s*(?:,\s*(?:and\b|[+&])|[,+&]|\band\b)\s*(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?\d+(?:\.\d+)?)+)",
                c
            )
            if m_list:
                raw_nums = re.findall(r"\d+(?:\.\d+)?", m_list.group("list"))
                for n in raw_nums:
                    val = round(float(n), 2)
                    if 0 <= val <= 2000:
                        extracted_vols.add(val)
                if extracted_vols:
                    break

            # Single volume pattern: Vol. 23, v05, Volume 12, Book 3, v15, Vol 1.5
            m_single = re.search(
                r"(?i)(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*(\d+(?:\.\d+)?)\b",
                c
            )
            if m_single:
                try:
                    s_num = float(m_single.group(1))
                    if 0 <= s_num <= 2000:
                        extracted_vols.add(round(s_num, 2))
                        break
                except ValueError:
                    pass

    # 3. Discard Calibre default series_index = 1.0 if title explicitly specifies volumes not containing 1.0
    if extracted_vols:
        if series_index == 1.0 and 1.0 not in extracted_vols:
            vols.discard(1.0)
        vols.update(extracted_vols)

    return detected_series, vols

class CalibreScanner:
    """
    Safely reads Calibre's metadata.db with zero-locking overhead.
    Uses 'mode=ro&immutable=1' to prevent any filesystem lock acquisition,
    guaranteeing that host Calibre instances can write freely without locking contention.
    """
    def __init__(self, calibre_path: str):
        self.calibre_path = calibre_path
        self.db_path = os.path.join(calibre_path, "metadata.db") if not calibre_path.endswith("metadata.db") else calibre_path

    def is_available(self) -> bool:
        """Check if metadata.db exists and is readable."""
        return os.path.isfile(self.db_path) and os.access(self.db_path, os.R_OK)

    def _get_connection(self) -> sqlite3.Connection:
        """
        Open a read-only, non-locking connection to metadata.db.
        'immutable=1' instructs SQLite that this connection will never modify the file
        and completely bypasses file-locking protocols on local and network shares.
        """
        if not self.is_available():
            raise FileNotFoundError(f"Calibre metadata.db not found or not readable at: {self.db_path}")

        # Normalize path for SQLite URI (forward slashes)
        abs_path = os.path.abspath(self.db_path).replace("\\", "/")
        uri = f"file:{abs_path}?mode=ro&immutable=1"

        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("PRAGMA query_only = ON;")
            cursor.execute("PRAGMA busy_timeout = 2000;")
            return conn
        except sqlite3.OperationalError as e:
            # Fallback if immutable URI is not supported on some unusual network filesystems
            logger.warning(f"Could not open with immutable=1, attempting fallback read-only: {e}")
            uri_ro = f"file:{abs_path}?mode=ro"
            conn = sqlite3.connect(uri_ro, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn

    def get_library_summary(self) -> Dict[str, Any]:
        """Returns total books, total series, and total authors in Calibre."""
        if not self.is_available():
            return {"available": False, "total_books": 0, "total_series": 0, "total_authors": 0}

        conn = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM books;")
            total_books = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM series;")
            total_series = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM authors;")
            total_authors = cur.fetchone()[0]

            return {
                "available": True,
                "db_path": self.db_path,
                "total_books": total_books,
                "total_series": total_series,
                "total_authors": total_authors
            }
        except Exception as e:
            logger.error(f"Error reading library summary: {e}")
            return {"available": False, "error": str(e), "total_books": 0, "total_series": 0, "total_authors": 0}
        finally:
            if conn:
                conn.close()

    def get_all_series(self) -> List[Dict[str, Any]]:
        """
        Retrieves all series from Calibre along with book counts, latest series_index,
        and associated authors.
        """
        if not self.is_available():
            return []

        query = """
            SELECT 
                s.id AS series_id,
                s.name AS series_name,
                COUNT(bsl.book) AS book_count,
                COALESCE(MAX(b.series_index), 0.0) AS latest_index,
                GROUP_CONCAT(DISTINCT a.name) AS authors
            FROM series s
            JOIN books_series_link bsl ON s.id = bsl.series
            JOIN books b ON bsl.book = b.id
            LEFT JOIN books_authors_link bal ON b.id = bal.book
            LEFT JOIN authors a ON bal.author = a.id
            GROUP BY s.id, s.name
            ORDER BY s.name COLLATE NOCASE ASC;
        """

        conn = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append({
                    "series_id": row["series_id"],
                    "series_name": row["series_name"],
                    "book_count": row["book_count"],
                    "latest_index": float(row["latest_index"]),
                    "authors": row["authors"] or "Unknown"
                })
            return results
        except Exception as e:
            logger.error(f"Error fetching series from Calibre: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_all_books(self) -> List[Dict[str, Any]]:
        """
        Retrieves all books with title, series info, volume index, and authors.
        """
        if not self.is_available():
            return []

        query = """
            SELECT 
                b.id AS book_id,
                b.title AS title,
                b.series_index AS series_index,
                s.id AS series_id,
                s.name AS series_name,
                GROUP_CONCAT(DISTINCT a.name) AS authors,
                b.uuid AS uuid
            FROM books b
            LEFT JOIN books_series_link bsl ON b.id = bsl.book
            LEFT JOIN series s ON bsl.series = s.id
            LEFT JOIN books_authors_link bal ON b.id = bal.book
            LEFT JOIN authors a ON bal.author = a.id
            GROUP BY b.id
            ORDER BY b.id DESC;
        """

        conn = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append({
                    "book_id": row["book_id"],
                    "title": row["title"],
                    "series_index": float(row["series_index"]) if row["series_index"] is not None else 1.0,
                    "series_id": row["series_id"],
                    "series_name": row["series_name"],
                    "authors": row["authors"] or "Unknown",
                    "uuid": row["uuid"]
                })
            return results
        except Exception as e:
            logger.error(f"Error fetching books from Calibre: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def get_series_volumes_map(self) -> Dict[str, Set[float]]:
        """
        Returns a map of normalized series name / series ID -> set of owned volume floats.
        Extracts single volumes as well as omnibus bundles (e.g. 'Overlord Vol 1-3',
        'Title v01-02', 'Title [1-4]'), expanding them so all contained volumes
        are recognized as owned.
        """
        if not self.is_available():
            return {}

        query = """
            SELECT s.id AS series_id, s.name AS ser_name, b.title, b.series_index
            FROM books b
            LEFT JOIN books_series_link bsl ON b.id = bsl.book
            LEFT JOIN series s ON bsl.series = s.id;
        """
        conn = None
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(query)
            res: Dict[str, Set[float]] = {}
            from app.parser import normalize_text
            for row in cur.fetchall():
                s_id = row["series_id"]
                s_name = row["ser_name"] or ""
                b_title = row["title"] or ""
                idx = float(row["series_index"]) if row["series_index"] is not None else None

                extracted_series, vols = parse_calibre_volumes_and_series(b_title, s_name, idx)
                target_series = s_name.strip() or (extracted_series or "").strip()
                if target_series and vols:
                    t_lower = target_series.lower()
                    t_norm = normalize_text(target_series)
                    res.setdefault(target_series, set()).update(vols)
                    res.setdefault(t_lower, set()).update(vols)
                    if t_norm:
                        res.setdefault(t_norm, set()).update(vols)
                    if s_id is not None:
                        res.setdefault(str(s_id), set()).update(vols)
                        res.setdefault(s_id, set()).update(vols)
                    parts = [p.strip() for p in re.split(r"\s*[|/–—]\s*|\s+-\s+|//|;", target_series) if p.strip()]
                    if len(parts) > 1:
                        for p in parts:
                            res.setdefault(p, set()).update(vols)
                            res.setdefault(p.lower(), set()).update(vols)
                            p_norm = normalize_text(p)
                            if p_norm:
                                res.setdefault(p_norm, set()).update(vols)
            return res
        except Exception as e:
            logger.error(f"Error reading series volumes map: {e}")
            return {}
        finally:
            if conn:
                conn.close()

    def get_all_volumes_per_series(self) -> Dict[str, Set[float]]:
        """Alias for get_series_volumes_map."""
        return self.get_series_volumes_map()

    def sync_library(self, db) -> Dict[str, Any]:
        """
        Synchronizes Calibre library with database:
        1. Reads all series.
        2. Computes series volumes map with omnibus parsing.
        3. Updates series_tracking including owned_volumes_json.
        Returns summary dictionary.
        """
        if not self.is_available():
            return {"synced": False, "series_synced": 0, "total_books": 0, "total_series": 0}
        series = self.get_all_series()
        volumes_map = self.get_series_volumes_map()
        count = db.sync_calibre_series(series, volumes_map=volumes_map)
        summary = self.get_library_summary()
        return {
            "synced": True,
            "series_synced": count,
            "total_books": summary.get("total_books", 0),
            "total_series": summary.get("total_series", 0),
        }
