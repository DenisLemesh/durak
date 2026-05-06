"""
database.py — SQLite база данных пользователей
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join('data', 'users.db')
os.makedirs('data', exist_ok=True)


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                tg_id       INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                joined_at   TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                games       INTEGER DEFAULT 0,
                wins        INTEGER DEFAULT 0
            )
        ''')
        conn.commit()


def upsert_user(tg_id: int, first_name: str = None,
                last_name: str = None, username: str = None):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute('''
            INSERT INTO users (tg_id, username, first_name, last_name, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                last_seen  = excluded.last_seen
        ''', (tg_id, username, first_name, last_name, now, now))
        conn.commit()


def increment_stats(tg_id: int, won: bool):
    with get_conn() as conn:
        if won:
            conn.execute('UPDATE users SET games=games+1, wins=wins+1 WHERE tg_id=?', (tg_id,))
        else:
            conn.execute('UPDATE users SET games=games+1 WHERE tg_id=?', (tg_id,))
        conn.commit()


def get_all_users():
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            'SELECT * FROM users ORDER BY joined_at DESC'
        ).fetchall()]


def get_user(tg_id: int):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM users WHERE tg_id=?', (tg_id,)).fetchone()
        return dict(row) if row else None


init_db()
