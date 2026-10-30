import sqlite3
import os

def create_mock_calibre_db(db_path: str):
    """
    Creates a realistic Calibre metadata.db file conforming to Calibre's schema.
    Populates it with sample series and books.
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Core Calibre schema
    cur.executescript("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'Unknown' COLLATE NOCASE,
            sort TEXT COLLATE NOCASE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pubdate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            series_index REAL NOT NULL DEFAULT 1.0,
            author_sort TEXT COLLATE NOCASE,
            path TEXT NOT NULL DEFAULT '',
            uuid TEXT,
            has_cover BOOL DEFAULT 0,
            last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:00+00:00'
        );

        CREATE TABLE authors (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE,
            sort TEXT COLLATE NOCASE,
            link TEXT NOT NULL DEFAULT '',
            UNIQUE(name)
        );

        CREATE TABLE series (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE,
            sort TEXT COLLATE NOCASE,
            link TEXT NOT NULL DEFAULT '',
            UNIQUE(name)
        );

        CREATE TABLE books_authors_link (
            id INTEGER PRIMARY KEY,
            book INTEGER NOT NULL,
            author INTEGER NOT NULL,
            UNIQUE(book, author)
        );

        CREATE TABLE books_series_link (
            id INTEGER PRIMARY KEY,
            book INTEGER NOT NULL,
            series INTEGER NOT NULL,
            UNIQUE(book)
        );
    """)

    # Populate Sample Data
    # Series:
    # 1. DanMachi (Vols 1 to 18)
    # 2. Overlord (Vols 1 to 15)
    # 3. Dungeon Crawler Carl (Vols 1 to 6)
    cur.execute("INSERT INTO series (id, name, sort) VALUES (1, 'DanMachi', 'DanMachi');")
    cur.execute("INSERT INTO series (id, name, sort) VALUES (2, 'Overlord', 'Overlord');")
    cur.execute("INSERT INTO series (id, name, sort) VALUES (3, 'Dungeon Crawler Carl', 'Dungeon Crawler Carl');")

    cur.execute("INSERT INTO authors (id, name, sort) VALUES (1, 'Fujino Omori', 'Omori, Fujino');")
    cur.execute("INSERT INTO authors (id, name, sort) VALUES (2, 'Kugane Maruyama', 'Maruyama, Kugane');")
    cur.execute("INSERT INTO authors (id, name, sort) VALUES (3, 'Matt Dinniman', 'Dinniman, Matt');")

    # Add DanMachi 1-18
    for i in range(1, 19):
        cur.execute("INSERT INTO books (title, series_index, author_sort) VALUES (?, ?, ?);",
                    (f"DanMachi, Vol. {i}", float(i), "Omori, Fujino"))
        b_id = cur.lastrowid
        cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, 1);", (b_id,))
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, 1);", (b_id,))

    # Add Overlord 1-15
    for i in range(1, 16):
        cur.execute("INSERT INTO books (title, series_index, author_sort) VALUES (?, ?, ?);",
                    (f"Overlord, Vol. {i}", float(i), "Maruyama, Kugane"))
        b_id = cur.lastrowid
        cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, 2);", (b_id,))
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, 2);", (b_id,))

    # Add Dungeon Crawler Carl 1-6
    for i in range(1, 7):
        cur.execute("INSERT INTO books (title, series_index, author_sort) VALUES (?, ?, ?);",
                    (f"Dungeon Crawler Carl Book {i}", float(i), "Dinniman, Matt"))
        b_id = cur.lastrowid
        cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, 3);", (b_id,))
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, 3);", (b_id,))

    conn.commit()
    conn.close()
    return db_path

def add_book_to_mock_calibre(db_path: str, title: str, series_id: int, series_index: float, author_id: int = 1):
    """Simulates adding a new book into Calibre."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("INSERT INTO books (title, series_index) VALUES (?, ?);", (title, float(series_index)))
    b_id = cur.lastrowid
    cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, ?);", (b_id, author_id))
    cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, ?);", (b_id, series_id))
    conn.commit()
    conn.close()
    return b_id
