"""Live status of the bot's background pipeline, written by the bot and read by the dashboard."""

import datetime

from sqlalchemy import Engine, select

from storage.db import upsert, utcnow
from storage.schema import background_status


class StatusBoard:
    def __init__(self, engine: Engine, worker: str = "pipeline") -> None:
        self.engine = engine
        self.worker = worker
        self._state: tuple | None = None
        self._since: datetime.datetime | None = None

    def set(
        self,
        state: str,
        detail: str | None = None,
        done: int | None = None,
        total: int | None = None,
        next_run_at: datetime.datetime | None = None,
    ) -> None:
        now = utcnow()
        if self._state != (state, detail):
            self._state, self._since = (state, detail), now
        with self.engine.begin() as db:
            upsert(
                db,
                background_status,
                {
                    "worker": self.worker,
                    "state": state,
                    "detail": detail,
                    "done": done,
                    "total": total,
                    "started_at": self._since,
                    "next_run_at": next_run_at,
                    "updated_at": now,
                },
                ["worker"],
                ["state", "detail", "done", "total", "started_at", "next_run_at", "updated_at"],
            )


def read_status(engine: Engine, worker: str = "pipeline"):
    with engine.connect() as db:
        return db.execute(
            select(background_status).where(background_status.c.worker == worker)
        ).first()
