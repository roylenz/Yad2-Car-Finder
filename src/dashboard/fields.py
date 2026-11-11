"""Labels and grouping for an ad's attributes on the dashboard."""

import datetime

SELLER_TYPES = {"private": "Private seller", "commercial": "Dealer"}

VERIFIED_LABELS = {
    "year": "year",
    "hand": "hand",
    "owner": "ownership",
    "originalOwner": "original owner",
    "testDate": "test date",
    "km": "mileage",
}

EQUIPMENT_LABELS = {
    "airConditioner": "Air conditioning",
    "powerSteering": "Power steering",
    "magnesiumWheel": "Alloy wheels",
    "tirePressureMonitoringSystem": "Tyre pressure sensors",
    "abs": "ABS",
    "controlStability": "Stability control",
    "breakingAssistSystem": "Brake assist",
    "reverseCamera": "Reversing camera",
    "adaptiveCruiseControl": "Adaptive cruise control",
    "highBeamsAutoControl": "Automatic high beams",
    "blindSpotAssist": "Blind-spot assist",
    "identifyPedestrians": "Pedestrian detection",
    "seatBeltsSensors": "Seat-belt sensors",
    "identifyingDangerousNearing": "Collision warning",
    "autoLightingInForward": "Automatic headlights",
    "identifyTrafficSigns": "Traffic-sign recognition",
    "roadDeviationControl": "Lane-departure control",
    "forwardDistanceMonitor": "Forward distance monitor",
    "isHandicappedFriendly": "Accessible for disabled drivers",
    "sunRoof": "Sunroof",
    "isTurbo": "Turbo",
}

# Attributes shown elsewhere on the page, or only kept for the record.
_HIDDEN = {
    "description", "specification", "equipment", "tags", "manufacturer_id", "model_id",
    "sub_model_id", "price_before",
}


def _date(value) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 7:  # YYYY-MM
        return f"{text[5:7]}/{text[:4]}"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.time() == datetime.time() and parsed.day == 1:
        return parsed.strftime("%m/%Y")  # Yad2 gives test dates to the month
    if parsed.time() == datetime.time():
        return parsed.strftime("%d/%m/%Y")
    return parsed.strftime("%d/%m/%Y %H:%M")


def _km(value) -> str | None:
    return f"{value:,} km" if isinstance(value, int) else None


def _plain(value) -> str | None:
    if value is None or value in ("", []):
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


_GROUPS = [
    (
        "Registration & tests",
        [
            ("on_road_since", "On the road since", _date),
            ("last_test_date", "Last test", _date),
            ("km_at_last_test", "Mileage at last test", _km),
            ("km_since_last_test", "Driven since last test", _km),
            ("test_valid_until", "Test valid until", _date),
            ("ownership", "Ownership", _plain),
            ("original_owner", "Original owner", _plain),
            (
                "verified_by_ministry",
                "Verified by Ministry of Transport",
                lambda keys: ", ".join(VERIFIED_LABELS.get(key, key) for key in keys) or None,
            ),
        ],
    ),
    (
        "Car",
        [
            ("engine_type", "Engine", _plain),
            ("powertrain", "Powertrain", _plain),
            ("engine_volume", "Engine volume", lambda v: f"{v:,} cc" if isinstance(v, int) else _plain(v)),
            ("horse_power", "Horsepower", _plain),
            ("gearbox", "Gearbox", _plain),
            ("drive", "Drive", _plain),
            ("fuel_consumption", "Fuel consumption", lambda v: f"{v} km/l" if v else None),
            ("body_type", "Body", _plain),
            ("car_family", "Category", _plain),
            ("color", "Colour", _plain),
            ("seats", "Seats", _plain),
            ("doors", "Doors", _plain),
        ],
    ),
    (
        "Safety",
        [
            ("safety_points", "Safety equipment level", lambda v: f"{v} of 8 (8 = most)" if v is not None else None),
            ("pollution_level", "Pollution level", lambda v: f"{v} of 15 (1 = cleanest)" if v is not None else None),
            ("airbags", "Airbags", _plain),
        ],
    ),
    (
        "Ad",
        [
            ("seller_type", "Seller", lambda v: SELLER_TYPES.get(v, v)),
            ("city", "City", _plain),
            ("area", "Area", _plain),
            ("ad_number", "Ad number", lambda v: str(v)),
            ("updated_at", "Updated on Yad2", _date),
            ("rebounced_at", "Bumped on Yad2", _date),
            ("ends_at", "Ad expires", _date),
            ("car_tags", "Yad2 tags", _plain),
        ],
    ),
]


def detail_groups(attributes: dict | None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Ordered (group, [(label, value)]) for display; unknown attributes go under "Other"."""
    attributes = attributes or {}
    shown = set(_HIDDEN)
    groups = []
    for title, fields in _GROUPS:
        rows = []
        for key, label, formatter in fields:
            shown.add(key)
            value = attributes.get(key)
            text = formatter(value) if value not in (None, "", []) else None
            if text:
                rows.append((label, text))
        if rows:
            groups.append((title, rows))
    other = [
        (key.replace("_", " ").capitalize(), _plain(value))
        for key, value in sorted(attributes.items())
        if key not in shown and _plain(value)
    ]
    if other:
        groups.append(("Other", other))
    return groups


def equipment(attributes: dict | None) -> tuple[list[str], list[str]]:
    """(fitted, not fitted) equipment labels."""
    specification = (attributes or {}).get("specification") or {}
    fitted, missing = [], []
    for key, value in specification.items():
        if not isinstance(value, bool):
            continue
        label = EQUIPMENT_LABELS.get(key, key)
        (fitted if value else missing).append(label)
    windows = specification.get("electricWindow")
    if windows:
        fitted.append(f"{windows} electric windows")
    return fitted, missing
