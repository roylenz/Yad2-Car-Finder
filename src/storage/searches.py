"""Saved searches. A chat can have several; each is scanned on its own schedule."""

from dataclasses import dataclass

from sqlalchemy import Engine, delete, insert, select, update

from storage.ads import AdRepository
from storage.db import utcnow
from storage.schema import search_ads, searches


@dataclass(frozen=True)
class Search:
    """A saved search. A chat can have several; each is scanned on its own schedule."""

    id: int
    chat_id: int
    url: str
    interval_minutes: int

    @classmethod
    def from_row(cls, row) -> "Search":
        return cls(row.id, row.chat_id, row.url, row.interval_minutes)


class SearchStore:
    """Used by the bot and the dashboard; the bot schedules what is saved here."""

    def __init__(self, engine: Engine, ads: AdRepository | None = None) -> None:
        self.engine = engine
        self.ads = ads or AdRepository(engine)

    def add_search(
        self, chat_id: int, url: str, interval_minutes: int, added_from: str = "telegram"
    ) -> tuple["Search", bool]:
        """Save a search for the chat (alongside its others). Returns it and whether it is new;
        adding a search the chat already has only updates its interval."""
        with self.engine.begin() as db:
            existing = db.execute(
                select(searches).where(searches.c.chat_id == chat_id, searches.c.url == url)
            ).first()
            if existing is not None:
                db.execute(
                    update(searches)
                    .where(searches.c.id == existing.id)
                    .values(interval_minutes=interval_minutes)
                )
                return Search(existing.id, chat_id, url, interval_minutes), False
            search_id = db.execute(
                insert(searches).values(
                    chat_id=chat_id,
                    url=url,
                    interval_minutes=interval_minutes,
                    created_at=utcnow(),
                    added_from=added_from,
                )
            ).inserted_primary_key[0]
        return Search(search_id, chat_id, url, interval_minutes), True

    def get_search(self, search_id: int) -> "Search | None":
        with self.engine.connect() as db:
            row = db.execute(select(searches).where(searches.c.id == search_id)).first()
        return Search.from_row(row) if row else None

    def chat_searches(self, chat_id: int) -> list["Search"]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(searches).where(searches.c.chat_id == chat_id).order_by(searches.c.id)
            ).all()
        return [Search.from_row(row) for row in rows]

    def all_searches(self) -> list["Search"]:
        with self.engine.connect() as db:
            rows = db.execute(select(searches).order_by(searches.c.id)).all()
        return [Search.from_row(row) for row in rows]

    def searches_added_on_dashboard(self) -> list["Search"]:
        """Searches added on the dashboard that the chat was not told about yet (marks them told)."""
        with self.engine.begin() as db:
            rows = db.execute(
                select(searches).where(searches.c.added_from == "dashboard", searches.c.announced_at.is_(None))
            ).all()
            if rows:
                db.execute(
                    update(searches)
                    .where(searches.c.id.in_([row.id for row in rows]))
                    .values(announced_at=utcnow())
                )
        return [Search.from_row(row) for row in rows]

    def active_searches(self) -> list["Search"]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(searches).where(searches.c.interval_minutes > 0).order_by(searches.c.id)
            ).all()
        return [Search.from_row(row) for row in rows]

    def stop_search(self, search_id: int) -> None:
        """Forget a search; the ad history is kept. A chat left without searches also forgets
        what it was told, so a search started again later alerts its results afresh."""
        search = self.get_search(search_id)
        if search is None:
            return
        with self.engine.begin() as db:
            db.execute(delete(search_ads).where(search_ads.c.search_id == search_id))
            db.execute(delete(searches).where(searches.c.id == search_id))
        if not self.chat_searches(search.chat_id):
            self.ads.clear_chat(search.chat_id)

    def stop_chat(self, chat_id: int) -> None:
        for search in self.chat_searches(chat_id):
            self.stop_search(search.id)
        self.ads.clear_chat(chat_id)
