"""Global ad catalog: current values, change history, photo rows and per-chat alert state."""

import datetime

from sqlalchemy import Engine, and_, delete, func, select, update

from car.listing import CarListing, KnownAd
from storage.db import upsert, utcnow
from storage.schema import ad_snapshots, ads, chat_ads, photos, search_ads

# Attributes that change on their own and are not worth a history entry.
_VOLATILE_ATTRIBUTES = {
    "updated_at", "rebounced_at", "ends_at", "price_before", "tags",
    "km_since_last_test",  # follows km, which is tracked
    "specification",  # tracked through equipment, airbags, safety_points, pollution_level
    "final_score",  # Yad2's feed ranking score; drifts on every re-rank
}
_TRACKED_FIELDS = ("price", "km", "hand", "year", "title", "sub_model")


def is_badge_price(stored_price: int | None, listing: CarListing) -> bool:
    """True when a stored price is really Yad2's "dropped by" badge amount for this listing.

    The old card scraper read the badge (price before the drop minus the current price)
    as the price. The feed JSON gives both numbers, so such values can be recognised exactly.
    """
    price_before = listing.attributes.get("price_before")
    return (
        stored_price is not None
        and listing.price is not None
        and isinstance(price_before, int)
        and stored_price == price_before - listing.price
        and stored_price != listing.price
    )


def _listing_from_row(row, price: int | None = None) -> CarListing:
    return CarListing(
        url=row.url,
        title=row.title,
        price=row.price if price is None else price,
        km=row.km,
        hand=row.hand,
        year=row.year,
        listed_at=row.listed_at,
        manufacturer=row.manufacturer,
        model=row.model,
        sub_model=row.sub_model,
        attributes=row.attributes or {},
    )


class AdRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- reading ------------------------------------------------------------------------------

    def known_ads(self) -> dict[str, KnownAd]:
        with self.engine.connect() as db:
            rows = db.execute(select(ads.c.url, ads.c.price, ads.c.details_fetched_at)).all()
        return {row.url: KnownAd(row.price, row.details_fetched_at) for row in rows}

    def get(self, token: str) -> CarListing | None:
        with self.engine.connect() as db:
            row = db.execute(select(ads).where(ads.c.token == token)).first()
        return _listing_from_row(row) if row else None

    def chat_listings(self, chat_id: int) -> dict[str, CarListing]:
        """Ads this chat was alerted about, with the price it was last told."""
        with self.engine.connect() as db:
            rows = db.execute(
                select(ads, chat_ads.c.last_price)
                .join(chat_ads, chat_ads.c.token == ads.c.token)
                .where(chat_ads.c.chat_id == chat_id)
            ).all()
        return {row.url: _listing_from_row(row, row.last_price) for row in rows}

    def search_urls(self, search_id: int) -> frozenset[str]:
        """Ads that were in this search's results on its last scan and are not removed."""
        with self.engine.connect() as db:
            rows = db.execute(
                select(ads.c.url)
                .join(search_ads, search_ads.c.token == ads.c.token)
                .where(
                    search_ads.c.search_id == search_id,
                    search_ads.c.in_search.is_(True),
                    ads.c.removed_at.is_(None),
                )
            ).all()
        return frozenset(row.url for row in rows)

    def search_count(self, search_id: int) -> int:
        with self.engine.connect() as db:
            return db.execute(
                select(func.count()).select_from(search_ads).where(search_ads.c.search_id == search_id)
            ).scalar_one()

    def chat_count(self, chat_id: int) -> int:
        with self.engine.connect() as db:
            return db.execute(
                select(func.count()).select_from(chat_ads).where(chat_ads.c.chat_id == chat_id)
            ).scalar_one()

    # -- writing ------------------------------------------------------------------------------

    def record(self, listings: list[CarListing], now: datetime.datetime | None = None) -> None:
        """Upsert ads seen now, writing a history row for new ads and for changes."""
        now = now or utcnow()
        with self.engine.begin() as db:
            for listing in listings:
                old = db.execute(select(ads).where(ads.c.token == listing.token)).first()
                if old is None:
                    self._insert(db, listing, now)
                else:
                    self._update(db, old, listing, now)
                self._record_photos(db, listing, now)

    @staticmethod
    def _insert(db, listing: CarListing, now: datetime.datetime) -> None:
        db.execute(
            ads.insert().values(
                token=listing.token,
                url=listing.url,
                title=listing.title,
                price=listing.price,
                km=listing.km,
                hand=listing.hand,
                year=listing.year,
                manufacturer=listing.manufacturer,
                model=listing.model,
                sub_model=listing.sub_model,
                attributes=listing.attributes,
                raw=listing.raw,
                listed_at=listing.listed_at,
                first_seen_at=now,
                last_seen_at=now,
                details_fetched_at=now if listing.detailed else None,
                seller_phone=listing.seller_phone,
                seller_name=listing.seller_name,
                seller_phone_checked_at=now if listing.seller_phone else None,
            )
        )
        db.execute(
            ad_snapshots.insert().values(
                token=listing.token, observed_at=now, price=listing.price, km=listing.km
            )
        )

    @staticmethod
    def _update(db, old, listing: CarListing, now: datetime.datetime) -> None:
        changes = {}
        values = {}
        if is_badge_price(old.price, listing):
            # Saved by the old card scraper from the price-drop badge: correct it everywhere
            # without recording a price change.
            db.execute(
                update(ad_snapshots)
                .where(ad_snapshots.c.token == old.token, ad_snapshots.c.price == old.price)
                .values(price=listing.price)
            )
            db.execute(
                update(chat_ads)
                .where(chat_ads.c.token == old.token, chat_ads.c.last_price == old.price)
                .values(last_price=listing.price)
            )
            values["price"] = listing.price
        for name in _TRACKED_FIELDS:
            new_value = getattr(listing, name)
            old_value = getattr(old, name)
            if new_value is None:
                continue
            values[name] = new_value
            if name == "price" and is_badge_price(old_value, listing):
                continue
            if name == "title" and old_value and " ".join(old_value.split()) == " ".join(new_value.split()):
                continue  # only spacing differs between Yad2's card and JSON titles
            if old_value is not None and new_value != old_value:
                changes[name] = [old_value, new_value]
        old_attributes = old.attributes or {}
        for key, new_value in listing.attributes.items():
            old_value = old_attributes.get(key)
            if key not in _VOLATILE_ATTRIBUTES and old_value is not None and new_value != old_value:
                changes[key] = [old_value, new_value]
        values.update(
            attributes={**old_attributes, **listing.attributes},
            last_seen_at=now,
            removed_at=None,
            manufacturer=listing.manufacturer or old.manufacturer,
            model=listing.model or old.model,
            listed_at=listing.listed_at or old.listed_at,
        )
        if old.removed_at is not None:
            # Back on Yad2: whatever the sale follow-up concluded no longer holds.
            values.update(sale_status=None, sold_month=None, sold_to=None)
        if listing.seller_phone:
            values.update(seller_phone=listing.seller_phone, seller_phone_checked_at=now)
        if listing.seller_name:
            values["seller_name"] = listing.seller_name
        if listing.detailed:
            values.update(raw=listing.raw, details_fetched_at=now)
        elif old.raw is None:
            values["raw"] = listing.raw
        db.execute(update(ads).where(ads.c.token == old.token).values(**values))
        if changes or old.removed_at is not None:
            if old.removed_at is not None:
                changes["relisted"] = [old.removed_at.isoformat(), now.isoformat()]
            db.execute(
                ad_snapshots.insert().values(
                    token=old.token,
                    observed_at=now,
                    price=values.get("price", old.price),
                    km=values.get("km", old.km),
                    changes=changes,
                )
            )

    @staticmethod
    def _record_photos(db, listing: CarListing, now: datetime.datetime) -> None:
        if not listing.images:
            return
        upsert(
            db,
            photos,
            [
                {
                    "token": listing.token,
                    "position": position,
                    "source_url": url,
                    "first_seen_at": now,
                    "download_attempts": 0,
                }
                for position, url in enumerate(listing.images)
            ],
            ["token", "source_url"],
            lambda excluded: {"position": excluded.position, "removed_from_ad_at": None},
        )
        db.execute(
            update(photos)
            .where(
                photos.c.token == listing.token,
                photos.c.source_url.not_in(listing.images),
                photos.c.removed_from_ad_at.is_(None),
            )
            .values(removed_from_ad_at=now)
        )

    def mark_removed(self, urls: list[str], now: datetime.datetime | None = None) -> None:
        if not urls:
            return
        now = now or utcnow()
        with self.engine.begin() as db:
            for url in urls:
                token = url.rstrip("/").rsplit("/", 1)[-1]
                result = db.execute(
                    update(ads)
                    .where(ads.c.token == token, ads.c.removed_at.is_(None))
                    .values(removed_at=now)
                )
                if result.rowcount:
                    db.execute(
                        ad_snapshots.insert().values(
                            token=token, observed_at=now, changes={"removed": [None, now.isoformat()]}
                        )
                    )

    def save_chat(
        self,
        chat_id: int,
        in_results: list[CarListing],
        now: datetime.datetime | None = None,
    ) -> None:
        """Remember the prices this chat was told (shared by all the chat's searches)."""
        now = now or utcnow()
        if not in_results:
            return
        with self.engine.begin() as db:
            upsert(
                db,
                chat_ads,
                [
                    {"chat_id": chat_id, "token": listing.token, "last_price": listing.price, "first_seen_at": now}
                    for listing in in_results
                ],
                ["chat_id", "token"],
                lambda excluded: {
                    "last_price": func.coalesce(excluded.last_price, chat_ads.c.last_price),
                },
            )

    def save_search(
        self,
        search_id: int,
        in_results: list[CarListing],
        out_of_results: list[str] = (),
        now: datetime.datetime | None = None,
    ) -> None:
        """Remember which ads are in this search's results."""
        now = now or utcnow()
        with self.engine.begin() as db:
            if in_results:
                upsert(
                    db,
                    search_ads,
                    [
                        {"search_id": search_id, "token": listing.token, "in_search": True, "first_seen_at": now}
                        for listing in in_results
                    ],
                    ["search_id", "token"],
                    lambda excluded: {"in_search": True},
                )
            tokens = [url.rstrip("/").rsplit("/", 1)[-1] for url in out_of_results]
            if tokens:
                db.execute(
                    update(search_ads)
                    .where(and_(search_ads.c.search_id == search_id, search_ads.c.token.in_(tokens)))
                    .values(in_search=False)
                )

    def clear_chat(self, chat_id: int) -> None:
        with self.engine.begin() as db:
            db.execute(delete(chat_ads).where(chat_ads.c.chat_id == chat_id))

    # -- photos -------------------------------------------------------------------------------

    @staticmethod
    def _missing_contact(max_attempts: int):
        return and_(
            ads.c.removed_at.is_(None),
            ads.c.seller_phone.is_(None) | ads.c.seller_name.is_(None),
            ads.c.seller_phone_attempts < max_attempts,
        )

    def ads_without_phone(self, limit: int = 20, max_attempts: int = 3) -> list[tuple[str, str]]:
        """(token, url) of ads still on Yad2 missing the seller's number or name, newest first."""
        with self.engine.connect() as db:
            rows = db.execute(
                select(ads.c.token, ads.c.url)
                .where(self._missing_contact(max_attempts))
                .order_by(ads.c.first_seen_at.desc())
                .limit(limit)
            ).all()
        return [(row.token, row.url) for row in rows]

    def count_without_phone(self, max_attempts: int = 3) -> int:
        with self.engine.connect() as db:
            return db.execute(
                select(func.count()).select_from(ads).where(self._missing_contact(max_attempts))
            ).scalar_one()

    def save_phone(self, token: str, phone: str | None, name: str | None = None,
                   now: datetime.datetime | None = None, attempts: int = 1) -> None:
        """Record a lookup: what was found; a lookup that missed the number or the name counts
        `attempts` more tries, so an ad without one isn't opened forever."""
        now = now or utcnow()
        values = {"seller_phone_checked_at": now}
        if phone:
            values["seller_phone"] = phone
        if name:
            values["seller_name"] = name
        if not (phone and name):
            values["seller_phone_attempts"] = ads.c.seller_phone_attempts + attempts
        with self.engine.begin() as db:
            db.execute(update(ads).where(ads.c.token == token).values(**values))

    def pending_photos(self, limit: int = 50, max_attempts: int = 5) -> list[tuple[int, str]]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(photos.c.id, photos.c.source_url)
                .where(photos.c.sha256.is_(None), photos.c.download_attempts < max_attempts)
                .order_by(photos.c.id)
                .limit(limit)
            ).all()
        return [(row.id, row.source_url) for row in rows]

    def pending_photo_count(self, max_attempts: int = 5) -> int:
        with self.engine.connect() as db:
            return db.execute(
                select(func.count())
                .select_from(photos)
                .where(photos.c.sha256.is_(None), photos.c.download_attempts < max_attempts)
            ).scalar_one()

    def photo_stored(self, photo_id: int, sha256: str, path: str, size_bytes: int) -> None:
        with self.engine.begin() as db:
            db.execute(
                update(photos)
                .where(photos.c.id == photo_id)
                .values(
                    sha256=sha256,
                    path=path,
                    size_bytes=size_bytes,
                    download_attempts=photos.c.download_attempts + 1,
                )
            )

    def photo_failed(self, photo_id: int) -> None:
        with self.engine.begin() as db:
            db.execute(
                update(photos)
                .where(photos.c.id == photo_id)
                .values(download_attempts=photos.c.download_attempts + 1)
            )
