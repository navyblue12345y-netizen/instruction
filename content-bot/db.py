"""
Работа с SQLite БД: инициализация, CRUD постов, дедупликация.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "content_bot.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type  TEXT NOT NULL,
            source_id    TEXT NOT NULL,
            original_text  TEXT,
            rewritten_text TEXT,
            media_url    TEXT,
            media_type   TEXT,
            media_files  TEXT,
            channel      TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL,
            posted_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS seen_posts (
            url_hash TEXT PRIMARY KEY,
            seen_at  TEXT NOT NULL
        );
    """)
    # Миграция: добавляем media_files если ещё нет
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if "media_files" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN media_files TEXT")
    conn.commit()
    conn.close()


# ── Дедупликация ────────────────────────────────────────────────────────────

def _hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def is_seen(url: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM seen_posts WHERE url_hash=?", (_hash(url),)).fetchone()
    conn.close()
    return row is not None


def add_seen(url: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO seen_posts (url_hash, seen_at) VALUES (?, ?)",
        (_hash(url), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ── Посты ───────────────────────────────────────────────────────────────────

def add_post(source_type: str, source_id: str, original_text: str,
             rewritten_text: str, media_url: str, media_type: str,
             channel: str, media_files: list = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO posts
           (source_type, source_id, original_text, rewritten_text,
            media_url, media_type, media_files, channel, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (source_type, source_id, original_text, rewritten_text,
         media_url, media_type,
         json.dumps(media_files) if media_files else None,
         channel, datetime.utcnow().isoformat()),
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return post_id


def mark_posted(post_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE posts SET status='posted', posted_at=? WHERE id=?",
        (datetime.utcnow().isoformat(), post_id),
    )
    conn.commit()
    conn.close()


def mark_skipped(post_id: int):
    conn = get_conn()
    conn.execute("UPDATE posts SET status='skipped' WHERE id=?", (post_id,))
    conn.commit()
    conn.close()


def get_pending_posts(channel: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM posts WHERE channel=? AND status='pending' ORDER BY created_at ASC LIMIT ?",
        (channel, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT channel, status, COUNT(*) as cnt FROM posts GROUP BY channel, status"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
