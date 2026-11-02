"""Validation and normalization for Yad2 car search links sent in Telegram."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from car.car_search_filter import CarSearchFilter

BASE_URL = "https://www.yad2.co.il/vehicles/cars"
FILTER_KEYS = {
    "manufacturer", "model", "year", "price", "km", "hand", "gearBox",
    "topArea", "area", "city", "engineType", "bodyType", "priceOnly", "imgOnly",
}


def search_url_for_filter(car_filter: CarSearchFilter) -> str:
    params = car_filter.get_url_parameters()
    return BASE_URL + ("?" + "&".join(params) if params else "")


def normalize_search_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"www.yad2.co.il", "yad2.co.il"}:
        raise ValueError("Send an HTTPS Yad2 car search URL.")
    if parsed.username or parsed.password or parsed.port or parsed.path.rstrip("/") != "/vehicles/cars":
        raise ValueError("The link must point to Yad2's car search page.")
    params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
    if not any(key in FILTER_KEYS and value for key, value in params):
        raise ValueError("The Yad2 URL needs at least one car search filter.")
    return urlunsplit(("https", "www.yad2.co.il", "/vehicles/cars", urlencode(params), ""))
