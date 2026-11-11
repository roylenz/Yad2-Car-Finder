"""Parse the JSON Yad2 embeds in its Next.js pages (`<script id="__NEXT_DATA__">`).

Search pages hold the results under the `feed-mix` or `feed` query (price, photos, creation time, model,
year, hand). Item pages hold the full ad under `["vehicles", "item", token]` (adds mileage,
colour, gearbox, test date, city, description, specification and dates).
"""

import datetime
import re
from zoneinfo import ZoneInfo

from car.listing import CarListing

ITEM_URL = "https://www.yad2.co.il/vehicles/item/{token}"
# Yad2 A/B tests two feed layouts: "feed-mix" (ads, ...) and "feed" (private, commercial, ...).
_FEED_QUERIES = ("feed-mix", "feed")
_FEED_GROUPS = ("ads", "private", "commercial", "platinum", "boost", "solo")
_YAD2_TIMEZONE = ZoneInfo("Asia/Jerusalem")


def _queries(next_data: dict) -> list[dict]:
    return (
        next_data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries")
        or []
    )


def feed_data(next_data: dict) -> dict | None:
    for query in _queries(next_data):
        key = query.get("queryKey") or []
        if key and key[0] in _FEED_QUERIES:
            return (query.get("state") or {}).get("data")
    return None


def feed_ads(next_data: dict) -> list[dict]:
    data = feed_data(next_data) or {}
    seen, ads = set(), []
    for group in _FEED_GROUPS:
        for ad in data.get(group) or []:
            token = ad.get("token")
            if token and token not in seen:
                seen.add(token)
                ads.append(ad)
    return ads


def feed_total(next_data: dict) -> int | None:
    """How many results Yad2 says the search has (all pages)."""
    pagination = (feed_data(next_data) or {}).get("pagination") or {}
    total = pagination.get("total")
    return int(total) if isinstance(total, int | str) and str(total).isdigit() else None


def unknown_feed_groups(next_data: dict) -> list[str]:
    """Lists of ads in the feed that `feed_ads` does not read (a new Yad2 layout would hide ads)."""
    data = feed_data(next_data) or {}
    return sorted(
        key for key, value in data.items()
        if key not in _FEED_GROUPS and isinstance(value, list)
        and any(isinstance(item, dict) and item.get("token") for item in value)
    )


def feed_pages(next_data: dict) -> int:
    pagination = (feed_data(next_data) or {}).get("pagination") or {}
    return int(pagination.get("pages") or 1)


def item_data(next_data: dict) -> dict | None:
    for query in _queries(next_data):
        key = query.get("queryKey") or []
        if len(key) >= 2 and key[:2] == ["vehicles", "item"]:
            return (query.get("state") or {}).get("data") or None
    return None


def _text(value: dict | None) -> str | None:
    return value.get("text") if isinstance(value, dict) else None


def _yad2_time(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        local = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if local.tzinfo is None:
        local = local.replace(tzinfo=_YAD2_TIMEZONE)
    return local.astimezone(datetime.UTC).replace(tzinfo=None)


# Fields mapped above, plus internal/personal ones that are not stored as attributes.
_HANDLED_KEYS = {
    "token", "price", "km", "hand", "metaData", "address", "dates", "createdAt", "vehicleDates",
    "manufacturer", "model", "subModel", "engineType", "engineVolume", "horsePower", "gearBox",
    "color", "bodyType", "seats", "numberOfDoors", "owner", "originalOwner",
    "combinedFuelConsumption", "powertrainArchitecture", "carFamilyType", "carTag", "tags",
    "specification", "isVerifiedByMt", "kmInLastTest", "lastTestDate", "adType", "adNumber",
    "customer", "orderId", "categoryId", "subcategoryId", "priority", "clientType",
    "filterTags", "isFinancingConsent", "packages",
}


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _texts(values) -> list[str] | None:
    if not isinstance(values, list):
        return None
    return [value["text"] for value in values if isinstance(value, dict) and value.get("text")]


def _plain(value):
    """Reduce Yad2's {id, text} objects to their text."""
    if isinstance(value, dict) and "text" in value:
        return value["text"]
    if isinstance(value, list) and value and all(isinstance(item, dict) and "text" in item for item in value):
        return [item["text"] for item in value]
    return value


def _int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def listing_from_json(ad: dict, detailed: bool = False) -> CarListing:
    """Build a listing from a feed ad or a full item (the item has more fields)."""
    meta = ad.get("metaData") or {}
    address = ad.get("address") or {}
    dates = ad.get("dates") or {}
    vehicle_dates = ad.get("vehicleDates") or {}
    manufacturer = _text(ad.get("manufacturer"))
    model = _text(ad.get("model"))
    specification = ad.get("specification") or {}
    month = (vehicle_dates.get("monthOfProduction") or {}).get("id")
    year = _int(vehicle_dates.get("yearOfProduction"))
    km, km_at_last_test = _int(ad.get("km")), _int(ad.get("kmInLastTest"))
    attributes = {
        # Registration and tests ("עלייה לכביש" on Yad2 is the production month and year).
        "on_road_since": f"{year}-{month:02d}" if year and month else None,
        "last_test_date": ad.get("lastTestDate"),
        "km_at_last_test": km_at_last_test,
        "km_since_last_test": km - km_at_last_test if km is not None and km_at_last_test else None,
        "test_valid_until": vehicle_dates.get("testDate"),
        "ownership": _text(ad.get("owner")),
        "original_owner": _text(ad.get("originalOwner")),
        "verified_by_ministry": sorted(
            key for key, value in (ad.get("isVerifiedByMt") or {}).items() if value
        ),
        # Car
        "engine_type": _text(ad.get("engineType")),
        "powertrain": _text(ad.get("powertrainArchitecture")),
        "engine_volume": ad.get("engineVolume"),
        "horse_power": ad.get("horsePower"),
        "gearbox": _text(ad.get("gearBox")),
        "drive": _text(specification.get("ignition")),
        "fuel_consumption": ad.get("combinedFuelConsumption"),
        "body_type": _text(ad.get("bodyType")),
        "car_family": _texts(ad.get("carFamilyType")),
        "color": _text(ad.get("color")),
        "seats": ad.get("seats"),
        "doors": ad.get("numberOfDoors"),
        "safety_points": specification.get("safetyPoints"),
        "pollution_level": specification.get("pollutionLevel"),
        "airbags": specification.get("airBags"),
        "equipment": sorted(key for key, value in specification.items() if value is True),
        "specification": specification or None,
        # Ad
        "seller_type": ad.get("adType"),
        "area": _text(address.get("area")),
        "city": _text(address.get("city")),
        "description": meta.get("description"),
        "price_before": meta.get("priceBeforeTag"),
        "ad_number": ad.get("adNumber"),
        "updated_at": dates.get("updatedAt"),
        "rebounced_at": dates.get("rebouncedAt"),
        "ends_at": dates.get("endsAt"),
        "tags": [tag.get("name") for tag in ad.get("tags") or [] if tag.get("name")],
        "car_tags": _texts(ad.get("carTag")),
        "manufacturer_id": (ad.get("manufacturer") or {}).get("id"),
        "model_id": (ad.get("model") or {}).get("id"),
        "sub_model_id": (ad.get("subModel") or {}).get("id"),
    }
    # Keep any field Yad2 adds later, so no detail is lost.
    for key, value in ad.items():
        if key in _HANDLED_KEYS:
            continue
        name = _snake_case(key)
        if name not in attributes:
            attributes[name] = _plain(value)
    images = tuple(meta.get("images") or ([meta["coverImage"]] if meta.get("coverImage") else []))
    hand = ad.get("hand")
    return CarListing(
        url=ITEM_URL.format(token=ad["token"]),
        title=" ".join(" ".join(part for part in (manufacturer, model) if part).split())
        or "Car listing",
        price=_int(ad.get("price")),
        km=km,
        hand=_int(hand.get("id")) if isinstance(hand, dict) else _int(hand),
        year=year,
        images=images,
        listed_at=_yad2_time(dates.get("createdAt") or ad.get("createdAt")),
        manufacturer=manufacturer,
        model=model,
        sub_model=_text(ad.get("subModel")),
        attributes={key: value for key, value in attributes.items() if value not in (None, "", [])},
        raw=ad,
        detailed=detailed,
    )
