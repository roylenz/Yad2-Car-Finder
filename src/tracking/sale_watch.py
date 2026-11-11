"""Follow removed ads for a week to see whether the car was sold.

Yad2 does not say why an ad was taken down. For ads with a registry-confirmed licence plate,
the government ownership history (updated daily) shows a sale as a new owner entry: one that
was not in the history saved with the ad, from the month of removal or later. A dealer taking
the car counts too (`sold_to` says who). Each removed ad is checked about every 12 hours for
7 days, then marked `not_seen`. Ads removed before this existed get one check.
"""

import datetime
from collections import Counter
from collections.abc import Callable

import httpx
from sqlalchemy import Engine, or_, select, update

from common.logger import logger
from processing import plates
from storage.db import utcnow
from storage.schema import ads

WATCH = datetime.timedelta(days=7)
RECHECK = datetime.timedelta(hours=12)


def new_owners(history: list[dict], baseline: list[dict] | None, removed: datetime.datetime) -> list[dict]:
    """Ownership entries that appeared since the baseline, dated in the removal month or later."""
    known = Counter((entry["month"], entry["owner"]) for entry in baseline or [])
    removed_month = f"{removed:%Y-%m}"
    found = []
    for entry in history:
        key = (entry["month"], entry["owner"])
        if known[key]:
            known[key] -= 1
            continue
        if entry["month"] >= removed_month:
            found.append(entry)
    return found


class SaleWatch:
    def __init__(self, engine: Engine, client: httpx.AsyncClient | None = None) -> None:
        self.engine = engine
        self._client = client

    def due(self, now: datetime.datetime | None = None) -> list:
        now = now or utcnow()
        with self.engine.connect() as db:
            rows = db.execute(
                select(ads.c.token, ads.c.plate, ads.c.removed_at, ads.c.ownership,
                       ads.c.ownership_checked_at, ads.c.sale_status)
                .where(
                    ads.c.removed_at.is_not(None),
                    ads.c.plate_status == "confirmed",
                    ads.c.plate.is_not(None),
                    or_(ads.c.sale_status.is_(None), ads.c.sale_status == "watching"),
                )
                .order_by(ads.c.removed_at.desc())
            ).all()
        return [
            row for row in rows
            if row.sale_status is None
            or row.ownership_checked_at is None
            or row.ownership_checked_at <= now - RECHECK
            or row.removed_at <= now - WATCH
        ]

    def pending_count(self) -> int:
        return len(self.due())

    async def run(
        self,
        busy: Callable[[], bool] = lambda: False,
        progress: Callable[[int, int], None] | None = None,
        limit: int = 30,
    ) -> int:
        """Check the ads that are due; returns how many were checked."""
        rows = self.due()[:limit]
        if not rows:
            return 0
        client = self._client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "yad2-car-finder"})
        checked = 0
        try:
            for row in rows:
                if busy():
                    break
                if progress:
                    progress(checked, len(rows))
                try:
                    history = await plates.ownership_history(client, row.plate)
                except Exception as error:
                    logger.warning("Sale follow-up lookup failed", token=row.token, error=str(error))
                    continue
                self._save(row, history)
                checked += 1
        finally:
            if self._client is None:
                await client.aclose()
        return checked

    def _save(self, row, history: list[dict], now: datetime.datetime | None = None) -> None:
        now = now or utcnow()
        found = new_owners(history, row.ownership, row.removed_at)
        values: dict = {"ownership_checked_at": now}
        if found:
            values.update(sale_status="sold", sold_month=found[0]["month"], sold_to=found[0]["owner"],
                          ownership=history)
        elif row.removed_at <= now - WATCH:
            values["sale_status"] = "not_seen"
        else:
            values["sale_status"] = "watching"
        if row.ownership is None:
            values["ownership"] = history
        with self.engine.begin() as db:
            db.execute(update(ads).where(ads.c.token == row.token).values(**values))
