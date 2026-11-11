"""Group reposts: ads judged to be the same car share `ads.car_id`, with the reasons in `ad_links`.

`car_id` is the token of the group's earliest ad. Linking two ads merges their whole groups.
"""

import datetime

from sqlalchemy import Connection, func, or_, select, update

from storage.db import upsert, utcnow
from storage.schema import ad_links, ads


def car_key(db: Connection, token: str) -> str | None:
    return db.execute(
        select(func.coalesce(ads.c.car_id, ads.c.token)).where(ads.c.token == token)
    ).scalar_one_or_none()


def group_tokens(db: Connection, token: str) -> list[str]:
    """All ads of the car this ad belongs to (itself included), oldest first."""
    key = car_key(db, token)
    if key is None:
        return []
    return list(
        db.execute(
            select(ads.c.token)
            .where(func.coalesce(ads.c.car_id, ads.c.token) == key)
            .order_by(ads.c.first_seen_at, ads.c.token)
        ).scalars()
    )


def link(db: Connection, first: str, second: str, reason: str, now: datetime.datetime | None = None) -> bool:
    """Record that two ads are the same car and merge their groups. True if anything changed."""
    if first == second:
        return False
    token, other = sorted((first, second))
    upsert(
        db,
        ad_links,
        {"token": token, "other": other, "reason": reason, "created_at": now or utcnow()},
        ["token", "other"],
        None,
    )
    keys = {car_key(db, first), car_key(db, second)}
    if None in keys or len(keys) == 1:
        return False
    members = db.execute(
        select(ads.c.token, ads.c.first_seen_at)
        .where(func.coalesce(ads.c.car_id, ads.c.token).in_(keys))
        .order_by(ads.c.first_seen_at, ads.c.token)
    ).all()
    root = members[0].token
    db.execute(
        update(ads)
        .where(ads.c.token.in_([member.token for member in members]))
        .values(car_id=root)
    )
    return True


def links_for(db: Connection, tokens: list[str]) -> list:
    return db.execute(
        select(ad_links).where(or_(ad_links.c.token.in_(tokens), ad_links.c.other.in_(tokens)))
    ).all()
