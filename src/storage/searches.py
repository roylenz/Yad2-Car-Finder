"""Saved searches. A chat can have several; each is scanned on its own schedule.

A search can have extra Yad2 links. Each link is a search row with `parent_id` set: it is
scanned like any search, follows its parent's interval and is stopped with it, and the
dashboard counts its ads as the parent search's."""

from dataclasses import dataclass

from sqlalchemy import Engine, delete, insert, or_, select, update

from car.search_url import describe_search
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
    parent_id: int | None = None  # set on an extra link of another search
    name: str | None = None

    @property
    def label(self) -> str:
        """The name given to the search, else a description of its link."""
        return self.name or describe_search(self.url)

    @classmethod
    def from_row(cls, row) -> "Search":
        return cls(row.id, row.chat_id, row.url, row.interval_minutes, row.parent_id, row.name)


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
                return Search(existing.id, chat_id, url, interval_minutes, existing.parent_id, existing.name), False
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

    def add_link(self, search_id: int, url: str, added_from: str = "dashboard") -> "Search":
        """Add another Yad2 link to a search. Raises ValueError when the search is gone or its
        chat already tracks the link."""
        parent = self.get_search(search_id)
        if parent is None:
            raise ValueError("That search was stopped.")
        if parent.parent_id is not None:
            parent = self.get_search(parent.parent_id) or parent
        if url in self.chat_urls(parent.chat_id):
            raise ValueError("That chat already tracks this link.")
        with self.engine.begin() as db:
            link_id = db.execute(
                insert(searches).values(
                    chat_id=parent.chat_id,
                    url=url,
                    interval_minutes=parent.interval_minutes,
                    created_at=utcnow(),
                    added_from=added_from,
                    parent_id=parent.id,
                )
            ).inserted_primary_key[0]
        return Search(link_id, parent.chat_id, url, parent.interval_minutes, parent.id)

    def links(self, search_id: int) -> list["Search"]:
        """The extra links of a search."""
        with self.engine.connect() as db:
            rows = db.execute(
                select(searches).where(searches.c.parent_id == search_id).order_by(searches.c.id)
            ).all()
        return [Search.from_row(row) for row in rows]

    def main_search(self, search: "Search") -> "Search":
        """The search a link belongs to (the search itself when it is not a link)."""
        if search.parent_id is None:
            return search
        return self.get_search(search.parent_id) or search

    def chat_searches(self, chat_id: int) -> list["Search"]:
        """The chat's searches, without their extra links."""
        with self.engine.connect() as db:
            rows = db.execute(
                select(searches)
                .where(searches.c.chat_id == chat_id, searches.c.parent_id.is_(None))
                .order_by(searches.c.id)
            ).all()
        return [Search.from_row(row) for row in rows]

    def chat_urls(self, chat_id: int) -> set[str]:
        """Every link the chat tracks: its searches' and their extra links'."""
        with self.engine.connect() as db:
            return set(db.execute(select(searches.c.url).where(searches.c.chat_id == chat_id)).scalars())

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

    def set_interval(self, search_id: int, interval_minutes: int) -> "Search | None":
        """Change how often a search (and its extra links) is scanned; the bot reschedules it on
        its next sync."""
        with self.engine.begin() as db:
            db.execute(
                update(searches)
                .where(or_(searches.c.id == search_id, searches.c.parent_id == search_id))
                .values(interval_minutes=interval_minutes)
            )
        return self.get_search(search_id)

    def set_url(self, search_id: int, url: str) -> "Search":
        """Change a search's (or extra link's) Yad2 link; its next scan uses it. Raises
        ValueError when the search is gone or its chat already tracks that link."""
        search = self.get_search(search_id)
        if search is None:
            raise ValueError("That search was stopped.")
        if url != search.url:
            if url in self.chat_urls(search.chat_id):
                raise ValueError("That chat already tracks a search with these filters.")
            with self.engine.begin() as db:
                db.execute(update(searches).where(searches.c.id == search_id).values(url=url))
        return self.get_search(search_id) or search

    def rename(self, search_id: int, name: str) -> "Search | None":
        """Name a search; an empty name goes back to describing it from its link."""
        with self.engine.begin() as db:
            db.execute(update(searches).where(searches.c.id == search_id).values(name=name.strip() or None))
        return self.get_search(search_id)

    def stop_search(self, search_id: int) -> None:
        """Forget a search and its extra links (or just the one link); the ad history is kept.
        A chat left without searches also forgets what it was told, so a search started again
        later alerts its results afresh."""
        search = self.get_search(search_id)
        if search is None:
            return
        ids = [search_id, *(link.id for link in self.links(search_id))]
        with self.engine.begin() as db:
            db.execute(delete(search_ads).where(search_ads.c.search_id.in_(ids)))
            db.execute(delete(searches).where(searches.c.id.in_(ids)))
        if not self.chat_searches(search.chat_id):
            self.ads.clear_chat(search.chat_id)

    def stop_chat(self, chat_id: int) -> None:
        for search in self.chat_searches(chat_id):
            self.stop_search(search.id)
        self.ads.clear_chat(chat_id)
