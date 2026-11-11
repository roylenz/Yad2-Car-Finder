"""Read-only queries behind the dashboard pages."""

import datetime
from dataclasses import dataclass

from sqlalchemy import Engine, and_, func, or_, select, true

from storage.db import utcnow
from tracking.tracker import price_floor, search_max_price
from storage.schema import ad_snapshots, ads, analysis_results, chat_ads, photos, scans, searches

SORTS = {
    "seen": ads.c.first_seen_at.desc(),
    "listed": func.coalesce(ads.c.listed_at, ads.c.first_seen_at).asc(),
    "price": ads.c.price.asc(),
    "price_desc": ads.c.price.desc(),
    "km": ads.c.km.asc(),
    "year": ads.c.year.desc(),
}
# Sorts that need several columns.
MULTI_SORTS = {
    "model": (ads.c.manufacturer, ads.c.model, ads.c.sub_model, ads.c.price),
}


def days_listed(row, now: datetime.datetime | None = None) -> int:
    start = row.listed_at or row.first_seen_at
    end = row.removed_at or now or utcnow()
    return max((end - start).days, 0)


@dataclass
class Overview:
    active: int
    new_today: int
    drops_today: int
    removed_week: int
    median_days_to_remove: int | None
    photos: int
    photo_bytes: int


def overview(engine: Engine) -> Overview:
    now = utcnow()
    day, week = now - datetime.timedelta(days=1), now - datetime.timedelta(days=7)
    with engine.connect() as db:
        count = lambda *where: db.execute(  # noqa: E731
            select(func.count()).select_from(ads).where(*where)
        ).scalar_one()
        price_changes = db.execute(
            select(ad_snapshots.c.changes).where(
                ad_snapshots.c.observed_at >= day, ad_snapshots.c.changes.is_not(None)
            )
        ).scalars()
        drops = sum(
            1 for changes in price_changes if "price" in changes and changes["price"][1] < changes["price"][0]
        )
        removed = db.execute(
            select(ads.c.listed_at, ads.c.first_seen_at, ads.c.removed_at).where(
                ads.c.removed_at.is_not(None)
            )
        ).all()
        photo_count, photo_bytes = db.execute(
            select(func.count(func.distinct(photos.c.sha256)), func.coalesce(func.sum(photos.c.size_bytes), 0))
            .where(photos.c.sha256.is_not(None))
        ).one()
        result = Overview(
            active=count(ads.c.removed_at.is_(None)),
            new_today=count(ads.c.first_seen_at >= day),
            drops_today=drops,
            removed_week=count(ads.c.removed_at >= week),
            median_days_to_remove=None,
            photos=photo_count,
            photo_bytes=photo_bytes,
        )
    durations = sorted(days_listed(row) for row in removed)
    if durations:
        result.median_days_to_remove = durations[len(durations) // 2]
    return result


def recent_events(engine: Engine, limit: int = 30) -> list:
    with engine.connect() as db:
        return db.execute(
            select(ad_snapshots, ads.c.title, ads.c.year)
            .join(ads, ads.c.token == ad_snapshots.c.token)
            .order_by(ad_snapshots.c.observed_at.desc(), ad_snapshots.c.id.desc())
            .limit(limit)
        ).all()


def manufacturers(engine: Engine) -> list[str]:
    with engine.connect() as db:
        return [
            value
            for value in db.execute(
                select(ads.c.manufacturer).distinct().order_by(ads.c.manufacturer)
            ).scalars()
            if value
        ]


def chat_ids(engine: Engine) -> list[int]:
    with engine.connect() as db:
        return list(db.execute(select(chat_ads.c.chat_id).distinct()).scalars())


def _cover_photo():
    return (
        select(photos.c.sha256)
        .where(photos.c.token == ads.c.token, photos.c.sha256.is_not(None))
        .order_by(photos.c.removed_from_ad_at.is_not(None), photos.c.position)
        .limit(1)
        .scalar_subquery()
    )


def list_ads(
    engine: Engine,
    status: str = "active",
    manufacturer: str | None = None,
    query: str | None = None,
    model: str | None = None,
    sub_model: str | None = None,
    chat_id: int | None = None,
    sort: str = "seen",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list, int]:
    conditions = []
    if status == "active":
        conditions.append(ads.c.removed_at.is_(None))
    elif status == "removed":
        conditions.append(ads.c.removed_at.is_not(None))
    if manufacturer:
        conditions.append(ads.c.manufacturer == manufacturer)
    if model:
        conditions.append(ads.c.model == model)
    if sub_model:
        conditions.append(ads.c.sub_model == sub_model)
    if query:
        pattern = f"%{query}%"
        conditions.append(
            or_(ads.c.title.like(pattern), ads.c.sub_model.like(pattern), ads.c.token == query)
        )
    if chat_id is not None:
        conditions.append(
            ads.c.token.in_(select(chat_ads.c.token).where(chat_ads.c.chat_id == chat_id))
        )
    where = and_(true(), *conditions)
    first_price = (
        select(ad_snapshots.c.price)
        .where(ad_snapshots.c.token == ads.c.token, ad_snapshots.c.price.is_not(None))
        .order_by(ad_snapshots.c.observed_at)
        .limit(1)
        .scalar_subquery()
    )
    with engine.connect() as db:
        total = db.execute(select(func.count()).select_from(ads).where(where)).scalar_one()
        rows = db.execute(
            select(ads, _cover_photo().label("cover"), first_price.label("first_price"))
            .where(where)
            .order_by(*_order(sort), ads.c.token)
            .limit(limit)
            .offset(offset)
        ).all()
    return rows, total


def ad_detail(engine: Engine, token: str):
    with engine.connect() as db:
        ad = db.execute(select(ads).where(ads.c.token == token)).first()
        if ad is None:
            return None
        history = db.execute(
            select(ad_snapshots)
            .where(ad_snapshots.c.token == token)
            .order_by(ad_snapshots.c.observed_at, ad_snapshots.c.id)
        ).all()
        ad_photos = db.execute(
            select(photos).where(photos.c.token == token).order_by(photos.c.position, photos.c.id)
        ).all()
        hashes = [photo.sha256 for photo in ad_photos if photo.sha256]
        results = db.execute(
            select(analysis_results)
            .where(
                or_(
                    and_(analysis_results.c.target_type == "ad", analysis_results.c.target_id == token),
                    and_(
                        analysis_results.c.target_type == "photo",
                        analysis_results.c.target_id.in_(hashes),
                    ),
                )
            )
            .order_by(analysis_results.c.processor, analysis_results.c.version.desc())
        ).all()
        chats = list(
            db.execute(select(chat_ads.c.chat_id).where(chat_ads.c.token == token)).scalars()
        )
    return ad, history, ad_photos, results, chats


def _order(sort: str) -> list:
    if sort in MULTI_SORTS:
        return [column.asc().nulls_last() for column in MULTI_SORTS[sort]]
    return [SORTS.get(sort, SORTS["seen"]).nulls_last()]


def ignored_price_floor(engine: Engine) -> int:
    """Asking prices below this are placeholders or lease payments (see tracking.tracker)."""
    with engine.connect() as db:
        urls = db.execute(select(searches.c.url)).scalars().all()
        maxima = [price for price in map(search_max_price, urls) if price]
        if maxima:
            return price_floor(max(maxima))
        highest = db.execute(
            select(func.max(ads.c.price)).where(ads.c.removed_at.is_(None))
        ).scalar_one()
    return price_floor(None, [highest])


def market_points(
    engine: Engine, manufacturer: str | None, model: str | None, sub_model: str | None = None
) -> list:
    conditions = [ads.c.price.is_not(None), ads.c.price >= ignored_price_floor(engine)]
    if manufacturer:
        conditions.append(ads.c.manufacturer == manufacturer)
    if model:
        conditions.append(ads.c.model == model)
    if sub_model:
        conditions.append(ads.c.sub_model == sub_model)
    with engine.connect() as db:
        return db.execute(
            select(
                ads.c.token, ads.c.title, ads.c.price, ads.c.km, ads.c.year, ads.c.hand,
                ads.c.manufacturer, ads.c.model, ads.c.sub_model,
                ads.c.listed_at, ads.c.first_seen_at, ads.c.removed_at,
            ).where(*conditions)
        ).all()


def models(engine: Engine, manufacturer: str | None) -> list[str]:
    statement = select(ads.c.model).distinct().order_by(ads.c.model)
    if manufacturer:
        statement = statement.where(ads.c.manufacturer == manufacturer)
    with engine.connect() as db:
        return [value for value in db.execute(statement).scalars() if value]


def photo_key(engine: Engine, sha256: str) -> str | None:
    with engine.connect() as db:
        return db.execute(
            select(photos.c.path).where(photos.c.sha256 == sha256).limit(1)
        ).scalar_one_or_none()


def sub_models(engine: Engine, manufacturer: str | None, model: str | None) -> list[str]:
    statement = select(ads.c.sub_model).distinct().order_by(ads.c.sub_model)
    if manufacturer:
        statement = statement.where(ads.c.manufacturer == manufacturer)
    if model:
        statement = statement.where(ads.c.model == model)
    with engine.connect() as db:
        return [value for value in db.execute(statement).scalars() if value]


def _median(values: list) -> int | None:
    values = sorted(value for value in values if value is not None)
    return values[len(values) // 2] if values else None


@dataclass
class Group:
    name: str
    manufacturer: str | None
    model: str | None
    sub_model: str | None
    points: list
    active: int
    removed: int
    min_price: int
    median_price: int
    max_price: int
    median_km: int | None
    median_year: int | None
    median_days: int | None


def group_points(rows: list, by_sub_model: bool) -> list[Group]:
    """Group market rows by subtype (or by model), cheapest median first."""
    buckets: dict[tuple, list] = {}
    for row in rows:
        if by_sub_model:
            key = (row.manufacturer, row.model, row.sub_model or "Unknown subtype")
        else:
            key = (row.manufacturer, row.model, None)
        buckets.setdefault(key, []).append(row)
    groups = []
    for (manufacturer, model, sub_model), members in buckets.items():
        prices = [row.price for row in members]
        groups.append(
            Group(
                name=sub_model if by_sub_model else " ".join(p for p in (manufacturer, model) if p),
                manufacturer=manufacturer,
                model=model,
                sub_model=sub_model,
                points=members,
                active=sum(1 for row in members if row.removed_at is None),
                removed=sum(1 for row in members if row.removed_at is not None),
                min_price=min(prices),
                median_price=_median(prices),
                max_price=max(prices),
                median_km=_median([row.km for row in members]),
                median_year=_median([row.year for row in members]),
                median_days=_median([days_listed(row) for row in members if row.removed_at]),
            )
        )
    return sorted(groups, key=lambda group: (group.median_price, group.name))


@dataclass
class SearchStatus:
    chat_id: int
    url: str
    interval_minutes: int
    next_scan_at: datetime.datetime | None
    active: object | None  # the queued or running scan, if any
    last: object | None  # the latest finished scan


def search_statuses(engine: Engine) -> list[SearchStatus]:
    """Every saved search with its next scheduled scan, current scan and latest result."""
    with engine.connect() as db:
        rows = db.execute(select(searches).order_by(searches.c.chat_id)).all()
        statuses = []
        for row in rows:
            latest = db.execute(
                select(scans)
                .where(scans.c.chat_id == row.chat_id)
                .order_by(scans.c.id.desc())
                .limit(20)
            ).all()
            active = next((scan for scan in latest if scan.status in ("queued", "running")), None)
            last = next(
                (scan for scan in latest if scan.status not in ("queued", "running")), None
            )
            statuses.append(
                SearchStatus(
                    row.chat_id, row.url, row.interval_minutes, row.next_scan_at, active, last
                )
            )
    return statuses


def scan_history(engine: Engine, limit: int = 200, chat_id: int | None = None) -> list:
    statement = select(scans).order_by(scans.c.id.desc()).limit(limit)
    if chat_id is not None:
        statement = statement.where(scans.c.chat_id == chat_id)
    with engine.connect() as db:
        return db.execute(statement).all()


def scan_counts(engine: Engine, since: datetime.datetime) -> dict[str, int]:
    with engine.connect() as db:
        rows = db.execute(
            select(scans.c.status, func.count())
            .where(scans.c.queued_at >= since)
            .group_by(scans.c.status)
        ).all()
    return {status: count for status, count in rows}
