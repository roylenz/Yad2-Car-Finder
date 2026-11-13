"""Everything the database knows that could help negotiate one car's price.

Read-only: the ad, its price history across reposts, how it compares with the same model on
the market, the registry check and the Claude photo inspection.
"""

import datetime
import statistics

from sqlalchemy import Engine

from dashboard import queries
from processing import plates


def gather(engine: Engine, token: str, now: datetime.datetime | None = None) -> dict | None:
    detail = queries.ad_detail(engine, token)
    if detail is None:
        return None
    ad, history, _photos, results, _chats, same_car = detail
    now = now or datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    floor = queries.ignored_price_floor(engine)
    attributes = ad.attributes or {}

    facts = {
        "ad": {
            "token": ad.token,
            "url": f"https://www.yad2.co.il/vehicles/item/{ad.token}",
            "title": ad.title,
            "manufacturer": ad.manufacturer,
            "model": ad.model,
            "sub_model": ad.sub_model,
            "year": ad.year,
            "km": ad.km,
            "hand": ad.hand,
            "asking_price": ad.price,
            "on_yad2": ad.removed_at is None,
            "seller_type": attributes.get("seller_type"),
            "city": attributes.get("city") or attributes.get("area"),
            "ownership_in_ad": attributes.get("ownership"),
            "on_road_since": attributes.get("on_road_since"),
            "last_test_date": attributes.get("last_test_date"),
            "test_valid_until": attributes.get("test_valid_until"),
            "km_at_last_test": attributes.get("km_at_last_test"),
            "color": attributes.get("color"),
            "description": attributes.get("description"),
        },
        "listing": _listing_history(ad, history, same_car, floor, now),
        "market": _market(engine, ad, same_car, now),
        "registry": _registry(ad, queries.latest_result(results, "plate_registry")),
        "condition": _condition(queries.latest_result(results, "claude_condition")),
    }
    return facts


def _listing_history(ad, history, same_car, floor: int, now: datetime.datetime) -> dict:
    prices = [(row.observed_at, row.price) for row in history if row.price is not None and row.price >= floor]
    changes = []
    for (_, old), (when, new) in zip(prices, prices[1:]):
        if new != old:
            changes.append({"date": when.date().isoformat(), "from": old, "to": new})
    starts = [ad.listed_at or ad.first_seen_at] + [row["first_seen_at"] for row in same_car]
    since = min(start for start in starts if start)
    first_price = prices[0][1] if prices else ad.price
    return {
        "on_market_since": since.date().isoformat(),
        "days_on_market": (now - since).days,
        "reposts": len(same_car),
        "first_asking_price": first_price,
        "total_drop": (first_price - ad.price) if first_price and ad.price else 0,
        "price_changes": changes,
    }


def _market(engine: Engine, ad, same_car, now: datetime.datetime) -> dict:
    """Same make and model, a year either side; the car itself (and its reposts) left out."""
    own = {ad.token, *(row["token"] for row in same_car)}
    rows = [
        row for row in queries.market_points(engine, ad.manufacturer, ad.model)
        if row.token not in own and (ad.year is None or row.year is None or abs(row.year - ad.year) <= 1)
    ]
    result = {"comparables": len(rows), "note": "Same make and model, a year either side; asking prices"}
    if not rows:
        return result
    result.update(_stats(rows, ad, now))
    same_trim = [row for row in rows if ad.sub_model and row.sub_model == ad.sub_model]
    if same_trim:
        result["same_sub_model"] = {"comparables": len(same_trim), **_stats(same_trim, ad, now)}
    cheapest = sorted((row for row in rows if row.removed_at is None), key=lambda row: row.price)[:3]
    result["cheapest_on_yad2_now"] = [
        {"title": row.title, "sub_model": row.sub_model, "year": row.year, "km": row.km, "price": row.price}
        for row in cheapest
    ]
    return result


def _stats(rows, ad, now: datetime.datetime) -> dict:
    prices = [row.price for row in rows]
    active = [row for row in rows if row.removed_at is None]
    removed = [row for row in rows if row.removed_at is not None]
    kms = [row.km for row in rows if row.km]
    stats = {
        "median_price": int(statistics.median(prices)),
        "min_price": min(prices),
        "max_price": max(prices),
        "on_yad2_now": len(active),
        "cheaper_than_this": sum(1 for price in prices if ad.price and price < ad.price),
        "median_km": int(statistics.median(kms)) if kms else None,
    }
    if removed:
        stats["removed_likely_sold"] = len(removed)
        stats["median_days_until_removed"] = int(statistics.median(queries.days_listed(row, now) for row in removed))
        stats["median_last_price_when_removed"] = int(statistics.median(row.price for row in removed))
    return stats


def _registry(ad, result: dict | None) -> dict:
    out: dict = {}
    if result and result.get("status") == "confirmed":
        record = result.get("registry") or {}
        out.update(
            plate_confirmed=True,
            trim=record.get("ramat_gimur"),
            on_road_since=record.get("moed_aliya_lakvish"),
            last_test=record.get("mivchan_acharon_dt"),
            test_valid_until=record.get("tokef_dt"),
            discrepancies=result.get("discrepancies") or [],
        )
    else:
        out["plate_confirmed"] = False
    if ad.ownership:
        summary = plates.ownership_summary(ad.ownership)
        out["ownership"] = {
            "owners_not_counting_dealers": summary["hands"],
            "last_owner_type": summary["last_owner"],
            "with_dealer_now": summary["with_dealer"],
            "months_with_dealer": summary["dealer_months"],
            "history": ad.ownership,
        }
    return out


def _condition(result: dict | None) -> dict | None:
    if not result or result.get("skipped"):
        return None
    return {
        key: result.get(key)
        for key in ("overall_condition", "summary", "damage", "price_factors", "positives",
                    "odometer_km", "photos_match_ad", "match_notes")
    }
