"""Validation and normalization for Yad2 car search links sent in Telegram."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from car.car_search_filter import CarSearchFilter

BASE_URL = "https://www.yad2.co.il/vehicles/cars"
def search_url_for_filter(car_filter: CarSearchFilter) -> str:
    params = car_filter.get_url_parameters()
    return BASE_URL + ("?" + "&".join(params) if params else "")


def normalize_search_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"www.yad2.co.il", "yad2.co.il"}:
        raise ValueError("Send an HTTPS Yad2 car search URL.")
    if parsed.username or parsed.password or parsed.port or parsed.path.rstrip("/") != "/vehicles/cars":
        raise ValueError("The link must point to Yad2's car search page.")
    params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "page" and not key.startswith("utm_")
    ]
    if not any(value for _, value in params):
        raise ValueError("The Yad2 URL needs at least one car search filter.")
    return urlunsplit(("https", "www.yad2.co.il", "/vehicles/cars", urlencode(params), ""))


def price_range(search_url: str) -> tuple[int, int] | None:
    """The search's `price=min-max` filter when it has a maximum (a minimum of -1 means none)."""
    value = dict(parse_qsl(urlsplit(search_url).query)).get("price", "")
    match = re.fullmatch(r"(-1|\d+)-(\d+)", value)
    if not match or int(match[2]) <= int(match[1]):
        return None
    return int(match[1]), int(match[2])


def with_price(search_url: str, low: int, high: int) -> str:
    """The same search limited to asking prices `low`..`high` (inclusive)."""
    parsed = urlsplit(search_url)
    params = [
        (key, f"{low}-{high}" if key == "price" else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "page"
    ]
    if not any(key == "price" for key, _ in params):
        params.append(("price", f"{low}-{high}"))
    return urlunsplit(parsed._replace(query=urlencode(params)))


RANGE_FILTERS = ("price", "year", "km", "hand")  # `key=min-max` filters; -1 means no limit


def range_filters(search_url: str) -> dict[str, tuple[int | None, int | None]]:
    """The search's (min, max) for each of `RANGE_FILTERS`; None where it sets no limit."""
    params = dict(parse_qsl(urlsplit(search_url).query))
    ranges = {}
    for key in RANGE_FILTERS:
        match = re.fullmatch(r"(-1|\d+)-(-1|\d+)", params.get(key, ""))
        low, high = (int(value) for value in match.groups()) if match else (-1, -1)
        ranges[key] = (low if low >= 0 else None, high if high >= 0 else None)
    return ranges


def with_ranges(search_url: str, ranges: dict[str, tuple[int | None, int | None]]) -> str:
    """The same search with the given `RANGE_FILTERS` set to (min, max); a filter given
    (None, None) is dropped and ones not given are kept. Raises ValueError for a minimum above
    its maximum, or when no filter would be left."""
    for key, (low, high) in ranges.items():
        if key not in RANGE_FILTERS:
            raise ValueError(f"{key} is not a range filter.")
        if low is not None and high is not None and low > high:
            raise ValueError(f"{key.capitalize()}: the minimum ({low}) is above the maximum ({high}).")
    values = {
        key: f"{-1 if low is None else low}-{-1 if high is None else high}"
        for key, (low, high) in ranges.items()
        if low is not None or high is not None
    }
    parsed = urlsplit(search_url)
    params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key not in ranges:
            params.append((key, value))
        elif key in values:
            params.append((key, values.pop(key)))
    params.extend(values.items())
    if not any(value for _, value in params):
        raise ValueError("A search needs at least one filter.")
    return urlunsplit(parsed._replace(query=urlencode(params)))


def split_price(low: int, high: int, prices: list[int], parts: int = 2) -> list[tuple[int, int]]:
    """Cut `low..high` into up to `parts` ranges holding about as many known prices each
    (equal widths when no prices are known). The ranges are inclusive and cover `low..high`."""
    inside = sorted(price for price in prices if low <= price <= high)
    parts = max(2, min(parts, high - low + 1))
    if inside:
        cuts = [inside[len(inside) * part // parts - 1] for part in range(1, parts)]
    else:
        cuts = [low + (high - low) * part // parts for part in range(1, parts)]
    ranges, start_at = [], low
    for cut in sorted({min(cut, high - 1) for cut in cuts}):
        if start_at <= cut < high:
            ranges.append((start_at, cut))
            start_at = cut + 1
    ranges.append((start_at, high))
    return ranges


def describe_search(search_url: str) -> str:
    """A short readable summary of a search's filters, e.g. "BYD Atto 3 · 2023+ · up to ₪100,000"."""
    from car.catalog import MANUFACTURER_BY_ID, model_name

    params = dict(parse_qsl(urlsplit(search_url).query))
    ids = lambda key: [int(value) for value in params.get(key, "").split(",") if value.isdigit()]  # noqa: E731
    makes = ids("manufacturer")
    parts = []
    if makes:
        parts.append(", ".join(MANUFACTURER_BY_ID.get(make, {}).get("name", str(make)) for make in makes))
    models = ids("model")
    if models:
        names = [model_name(model, set(makes)) for model in models]
        parts.append(", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else ""))
    if not makes and params.get("carTag"):
        parts.append({"6": "Electric"}.get(params["carTag"], f"Tag {params['carTag']}"))

    def bounds(key: str) -> tuple[str, str]:
        match = re.fullmatch(r"(-?\d+)-(-?\d+)", params.get(key, ""))
        if not match:
            return "", ""
        low, high = (value if int(value) > 0 else "" for value in match.groups())
        return low, high

    year_low, year_high = bounds("year")
    if year_low or year_high:
        parts.append(f"{year_low}–{year_high}" if year_low and year_high else (f"{year_low}+" if year_low else f"until {year_high}"))
    price_low, price_high = bounds("price")
    if price_high:
        parts.append(f"up to ₪{int(price_high):,}")
    elif price_low:
        parts.append(f"from ₪{int(price_low):,}")
    _, km_high = bounds("km")
    if km_high:
        parts.append(f"up to {int(km_high):,} km")
    hand_low, hand_high = bounds("hand")
    if hand_high:
        parts.append(f"hand {hand_high}" if hand_low == hand_high else f"hand ≤{hand_high}")
    return " · ".join(parts) or "Yad2 search"
