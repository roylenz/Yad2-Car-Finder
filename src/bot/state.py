"""Saved searches, access control, and the per-chat view of the ad catalog."""

from sqlalchemy import delete, insert, literal, select, update

from car.listing import CarListing
from storage.ads import AdRepository
from storage.db import default_database_url, get_engine, migrate, sqlite_url, upsert
from storage.schema import access, access_meta, access_requests, admins
from storage.searches import Search, SearchStore  # noqa: F401 (Search re-exported)


class BotState(SearchStore):
    def __init__(self, path: str | None = None, database_url: str | None = None) -> None:
        url = database_url or (sqlite_url(path) if path else default_database_url())
        migrate(url)
        self.engine = get_engine(url)
        self.ads = AdRepository(self.engine)

    def previous_listings(self, chat_id: int) -> dict[str, CarListing]:
        return self.ads.chat_listings(chat_id)

    def save_listings(self, search: "Search", listings: list[CarListing]) -> None:
        self.ads.record(listings)
        self.ads.save_chat(search.chat_id, listings)
        self.ads.save_search(search.id, listings)

    def seen_count(self, search_id: int) -> int:
        return self.ads.search_count(search_id)

    def access_override(self, user_id: int) -> bool | None:
        with self.engine.connect() as db:
            row = db.execute(select(access.c.allowed).where(access.c.user_id == user_id)).first()
        return bool(row.allowed) if row else None

    def set_access(self, user_id: int, allowed: bool) -> None:
        with self.engine.begin() as db:
            upsert(db, access, {"user_id": user_id, "allowed": int(allowed)}, ["user_id"], ["allowed"])

    def access_overrides(self) -> dict[int, bool]:
        with self.engine.connect() as db:
            rows = db.execute(select(access.c.user_id, access.c.allowed)).all()
        return {row.user_id: bool(row.allowed) for row in rows}

    def claim_first_admin(self, user_id: int, configured_admins: bool) -> bool:
        """Atomically claim bootstrap once, even if two users message together."""
        with self.engine.begin() as db:
            claimed = db.execute(
                insert(access_meta)
                .from_select(
                    ["key"],
                    select(literal("bootstrapped")).where(
                        ~select(access_meta.c.key)
                        .where(access_meta.c.key == "bootstrapped")
                        .exists()
                    ),
                )
            ).rowcount
            if configured_admins or not claimed:
                return False
            upsert(db, admins, {"user_id": user_id}, ["user_id"])
            return True

    def is_admin(self, user_id: int) -> bool:
        with self.engine.connect() as db:
            return db.execute(select(admins).where(admins.c.user_id == user_id)).first() is not None

    def admin_ids(self) -> set[int]:
        with self.engine.connect() as db:
            return {row.user_id for row in db.execute(select(admins.c.user_id))}

    def set_admin(self, user_id: int, admin: bool) -> None:
        with self.engine.begin() as db:
            if admin:
                upsert(db, admins, {"user_id": user_id}, ["user_id"])
            else:
                db.execute(delete(admins).where(admins.c.user_id == user_id))

    def request_access(self, user_id: int, name: str) -> None:
        with self.engine.begin() as db:
            upsert(
                db,
                access_requests,
                {"user_id": user_id, "name": name, "status": "pending"},
                ["user_id"],
                ["name", "status"],
            )

    def access_request(self, user_id: int) -> tuple[str, str] | None:
        with self.engine.connect() as db:
            row = db.execute(
                select(access_requests.c.name, access_requests.c.status).where(
                    access_requests.c.user_id == user_id
                )
            ).first()
        return tuple(row) if row else None

    def pending_requests(self) -> list[tuple[int, str]]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(access_requests.c.user_id, access_requests.c.name)
                .where(access_requests.c.status == "pending")
                .order_by(access_requests.c.user_id)
            ).all()
        return [tuple(row) for row in rows]

    def resolve_request(self, user_id: int, approved: bool) -> bool:
        with self.engine.begin() as db:
            result = db.execute(
                update(access_requests)
                .where(access_requests.c.user_id == user_id, access_requests.c.status == "pending")
                .values(status="approved" if approved else "denied")
            )
            if result.rowcount and approved:
                upsert(db, access, {"user_id": user_id, "allowed": 1}, ["user_id"], ["allowed"])
            return result.rowcount > 0

