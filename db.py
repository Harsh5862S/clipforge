"""
ClipForge database layer — PostgreSQL via psycopg2.

Locally: requires a running PostgreSQL server (see README).
On Render: automatically uses the DATABASE_URL environment variable that
Render injects when you attach a PostgreSQL database to your service.
"""

import os
import secrets
import hashlib
import hmac
import time

import psycopg2
import psycopg2.extras

# ── DATABASE CONNECTION SETTINGS ───────────────────────────────────────────
# On Render, DATABASE_URL is set automatically — nothing to configure.
# Locally, either set DATABASE_URL yourself, or fall back to the individual
# DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD variables below.
DATABASE_URL = os.environ.get("DATABASE_URL")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "clipforge")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "YOUR_POSTGRES_PASSWORD_HERE")
# ───────────────────────────────────────────────────────────────────────────


def get_conn():
    if DATABASE_URL:
        # Render (and most managed Postgres hosts) provide a full connection
        # string like: postgres://user:pass@host:port/dbname
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            is_admin BOOLEAN NOT NULL DEFAULT FALSE,
            is_banned BOOLEAN NOT NULL DEFAULT FALSE,
            is_verified BOOLEAN NOT NULL DEFAULT FALSE,
            created_at BIGINT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS otps (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at BIGINT NOT NULL,
            consumed BOOLEAN NOT NULL DEFAULT FALSE,
            created_at BIGINT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            detail TEXT,
            created_at BIGINT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS video_history (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            video_id TEXT NOT NULL,
            video_url TEXT NOT NULL,
            video_title TEXT,
            video_author TEXT,
            video_thumbnail TEXT,
            content_type TEXT,
            video_summary TEXT,
            clip_duration INTEGER,
            clip_count INTEGER,
            clips_json TEXT NOT NULL,
            created_at BIGINT NOT NULL
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Password hashing (PBKDF2, stdlib only) ──────────────────────────────

def hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    computed, _ = hash_password(password, salt)
    return hmac.compare_digest(computed, password_hash)


# ── User functions ──────────────────────────────────────────────────────

def create_user(email: str, password: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    pw_hash, salt = hash_password(password)

    cur.execute("SELECT COUNT(*) AS c FROM users")
    existing_count = cur.fetchone()["c"]
    is_admin = existing_count == 0

    cur.execute(
        "INSERT INTO users (email, password_hash, password_salt, is_admin, is_verified, created_at) "
        "VALUES (%s, %s, %s, %s, FALSE, %s) RETURNING id",
        (email.lower().strip(), pw_hash, salt, is_admin, int(time.time())),
    )
    user_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return user_id


def get_user_by_email(email: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = %s", (email.lower().strip(),))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def get_user_by_id(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def mark_verified(email: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_verified = TRUE WHERE email = %s", (email.lower().strip(),))
    conn.commit()
    cur.close()
    conn.close()


def set_password(email: str, password: str):
    pw_hash, salt = hash_password(password)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET password_hash = %s, password_salt = %s WHERE email = %s",
        (pw_hash, salt, email.lower().strip()),
    )
    conn.commit()
    cur.close()
    conn.close()


def list_all_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, is_admin, is_banned, is_verified, created_at FROM users ORDER BY created_at DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def set_user_banned(user_id: int, banned: bool):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned = %s WHERE id = %s", (banned, user_id))
    conn.commit()
    cur.close()
    conn.close()


def delete_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM activity_log WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()


# ── OTP functions ───────────────────────────────────────────────────────

def create_otp(email: str, purpose: str, ttl_seconds: int = 600) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO otps (email, code, purpose, expires_at, created_at) VALUES (%s, %s, %s, %s, %s)",
        (email.lower().strip(), code, purpose, int(time.time()) + ttl_seconds, int(time.time())),
    )
    conn.commit()
    cur.close()
    conn.close()
    return code


def verify_otp(email: str, code: str, purpose: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "SELECT * FROM otps WHERE email = %s AND code = %s AND purpose = %s "
        "AND consumed = FALSE AND expires_at > %s ORDER BY id DESC LIMIT 1",
        (email.lower().strip(), code, purpose, now),
    )
    row = cur.fetchone()

    if row is None:
        cur.close()
        conn.close()
        return False

    cur.execute("UPDATE otps SET consumed = TRUE WHERE id = %s", (row["id"],))
    conn.commit()
    cur.close()
    conn.close()
    return True


# ── Session functions ───────────────────────────────────────────────────

def create_session(user_id: int, ttl_seconds: int = 60 * 60 * 24 * 14) -> str:
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
        (token, user_id, now, now + ttl_seconds),
    )
    conn.commit()
    cur.close()
    conn.close()
    return token


def get_session_user(token: str):
    if not token:
        return None
    conn = get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "SELECT users.* FROM sessions JOIN users ON sessions.user_id = users.id "
        "WHERE sessions.token = %s AND sessions.expires_at > %s",
        (token, now),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def delete_session(token: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
    conn.commit()
    cur.close()
    conn.close()


# ── Activity logging ────────────────────────────────────────────────────

def log_activity(user_id: int, action: str, detail: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO activity_log (user_id, action, detail, created_at) VALUES (%s, %s, %s, %s)",
        (user_id, action, detail, int(time.time())),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_activity_for_user(user_id: int, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM activity_log WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_all_activity(limit: int = 200):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT activity_log.*, users.email FROM activity_log "
        "JOIN users ON activity_log.user_id = users.id "
        "ORDER BY activity_log.created_at DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ── Video history (saved analyses) ──────────────────────────────────────

def save_video_history(user_id: int, video_id: str, video_url: str, video_title: str,
                        video_author: str, video_thumbnail: str, content_type: str,
                        video_summary: str, clip_duration: int, clip_count: int,
                        clips_json: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO video_history "
        "(user_id, video_id, video_url, video_title, video_author, video_thumbnail, "
        "content_type, video_summary, clip_duration, clip_count, clips_json, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, video_id, video_url, video_title, video_author, video_thumbnail,
         content_type, video_summary, clip_duration, clip_count, clips_json, int(time.time())),
    )
    history_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return history_id


def get_video_history_for_user(user_id: int, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM video_history WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_video_history_entry(history_id: int, user_id: int):
    """Fetch a single history entry, scoped to the owning user for safety."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM video_history WHERE id = %s AND user_id = %s",
        (history_id, user_id),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def delete_video_history_entry(history_id: int, user_id: int) -> bool:
    """Delete a history entry, scoped to the owning user. Returns True if deleted."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM video_history WHERE id = %s AND user_id = %s",
        (history_id, user_id),
    )
    deleted = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return deleted
