import os
import sqlite3
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("noveltrax.calibre")

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
