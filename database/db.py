import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'amina.sqlite3'

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('PRAGMA foreign_keys = ON')
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT NOT NULL,
            joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        await db.commit()

async def upsert_user(telegram_id: int, username: str | None, display_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO users (telegram_id, username, display_name) VALUES (?, ?, ?) '
            'ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, display_name=excluded.display_name',
            (telegram_id, username, display_name),
        )
        await db.commit()
