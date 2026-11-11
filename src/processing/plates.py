"""Israeli licence plates: voting over photo readings and checking the government vehicle registry.

The registry is the Ministry of Transport's open dataset on data.gov.il (no key needed). A plate
read from the photos is only trusted once the registry lists it as the same make or model as
the ad, which filters out OCR mistakes.
"""

import datetime
import json
import re
from collections import defaultdict
from collections.abc import Iterable

import httpx

from car.listing import CarListing

REGISTRY_URL = "https://data.gov.il/api/3/action/datastore_search"
REGISTRY_RESOURCE = "053cea08-09bc-40ec-8f7a-156f0677aff3"  # private and commercial vehicles
PLATE_LENGTHS = (7, 8)
MIN_SCORE = 0.9  # summed OCR confidence a plate needs before it is looked up
MAX_CANDIDATES = 3

# Registry fields kept with the result, in display order.
REGISTRY_FIELDS = {
    "tozeret_nm": "Make",
    "kinuy_mishari": "Model",
    "degem_nm": "Model code",
    "ramat_gimur": "Trim",
    "shnat_yitzur": "Year",
    "moed_aliya_lakvish": "On the road since",
    "mivchan_acharon_dt": "Last test",
    "tokef_dt": "Licence valid until",
    "baalut": "Ownership",
    "tzeva_rechev": "Colour",
    "sug_delek_nm": "Fuel",
    "zmig_kidmi": "Front tyres",
    "zmig_ahori": "Rear tyres",
    "misgeret": "VIN",
}

# Yad2 ownership values -> registry ownership values.
_OWNERSHIP = {"פרטית": "פרטי", "החכר (ליסינג)": "ליסינג", "ליסינג": "ליסינג", "חברה": "חברה", "השכרה": "השכרה"}


def format_plate(plate: str) -> str:
    if len(plate) == 7:
        return f"{plate[:2]}-{plate[2:5]}-{plate[5:]}"
    if len(plate) == 8:
        return f"{plate[:3]}-{plate[3:5]}-{plate[5:]}"
    return plate


def is_valid_reading(reading: dict) -> bool:
    return bool(reading.get("valid")) and len(reading.get("digits", "")) in PLATE_LENGTHS


def vote(readings: Iterable[dict]) -> list[tuple[str, float]]:
    """Plates read across an ad's photos, best first, scored by summed OCR confidence."""
    scores: dict[str, float] = defaultdict(float)
    for reading in readings:
        if is_valid_reading(reading):
            scores[reading["digits"]] += float(reading.get("ocr_conf", 0))
    return sorted(((plate, round(score, 3)) for plate, score in scores.items()), key=lambda item: -item[1])


async def lookup(client: httpx.AsyncClient, plate: str) -> dict | None:
    """The registry record for a plate, or None if it is not listed."""
    response = await client.get(
        REGISTRY_URL,
        params={"resource_id": REGISTRY_RESOURCE, "filters": json.dumps({"mispar_rechev": int(plate)})},
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Registry lookup failed: {payload.get('error')}")
    records = payload["result"]["records"]
    if not records:
        return None
    return {key: records[0].get(key) for key in REGISTRY_FIELDS}


def _latin(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _hebrew(text: str | None) -> str:
    letters = re.sub(r"[^א-ת]", "", text or "")
    return letters.replace("וו", "ו").replace("יי", "י")


def _month(value) -> str | None:
    """'2023-9', '2023-09' or '2023-09-01T..' -> '2023-09'."""
    match = re.match(r"(\d{4})-(\d{1,2})", str(value or ""))
    return f"{match[1]}-{int(match[2]):02d}" if match else None


def _date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def compare(listing: CarListing, record: dict) -> dict:
    """Is the registry record the advertised car, and where do the ad and registry disagree?"""
    ad_text = _latin(" ".join(filter(None, (listing.model, listing.sub_model, listing.title))))
    registry_model = _latin(record.get("kinuy_mishari"))
    ad_model = _latin(listing.model)
    model_matches = bool(registry_model) and (
        registry_model in ad_text
        or registry_model.replace("EV", "") in ad_text
        or (bool(ad_model) and ad_model in registry_model)
    )
    ad_make, registry_make = _hebrew(listing.manufacturer), _hebrew(record.get("tozeret_nm"))
    make_matches = len(ad_make) >= 2 and ad_make[:3] == registry_make[:3]

    discrepancies = []
    year = record.get("shnat_yitzur")
    if listing.year and year and listing.year != year:
        discrepancies.append(f"Ad says {listing.year}, registry says {year}")
    attributes = listing.attributes or {}
    ad_road, registry_road = _month(attributes.get("on_road_since")), _month(record.get("moed_aliya_lakvish"))
    if ad_road and registry_road and ad_road != registry_road:
        discrepancies.append(f"Ad says on the road since {ad_road}, registry says {registry_road}")
    ownership = record.get("baalut")
    if ownership == "סוחר" and attributes.get("seller_type") == "private":
        discrepancies.append("Listed by a private seller, but registered to a dealer")
    elif ownership == "סוחר" and _OWNERSHIP.get(attributes.get("ownership") or "") == "פרטי":
        discrepancies.append("Ad says private ownership, but the car is registered to a dealer")
    elif ownership == "השכרה":
        discrepancies.append("Registered as a rental car")
    else:
        ad_ownership = _OWNERSHIP.get(attributes.get("ownership") or "")
        if ad_ownership and ownership and ad_ownership != ownership:
            discrepancies.append(f"Ad says {attributes['ownership']} ownership, registry says {ownership}")
    ad_test, registry_test = _date(attributes.get("last_test_date")), _date(record.get("mivchan_acharon_dt"))
    if ad_test and registry_test and abs((ad_test - registry_test).days) > 45:
        discrepancies.append(
            f"Ad says last test {ad_test:%m/%Y}, registry says {registry_test:%d/%m/%Y}"
        )
    return {
        "same_car": model_matches or make_matches,
        "model_matches": model_matches,
        "make_matches": make_matches,
        "discrepancies": discrepancies,
    }
