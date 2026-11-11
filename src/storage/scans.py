"""Scan log: one row per scan run, plus each search's next scheduled scan."""

import datetime

from sqlalchemy import Engine, func, select, update

from storage.db import utcnow
from storage.schema import scans, searches

ACTIVE = ("queued", "running")


class ScanLog:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def queued(self, chat_id: int, search_url: str) -> int:
        with self.engine.begin() as db:
            return db.execute(
                scans.insert().values(
                    chat_id=chat_id, search_url=search_url, status="queued", queued_at=utcnow()
                )
            ).inserted_primary_key[0]

    def started(self, scan_id: int) -> None:
        self._set(scan_id, status="running", started_at=utcnow(), progress="Starting browser")

    def progress(self, scan_id: int, text: str) -> None:
        """Called from the scraper thread; each call is its own short transaction."""
        self._set(scan_id, progress=text)

    def finished(
        self,
        scan_id: int,
        results: int,
        new_ads: int,
        price_drops: int,
        removed: int,
        details_fetched: int,
    ) -> None:
        self._set(
            scan_id,
            status="ok",
            finished_at=utcnow(),
            progress=None,
            results=results,
            new_ads=new_ads,
            price_drops=price_drops,
            removed=removed,
            details_fetched=details_fetched,
        )

    def failed(self, scan_id: int, error: str) -> None:
        self._set(scan_id, status="failed", finished_at=utcnow(), progress=None, error=error)

    def cancelled(self, scan_id: int, reason: str) -> None:
        self._set(scan_id, status="cancelled", finished_at=utcnow(), progress=None, error=reason)

    def interrupt_unfinished(self) -> int:
        """On startup: scans left queued/running by a restart will never finish."""
        with self.engine.begin() as db:
            return db.execute(
                update(scans)
                .where(scans.c.status.in_(ACTIVE))
                .values(
                    status="interrupted",
                    finished_at=utcnow(),
                    progress=None,
                    error="Bot restarted before the scan finished",
                )
            ).rowcount

    def set_next(self, chat_id: int, when: datetime.datetime | None) -> None:
        with self.engine.begin() as db:
            db.execute(
                update(searches).where(searches.c.chat_id == chat_id).values(next_scan_at=when)
            )

    def last_started(self, chat_id: int) -> datetime.datetime | None:
        """When this chat's most recent scan started (naive UTC)."""
        with self.engine.connect() as db:
            return db.execute(
                select(func.max(scans.c.started_at)).where(scans.c.chat_id == chat_id)
            ).scalar_one()

    def recent(self, limit: int = 100, chat_id: int | None = None) -> list:
        statement = select(scans).order_by(scans.c.queued_at.desc(), scans.c.id.desc()).limit(limit)
        if chat_id is not None:
            statement = statement.where(scans.c.chat_id == chat_id)
        with self.engine.connect() as db:
            return db.execute(statement).all()

    def _set(self, scan_id: int, **values) -> None:
        with self.engine.begin() as db:
            db.execute(update(scans).where(scans.c.id == scan_id).values(**values))
