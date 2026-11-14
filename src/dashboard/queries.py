"""Read-only queries behind the dashboard pages."""

import datetime
from dataclasses import dataclass

from sqlalchemy import Engine, and_, case, exists, func, or_, select, true

from storage import cars
from storage.db import utcnow
from tracking.tracker import price_floor, search_max_price
from car.search_url import describe_search
from storage.schema import (
    access, ad_snapshots, admins, ads, analysis_results, chat_ads, photos, scans, search_ads, searches,
)

def _car_key(table):
    return func.coalesce(table.c.car_id, table.c.token)


def _market_since():
    """When the car first went on the market: its earliest ad, counting reposts."""
    other = ads.alias("same_car")
    return (
        select(func.min(func.coalesce(other.c.listed_at, other.c.first_seen_at)))
        .where(_car_key(other) == _car_key(ads))
        .scalar_subquery()
    )


def _in_search(search_id: int | None) -> list:
    """Conditions keeping only cars a search found (any of the car's ads); none for all searches."""
    if search_id is None:
        return []
    found = ads.alias("found")
    return [
        _car_key(ads).in_(
            select(_car_key(found))
            .join(search_ads, search_ads.c.token == found.c.token)
            .where(search_ads.c.search_id == search_id)
        )
    ]


def _repost_count():
    other = ads.alias("repost")
    return (
        select(func.count() - 1).where(_car_key(other) == _car_key(ads)).scalar_subquery()
    )


def _latest_of_car():
    """Only the newest ad of each car (older ads of a reposted car are folded into it)."""
    newer = ads.alias("newer")
    return ~exists().where(
        _car_key(newer) == _car_key(ads),
        or_(
            newer.c.first_seen_at > ads.c.first_seen_at,
            and_(newer.c.first_seen_at == ads.c.first_seen_at, newer.c.token > ads.c.token),
        ),
    )


def _first_price(floor: int):
    """The car's first real asking price, across all its ads (reposts included)."""
    owner = ads.alias("first_ad")
    return (
        select(ad_snapshots.c.price)
        .join(owner, owner.c.token == ad_snapshots.c.token)
        .where(_car_key(owner) == _car_key(ads), ad_snapshots.c.price >= floor)
        .order_by(ad_snapshots.c.observed_at, ad_snapshots.c.id)
        .limit(1)
        .scalar_subquery()
    )


def _price_drop(floor: int):
    """Shekels the asking price fell since the car first went on the market (negative: rose)."""
    return case((ads.c.price >= floor, _first_price(floor) - ads.c.price), else_=None)


def _drop_count(floor: int):
    """How many times the asking price was lowered, across all the car's ads."""
    owner = ads.alias("drop_ad")
    old = func.json_extract(ad_snapshots.c.changes, "$.price[0]")
    new = func.json_extract(ad_snapshots.c.changes, "$.price[1]")
    drops_within_ads = (
        select(func.count())
        .select_from(ad_snapshots.join(owner, owner.c.token == ad_snapshots.c.token))
        .where(_car_key(owner) == _car_key(ads), new < old, new >= floor)
        .scalar_subquery()
    )
    # A repost that starts lower than the car's previous ad last asked counts as a drop too.
    previous, later = ads.alias("previous_ad"), ads.alias("later_ad")
    later_first = (
        select(ad_snapshots.c.price)
        .where(ad_snapshots.c.token == later.c.token, ad_snapshots.c.price >= floor)
        .order_by(ad_snapshots.c.observed_at, ad_snapshots.c.id)
        .limit(1)
        .scalar_subquery()
    )
    previous_last = (
        select(ad_snapshots.c.price)
        .where(ad_snapshots.c.token == previous.c.token, ad_snapshots.c.price >= floor)
        .order_by(ad_snapshots.c.observed_at.desc(), ad_snapshots.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    earlier = ads.alias("earlier_ad")
    closest_previous = (
        select(func.max(earlier.c.first_seen_at))
        .where(_car_key(earlier) == _car_key(later), earlier.c.first_seen_at < later.c.first_seen_at)
        .scalar_subquery()
    )
    drops_between_ads = (
        select(func.count())
        .select_from(later.join(previous, _car_key(previous) == _car_key(later)))
        .where(
            _car_key(later) == _car_key(ads),
            previous.c.first_seen_at == closest_previous,
            later_first < previous_last,
        )
        .scalar_subquery()
    )
    return drops_within_ads + drops_between_ads


SORTS = {
    # Days on the market, counted from the car's first ad.
    "seen": _market_since().desc(),
    "listed": _market_since().asc(),
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
    start = getattr(row, "market_since", None) or row.listed_at or row.first_seen_at
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


def overview(engine: Engine, search_id: int | None = None) -> Overview:
    now = utcnow()
    day, week = now - datetime.timedelta(days=1), now - datetime.timedelta(days=7)
    scope = _in_search(search_id)
    scoped_tokens = select(ads.c.token).where(*scope)
    with engine.connect() as db:
        count = lambda *where: db.execute(  # noqa: E731
            select(func.count()).select_from(ads).where(*where, *scope)
        ).scalar_one()
        price_changes = db.execute(
            select(ad_snapshots.c.changes).where(
                ad_snapshots.c.observed_at >= day, ad_snapshots.c.changes.is_not(None),
                *([ad_snapshots.c.token.in_(scoped_tokens)] if scope else []),
            )
        ).scalars()
        drops = sum(
            1 for changes in price_changes if "price" in changes and changes["price"][1] < changes["price"][0]
        )
        removed = db.execute(
            select(ads.c.listed_at, ads.c.first_seen_at, ads.c.removed_at).where(
                ads.c.removed_at.is_not(None), *scope
            )
        ).all()
        photo_count, photo_bytes = db.execute(
            select(func.count(func.distinct(photos.c.sha256)), func.coalesce(func.sum(photos.c.size_bytes), 0))
            .where(photos.c.sha256.is_not(None), *([photos.c.token.in_(scoped_tokens)] if scope else []))
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


def recent_events(engine: Engine, limit: int = 30, search_id: int | None = None) -> list:
    with engine.connect() as db:
        return db.execute(
            select(ad_snapshots, ads.c.title, ads.c.year)
            .join(ads, ads.c.token == ad_snapshots.c.token)
            .where(*_in_search(search_id))
            .order_by(ad_snapshots.c.observed_at.desc(), ad_snapshots.c.id.desc())
            .limit(limit)
        ).all()


def manufacturers(engine: Engine, search_id: int | None = None) -> list[str]:
    with engine.connect() as db:
        return [
            value
            for value in db.execute(
                select(ads.c.manufacturer).distinct().where(*_in_search(search_id)).order_by(ads.c.manufacturer)
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
    search_id: int | None = None,
) -> tuple[list, int]:
    conditions = [_latest_of_car(), *_in_search(search_id)]
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
        digits = query.replace("-", "").strip()
        conditions.append(
            or_(
                ads.c.title.like(pattern),
                ads.c.sub_model.like(pattern),
                ads.c.token == query,
                ads.c.plate == digits,
            )
        )
    if chat_id is not None:
        conditions.append(
            ads.c.token.in_(select(chat_ads.c.token).where(chat_ads.c.chat_id == chat_id))
        )
    where = and_(true(), *conditions)
    floor = ignored_price_floor(engine)
    with engine.connect() as db:
        total = db.execute(select(func.count()).select_from(ads).where(where)).scalar_one()
        rows = db.execute(
            select(
                ads,
                _cover_photo().label("cover"),
                _price_drop(floor).label("price_drop"),
                _drop_count(floor).label("drops"),
                _market_since().label("market_since"),
                _repost_count().label("reposts"),
            )
            .where(where)
            .order_by(*_order(sort, floor), ads.c.token)
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
        group = cars.group_tokens(db, token)
        linked = db.execute(
            select(ads.c.token, ads.c.title, ads.c.price, ads.c.first_seen_at, ads.c.removed_at)
            .where(ads.c.token.in_(group), ads.c.token != token)
            .order_by(ads.c.first_seen_at)
        ).all()
        reasons: dict[str, list[str]] = {}
        for link in cars.links_for(db, group):
            reasons.setdefault(link.token, []).append(link.reason)
            reasons.setdefault(link.other, []).append(link.reason)
        same_car = [
            {**row._asdict(), "reasons": sorted(set(reasons.get(row.token, [])))} for row in linked
        ]
        if linked:
            # The car's history across all its ads, oldest first.
            history = db.execute(
                select(ad_snapshots)
                .where(ad_snapshots.c.token.in_(group))
                .order_by(ad_snapshots.c.observed_at, ad_snapshots.c.id)
            ).all()
    return ad, history, ad_photos, results, chats, same_car


def latest_result(results: list, processor: str) -> dict | None:
    """The newest successful result of a processor among an ad's analysis rows."""
    rows = [row for row in results if row.processor == processor and row.status == "ok"]
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row.version) if row.version.isdigit() else 0, row.created_at)).result


def condition_summaries(engine: Engine, tokens: list[str]) -> dict[str, dict]:
    """Claude condition results for a page of ads: {token: {condition, damage, high_impact}}."""
    if not tokens:
        return {}
    with engine.connect() as db:
        rows = db.execute(
            select(analysis_results.c.target_id, analysis_results.c.result)
            .where(
                analysis_results.c.target_type == "ad",
                analysis_results.c.processor == "claude_condition",
                analysis_results.c.status == "ok",
                analysis_results.c.target_id.in_(tokens),
            )
            .order_by(analysis_results.c.created_at)
        ).all()
    summaries = {}
    for token, result in rows:
        if not result or "overall_condition" not in result:
            continue
        summaries[token] = {
            "condition": result["overall_condition"],
            "damage": len(result.get("damage") or []),
            "high_impact": sum(
                1 for factor in result.get("price_factors") or [] if factor.get("impact") == "high"
            ),
            "issues": len(result.get("price_factors") or []),
        }
    return summaries


def _order(sort: str, floor: int = 0) -> list:
    if sort in MULTI_SORTS:
        return [column.asc().nulls_last() for column in MULTI_SORTS[sort]]
    if sort == "drop":
        return [_price_drop(floor).desc().nulls_last()]
    if sort == "drops":
        return [_drop_count(floor).desc(), _price_drop(floor).desc().nulls_last()]
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
    engine: Engine, manufacturer: str | None, model: str | None, sub_model: str | None = None,
    search_id: int | None = None,
) -> list:
    conditions = [
        ads.c.price.is_not(None),
        ads.c.price >= ignored_price_floor(engine),
        _latest_of_car(),
        *_in_search(search_id),
    ]
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
                _market_since().label("market_since"),
                _car_key(ads).label("car"),
            ).where(*conditions)
        ).all()


def models(engine: Engine, manufacturer: str | None, search_id: int | None = None) -> list[str]:
    statement = select(ads.c.model).distinct().where(*_in_search(search_id)).order_by(ads.c.model)
    if manufacturer:
        statement = statement.where(ads.c.manufacturer == manufacturer)
    with engine.connect() as db:
        return [value for value in db.execute(statement).scalars() if value]


def photo_key(engine: Engine, sha256: str) -> str | None:
    with engine.connect() as db:
        return db.execute(
            select(photos.c.path).where(photos.c.sha256 == sha256).limit(1)
        ).scalar_one_or_none()


def sub_models(
    engine: Engine, manufacturer: str | None, model: str | None, search_id: int | None = None
) -> list[str]:
    statement = select(ads.c.sub_model).distinct().where(*_in_search(search_id)).order_by(ads.c.sub_model)
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
    id: int
    chat_id: int
    url: str
    description: str
    interval_minutes: int
    next_scan_at: datetime.datetime | None
    added_from: str | None
    active: object | None  # the queued or running scan, if any
    last: object | None  # the latest finished scan


def search_statuses(engine: Engine) -> list[SearchStatus]:
    """Every saved search with its next scheduled scan, current scan and latest result."""
    with engine.connect() as db:
        rows = db.execute(select(searches).order_by(searches.c.chat_id, searches.c.id)).all()
        statuses = []
        for row in rows:
            latest = db.execute(
                select(scans)
                .where(scans.c.search_id == row.id)
                .order_by(scans.c.id.desc())
                .limit(20)
            ).all()
            active = next((scan for scan in latest if scan.status in ("queued", "running")), None)
            last = next(
                (scan for scan in latest if scan.status not in ("queued", "running")), None
            )
            statuses.append(
                SearchStatus(
                    row.id, row.chat_id, row.url, describe_search(row.url), row.interval_minutes,
                    row.next_scan_at, row.added_from, active, last,
                )
            )
    return statuses


def search_choices(engine: Engine) -> list[tuple[int, str]]:
    """(id, label) of every saved search, for the dashboard's search picker."""
    with engine.connect() as db:
        rows = db.execute(select(searches.c.id, searches.c.url).order_by(searches.c.id)).all()
    return [(row.id, describe_search(row.url)) for row in rows]


def known_chats(engine: Engine) -> list[int]:
    """Chats a search can be added for: ones with searches, alerts or bot access."""
    with engine.connect() as db:
        found = set(db.execute(select(searches.c.chat_id)).scalars())
        found |= set(db.execute(select(chat_ads.c.chat_id).distinct()).scalars())
        found |= set(db.execute(select(access.c.user_id).where(access.c.allowed.is_(True))).scalars())
        found |= set(db.execute(select(admins.c.user_id)).scalars())
    return sorted(found)


def scan_history(
    engine: Engine, limit: int = 200, chat_id: int | None = None, search_id: int | None = None
) -> list:
    statement = select(scans).order_by(scans.c.id.desc()).limit(limit)
    if chat_id is not None:
        statement = statement.where(scans.c.chat_id == chat_id)
    if search_id is not None:
        statement = statement.where(scans.c.search_id == search_id)
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


@dataclass
class ProcessorStatus:
    name: str
    target: str
    version: str
    enabled: bool
    reason: str | None  # why it is not running
    done: int  # successful results for the current version
    failed: int  # gave up after the maximum attempts
    retrying: int
    waiting: int  # ready to run now
    last_at: datetime.datetime | None


def processor_statuses(engine: Engine, enabled: list) -> list[ProcessorStatus]:
    """Every known processor with its backlog, in the order the bot runs them."""
    from processing.registry import AVAILABLE
    from processing.runner import MAX_ATTEMPTS, pending_count

    running = {processor.name: processor for processor in enabled}
    ordered = [*enabled, *(cls() for name, cls in AVAILABLE.items() if name not in running)]
    statuses = []
    with engine.connect() as db:
        for processor in ordered:
            outcome = case(
                (analysis_results.c.status == "ok", "ok"),
                (analysis_results.c.attempts >= MAX_ATTEMPTS, "failed"),
                else_="retrying",
            ).label("outcome")
            rows = dict(
                db.execute(
                    select(outcome, func.count())
                    .where(
                        analysis_results.c.processor == processor.name,
                        analysis_results.c.version == processor.version,
                    )
                    .group_by(outcome)
                ).all()
            )
            last_at = db.execute(
                select(func.max(analysis_results.c.created_at)).where(
                    analysis_results.c.processor == processor.name
                )
            ).scalar_one()
            is_enabled = processor.name in running
            reason = None
            if not is_enabled:
                reason = getattr(type(processor), "available", lambda: None)() or "Not in PROCESSORS"
            statuses.append(
                ProcessorStatus(
                    processor.name, processor.target, processor.version, is_enabled, reason,
                    rows.get("ok", 0), rows.get("failed", 0), rows.get("retrying", 0),
                    pending_count(engine, processor), last_at,
                )
            )
    return statuses


def photo_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as db:
        state = case(
            (photos.c.sha256.is_not(None), "stored"),
            (photos.c.download_attempts >= 5, "failed"),
            else_="waiting",
        ).label("state")
        rows = db.execute(select(state, func.count()).group_by(state)).all()
    return {"stored": 0, "waiting": 0, "failed": 0, **dict(rows)}


def recent_results(engine: Engine, limit: int = 30, errors_only: bool = False) -> list:
    photo_token = (
        select(photos.c.token)
        .where(photos.c.sha256 == analysis_results.c.target_id)
        .order_by(photos.c.id)
        .limit(1)
        .correlate(analysis_results)
        .scalar_subquery()
    )
    statement = (
        select(
            analysis_results.c.processor,
            analysis_results.c.target_type,
            analysis_results.c.target_id,
            analysis_results.c.status,
            analysis_results.c.error,
            analysis_results.c.attempts,
            analysis_results.c.created_at,
            photo_token.label("photo_token"),
        )
        .order_by(analysis_results.c.created_at.desc())
        .limit(limit)
    )
    if errors_only:
        statement = statement.where(analysis_results.c.status == "error")
    with engine.connect() as db:
        return db.execute(statement).all()


def sale_counts(engine: Engine) -> dict[str, int]:
    """Removed ads with a confirmed plate, by what the registry follow-up concluded."""
    with engine.connect() as db:
        rows = db.execute(
            select(func.coalesce(ads.c.sale_status, "queued"), func.count())
            .where(ads.c.removed_at.is_not(None), ads.c.plate_status == "confirmed")
            .group_by(func.coalesce(ads.c.sale_status, "queued"))
        ).all()
    return {status: count for status, count in rows}


@dataclass
class ModelMarket:
    """Where one ad stands among every tracked ad of its make and model (all searches)."""
    rows: list
    active: int
    removed: int
    median_days: int | None  # until removal, for ads that are gone
    peer_label: str  # which ads the ad is compared with, e.g. "2022 · same subtype"
    peer_count: int  # ads on Yad2 in that comparison, the ad included
    peer_median: int | None
    rank: int | None  # 1 = the cheapest of them
    comparables: list  # the cheapest ads on Yad2 within a year of this one


MIN_PEERS = 3


def car_of(engine: Engine, token: str) -> str | None:
    """The key every ad of the same car shares (see storage.cars)."""
    with engine.connect() as db:
        return db.execute(select(_car_key(ads)).where(ads.c.token == token)).scalar_one_or_none()


def model_market(engine: Engine, ad) -> ModelMarket | None:
    """The market for the ad's model, across all searches; None without a make and model."""
    if not ad.manufacturer or not ad.model:
        return None
    rows = market_points(engine, ad.manufacturer, ad.model)
    car = ad.car_id or ad.token
    others = [row for row in rows if row.car != car]
    live = [row for row in others if row.removed_at is None]

    # Compare with the closest group that still has a few ads: same year and subtype, then
    # same year, then a year either side.
    choices = [
        (f"{ad.year} · same subtype", lambda row: row.year == ad.year and row.sub_model == ad.sub_model),
        (f"{ad.year}", lambda row: row.year == ad.year),
        (f"{ad.year - 1}–{ad.year + 1}" if ad.year else "all years",
         lambda row: ad.year is None or row.year is not None and abs(row.year - ad.year) <= 1),
        ("all years", lambda row: True),
    ]
    if not ad.sub_model:
        choices = choices[1:]
    for label, keep in choices:
        peers = [row for row in live if keep(row)]
        if len(peers) + 1 >= MIN_PEERS:
            break
    prices = [row.price for row in peers]
    if ad.price is not None and ad.removed_at is None:
        prices.append(ad.price)
    rank = None
    if ad.price is not None and prices:
        rank = 1 + sum(1 for price in prices if price < ad.price)
    near = [row for row in live if ad.year is None or row.year is not None and abs(row.year - ad.year) <= 1]
    return ModelMarket(
        rows=rows,
        active=sum(1 for row in rows if row.removed_at is None),
        removed=sum(1 for row in rows if row.removed_at is not None),
        median_days=_median([days_listed(row) for row in rows if row.removed_at]),
        peer_label=label,
        peer_count=len(prices),
        peer_median=_median(prices),
        rank=rank,
        comparables=sorted(near, key=lambda row: row.price)[:8],
    )
