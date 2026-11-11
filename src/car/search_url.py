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
