import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'amina.sqlite3'

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('PRAGMA foreign_keys = ON')
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT NOT NULL,
            joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_poll_id TEXT UNIQUE,
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, question TEXT NOT NULL,
            options_json TEXT NOT NULL, creator_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
            close_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            final_counts_json TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS poll_votes (
            poll_id TEXT NOT NULL, user_id INTEGER NOT NULL, option_id INTEGER NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY(poll_id,user_id))""")
        await db.commit()

async def upsert_user(telegram_id: int, username: str | None, display_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO users (telegram_id, username, display_name) VALUES (?, ?, ?) '
            'ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, display_name=excluded.display_name',
            (telegram_id, username, display_name))
        await db.commit()

async def save_poll(poll_id: str, chat_id: int, message_id: int, question: str,
                    options: list[str], creator_id: int, created_at: int, close_at: int) -> None:
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO polls (telegram_poll_id,chat_id,message_id,question,options_json,creator_id,created_at,close_at) VALUES (?,?,?,?,?,?,?,?)',
            (poll_id, chat_id, message_id, question, json.dumps(options, ensure_ascii=False), creator_id, created_at, close_at))
        await db.commit()

async def save_vote(poll_id: str, user_id: int, option_id: int | None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT close_at,status FROM polls WHERE telegram_poll_id=?', (poll_id,))
        row = await cur.fetchone()
        import time
        if not row or row[1] != 'active' or row[0] <= int(time.time()):
            return False
        if option_id is None:
            await db.execute('DELETE FROM poll_votes WHERE poll_id=? AND user_id=?', (poll_id,user_id))
        else:
            await db.execute('INSERT INTO poll_votes (poll_id,user_id,option_id) VALUES (?,?,?) ON CONFLICT(poll_id,user_id) DO UPDATE SET option_id=excluded.option_id,updated_at=strftime("%s","now")',
                (poll_id,user_id,option_id))
        await db.commit()
        return True

async def get_expired_polls(now: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT telegram_poll_id,chat_id,message_id,question,options_json FROM polls WHERE status="active" AND close_at<=?', (now,))
        return await cur.fetchall()

async def finalize_poll(poll_id: str):
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT options_json FROM polls WHERE telegram_poll_id=?', (poll_id,))
        row = await cur.fetchone()
        if not row: return []
        options = json.loads(row[0])
        counts = [0 for _ in options]
        cur = await db.execute('SELECT option_id,COUNT(*) FROM poll_votes WHERE poll_id=? GROUP BY option_id', (poll_id,))
        for option_id,count in await cur.fetchall():
            if 0 <= option_id < len(counts): counts[option_id] = count
        await db.execute('UPDATE polls SET status="closed",final_counts_json=? WHERE telegram_poll_id=?',
            (json.dumps(counts),poll_id))
        await db.commit()
        return list(zip(options,counts))

async def get_poll_history(limit: int = 10):
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute('SELECT question,options_json,final_counts_json,created_at,status FROM polls ORDER BY created_at DESC LIMIT ?', (limit,))
        rows = await cur.fetchall()
        return [(q, json.loads(opts), json.loads(counts) if counts else None, created, status) for q,opts,counts,created,status in rows]
