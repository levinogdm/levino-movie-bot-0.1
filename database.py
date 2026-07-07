import sqlite3
import secrets
from datetime import datetime, timezone

DB_PATH = "bot_data.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS files (
            code TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            title TEXT,
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
    # Movie requests: created as 'pending' the instant a search has no match,
    # then flipped to 'requested' only once the user taps the confirm button
    # (this avoids spamming the request channel with every failed search).
    # Once a matching file is uploaded by the admin, status becomes 'fulfilled'.
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


def save_file(file_id: str, file_type: str, title: str) -> str:
    conn = get_conn()
    c = conn.cursor()
    while True:
        code = generate_code()
        c.execute("SELECT 1 FROM files WHERE code=?", (code,))
        if not c.fetchone():
            break
    c.execute(
        "INSERT INTO files (code, file_id, file_type, title, created_at) VALUES (?,?,?,?,?)",
        (code, file_id, file_type, title, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return code


def get_file(code: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM files WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def search_files(query: str, limit: int = 8):
    conn = get_conn()
    c = conn.cursor()
    like_query = f"%{query.strip()}%"
    c.execute(
        "SELECT * FROM files WHERE title LIKE ? ORDER BY created_at DESC LIMIT ?",
        (like_query, limit),
    )
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


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


def get_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM users")
    users = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM files")
    files = c.fetchone()["n"]
    conn.close()
    return {"users": users, "files": files}


def remove_user(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


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
    """Find still-open ('requested') requests whose text overlaps the newly
    uploaded movie's title, in either direction, so close-but-not-identical
    wording still matches (e.g. request 'KGF 2' vs title 'KGF Chapter 2')."""
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
