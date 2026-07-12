import sqlite3
import secrets
import re
import difflib
from datetime import datetime, timezone

DB_PATH = "bot_data.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # One row per MOVIE (title + poster + which message this is on the main channel)
    c.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            code TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            poster_file_id TEXT,
            main_message_id INTEGER,
            created_at TEXT
        )
    """)
    # One row per FILE belonging to a movie (a movie can have several: different
    # sizes/qualities). All files under the same movie 'code' get delivered together.
    c.execute("""
        CREATE TABLE IF NOT EXISTS movie_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER,
            label TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            query_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def generate_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]


# ---------------- Title matching (for "same movie" detection) ----------------

def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def find_movie_by_title(title: str):
    """Partial/similar match: returns the best-matching existing movie, or
    None. A match is either a plain substring overlap (either direction) or
    a high spelling-similarity ratio, so small typos still match."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM movies")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    target = _normalize(title)
    if not target:
        return None

    best_row, best_score = None, 0.0
    for row in rows:
        existing = _normalize(row["title"])
        if not existing:
            continue
        if target in existing or existing in target:
            score = 1.0
        else:
            score = difflib.SequenceMatcher(None, target, existing).ratio()
        if score > best_score:
            best_score, best_row = score, row

    if best_row and best_score >= 0.6:
        return best_row
    return None


# ---------------- Movies & files ----------------

def create_movie(title: str, poster_file_id: str = None) -> str:
    conn = get_conn()
    c = conn.cursor()
    while True:
        code = generate_code()
        c.execute("SELECT 1 FROM movies WHERE code=?", (code,))
        if not c.fetchone():
            break
    c.execute(
        "INSERT INTO movies (code, title, poster_file_id, main_message_id, created_at) VALUES (?,?,?,?,?)",
        (code, title, poster_file_id, None, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return code


def update_movie_message_id(code: str, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE movies SET main_message_id=? WHERE code=?", (message_id, code))
    conn.commit()
    conn.close()


def update_movie_poster(code: str, poster_file_id: str, main_message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE movies SET poster_file_id=?, main_message_id=? WHERE code=?",
        (poster_file_id, main_message_id, code),
    )
    conn.commit()
    conn.close()


def add_movie_file(code: str, file_id: str, file_type: str, file_size: int = None, label: str = None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO movie_files (code, file_id, file_type, file_size, label, created_at) VALUES (?,?,?,?,?,?)",
        (code, file_id, file_type, file_size, label, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_movie_file(file_row_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM movie_files WHERE id=?", (file_row_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_movie(code: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM movies WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_movie_files(code: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM movie_files WHERE code=? ORDER BY id ASC", (code,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def search_movies(query: str, limit: int = 8):
    conn = get_conn()
    c = conn.cursor()
    like_query = f"%{query.strip()}%"
    c.execute(
        "SELECT * FROM movies WHERE title LIKE ? ORDER BY created_at DESC LIMIT ?",
        (like_query, limit),
    )
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ---------------- Users ----------------

def add_user(user_id: int, username: str, first_name: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    if not c.fetchone():
        c.execute(
            "INSERT INTO users (user_id, username, first_name, joined_at) VALUES (?,?,?,?)",
            (user_id, username, first_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    conn.close()


def get_all_user_ids():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def remove_user(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM users")
    users = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM movies")
    movies = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM movie_files")
    files = c.fetchone()["n"]
    conn.close()
    return {"users": users, "movies": movies, "files": files}


# ---------------- Movie requests ----------------

def create_request(user_id: int, query_text: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO requests (user_id, query_text, status, created_at) VALUES (?,?,?,?)",
        (user_id, query_text, "pending", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    req_id = c.lastrowid
    conn.close()
    return req_id


def get_request(req_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE id=?", (req_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def confirm_request(req_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE requests SET status='requested' WHERE id=?", (req_id,))
    conn.commit()
    c.execute("SELECT * FROM requests WHERE id=?", (req_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_matching_requests(title: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE status='requested'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    title_l = title.lower()
    matches = []
    for r in rows:
        q = r["query_text"].lower()
        if q in title_l or title_l in q:
            matches.append(r)
    return matches


def mark_request_fulfilled(req_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE requests SET status='fulfilled' WHERE id=?", (req_id,))
    conn.commit()
    conn.close()


def get_pending_requests(limit: int = 30):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM requests WHERE status='requested' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]
