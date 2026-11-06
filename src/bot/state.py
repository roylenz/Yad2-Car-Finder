"""Persistent searches and last observed listing values."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from car.listing import CarListing


class BotState:
    def __init__(self, path: str | None = None) -> None:
        db_path = Path(path or os.getenv("BOT_DB_PATH", "data/bot.sqlite3"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS searches ("
                "chat_id INTEGER PRIMARY KEY, url TEXT NOT NULL, interval_minutes INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS listings ("
                "chat_id INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, "
                "price INTEGER, km INTEGER, PRIMARY KEY (chat_id, url))"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(listings)")}
            if "hand" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN hand INTEGER")
            db.execute(
                "CREATE TABLE IF NOT EXISTS access (user_id INTEGER PRIMARY KEY, allowed INTEGER NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_requests ("
                "user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS access_meta (key TEXT PRIMARY KEY)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def set_search(self, chat_id: int, url: str, interval_minutes: int) -> None:
        with self._connect() as db:
            previous = db.execute("SELECT url FROM searches WHERE chat_id=?", (chat_id,)).fetchone()
            if previous is None or previous[0] != url:
                db.execute("DELETE FROM listings WHERE chat_id=?", (chat_id,))
            db.execute(
                "INSERT INTO searches(chat_id,url,interval_minutes) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET url=excluded.url, "
                "interval_minutes=excluded.interval_minutes",
                (chat_id, url, interval_minutes),
            )

    def get_search(self, chat_id: int) -> tuple[str, int] | None:
        with self._connect() as db:
            return db.execute(
                "SELECT url, interval_minutes FROM searches WHERE chat_id=?", (chat_id,)
            ).fetchone()

    def active_searches(self) -> list[tuple[int, str, int]]:
        with self._connect() as db:
            return db.execute(
                "SELECT chat_id, url, interval_minutes FROM searches WHERE interval_minutes > 0"
            ).fetchall()

    def stop_search(self, chat_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM searches WHERE chat_id=?", (chat_id,))
            db.execute("DELETE FROM listings WHERE chat_id=?", (chat_id,))

    def previous_listings(self, chat_id: int) -> dict[str, CarListing]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT url, title, price, km, hand FROM listings WHERE chat_id=?", (chat_id,)
            ).fetchall()
        return {row[0]: CarListing(*row) for row in rows}

    def save_listings(self, chat_id: int, listings: list[CarListing]) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT INTO listings(chat_id,url,title,price,km,hand) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(chat_id,url) DO UPDATE SET title=excluded.title, "
                "price=COALESCE(excluded.price,listings.price), "
                "km=COALESCE(excluded.km,listings.km), "
                "hand=COALESCE(excluded.hand,listings.hand)",
                [
                    (chat_id, item.url, item.title, item.price, item.km, item.hand)
                    for item in listings
                ],
            )

    def seen_count(self, chat_id: int) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM listings WHERE chat_id=?", (chat_id,)).fetchone()[0]

    def access_override(self, user_id: int) -> bool | None:
        with self._connect() as db:
            row = db.execute("SELECT allowed FROM access WHERE user_id=?", (user_id,)).fetchone()
        return bool(row[0]) if row else None

    def set_access(self, user_id: int, allowed: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO access(user_id,allowed) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET allowed=excluded.allowed",
                (user_id, int(allowed)),
            )

    def access_overrides(self) -> dict[int, bool]:
        with self._connect() as db:
            rows = db.execute("SELECT user_id, allowed FROM access").fetchall()
        return {user_id: bool(allowed) for user_id, allowed in rows}

    def claim_first_admin(self, user_id: int, configured_admins: bool) -> bool:
        """Atomically claim bootstrap once, even if two users message together."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if configured_admins:
                db.execute("INSERT OR IGNORE INTO access_meta(key) VALUES ('bootstrapped')")
                return False
            if db.execute(
                "SELECT 1 FROM access_meta WHERE key='bootstrapped'"
            ).fetchone():
                return False
            db.execute("INSERT INTO admins(user_id) VALUES (?)", (user_id,))
            db.execute("INSERT INTO access_meta(key) VALUES ('bootstrapped')")
            return True

    def is_admin(self, user_id: int) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None

    def admin_ids(self) -> set[int]:
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT user_id FROM admins")}

    def set_admin(self, user_id: int, admin: bool) -> None:
        with self._connect() as db:
            if admin:
                db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (user_id,))
            else:
                db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))

    def request_access(self, user_id: int, name: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO access_requests(user_id,name,status) VALUES (?,?,'pending') "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,status='pending'",
                (user_id, name),
            )

    def access_request(self, user_id: int) -> tuple[str, str] | None:
        with self._connect() as db:
            return db.execute(
                "SELECT name,status FROM access_requests WHERE user_id=?", (user_id,)
            ).fetchone()

    def pending_requests(self) -> list[tuple[int, str]]:
        with self._connect() as db:
            return db.execute(
                "SELECT user_id,name FROM access_requests WHERE status='pending' ORDER BY rowid"
            ).fetchall()

    def resolve_request(self, user_id: int, approved: bool) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE access_requests SET status=? WHERE user_id=? AND status='pending'",
                ("approved" if approved else "denied", user_id),
            )
            if cursor.rowcount and approved:
                db.execute(
                    "INSERT INTO access(user_id,allowed) VALUES (?,1) "
                    "ON CONFLICT(user_id) DO UPDATE SET allowed=1",
                    (user_id,),
                )
            return cursor.rowcount > 0
