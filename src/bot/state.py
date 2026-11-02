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
                "SELECT url, title, price, km FROM listings WHERE chat_id=?", (chat_id,)
            ).fetchall()
        return {url: CarListing(url, title, price, km) for url, title, price, km in rows}

    def save_listings(self, chat_id: int, listings: list[CarListing]) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT INTO listings(chat_id,url,title,price,km) VALUES (?,?,?,?,?) "
                "ON CONFLICT(chat_id,url) DO UPDATE SET title=excluded.title, "
                "price=COALESCE(excluded.price,listings.price), "
                "km=COALESCE(excluded.km,listings.km)",
                [(chat_id, item.url, item.title, item.price, item.km) for item in listings],
            )

    def seen_count(self, chat_id: int) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM listings WHERE chat_id=?", (chat_id,)).fetchone()[0]
