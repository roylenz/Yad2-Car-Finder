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
OWNERSHIP_RESOURCE = "bb2355dc-9ec7-4f06-9c3f-3344672171da"  # ownership history (month + owner type)
DEALER = "dealer"
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

# Yad2 and registry ownership values -> one vocabulary.
_OWNER_TYPES = {
    "פרטי": "private", "פרטית": "private",
    "החכר": "lease", "ליסינג": "lease", "החכר (ליסינג)": "lease",
    "חברה": "company",
    "השכרה": "rental",
    "סוחר": DEALER,
}
OWNER_LABELS = {"private": "Private", "lease": "Lease", "company": "Company", "rental": "Rental", DEALER: "Dealer"}


def owner_type(value: str | None) -> str | None:
    if not value:
        return None
    return _OWNER_TYPES.get(value.strip(), value.strip())


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


async def ownership_history(client: httpx.AsyncClient, plate: str) -> list[dict]:
    """[{"month": "2024-12", "owner": "private"}, ...] oldest first, from the registry's history.

    Dealers appear as owners too; they are not hands (see `ownership_summary`).
    """
    response = await client.get(
        REGISTRY_URL,
        params={
            "resource_id": OWNERSHIP_RESOURCE,
            "filters": json.dumps({"mispar_rechev": int(plate)}),
            "sort": "baalut_dt asc, _id asc",
            "limit": 100,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Ownership lookup failed: {payload.get('error')}")
    history = []
    for record in payload["result"]["records"]:
        month = str(record.get("baalut_dt") or "")
        history.append(
            {
                "month": f"{month[:4]}-{month[4:6]}" if len(month) == 6 else month,
                "owner": owner_type(record.get("baalut")),
            }
        )
    return history


def _months_between(start: str, end: datetime.date) -> int:
    year, month = (int(part) for part in start.split("-")[:2])
    return max((end.year - year) * 12 + end.month - month, 0)


def _first_day(month: str) -> datetime.date:
    return datetime.date(int(month[:4]), int(month[5:7]), 1)


def ownership_summary(history: list[dict], today: datetime.date | None = None) -> dict:
    """Hands (owners other than dealers) and the car's current run with dealers.

    Dealers are not hands: the meaningful owner is the last one before the dealers. A long run
    with dealers, or several dealers in a row, is leverage for a lower price.
    """
    today = today or datetime.date.today()
    owners = [entry for entry in history if entry["owner"] != DEALER]
    # A first registration to a company for a month or two is the importer or agency selling
    # the car new ("0 km"); sellers don't count it as a hand.
    agency = (
        len(owners) > 1
        and owners[0] is history[0]
        and owners[0]["owner"] == "company"
        and _months_between(owners[0]["month"], _first_day(owners[1]["month"])) < 3
    )
    if agency:
        owners = owners[1:]
    dealers = 0
    for entry in reversed(history):
        if entry["owner"] != DEALER:
            break
        dealers += 1
    dealer_since = history[-dealers]["month"] if dealers else None
    return {
        "hands": len(owners),
        "agency_first": agency,
        "last_owner": owners[-1]["owner"] if owners else None,
        "last_owner_since": owners[-1]["month"] if owners else None,
        "with_dealer": bool(dealers),
        "dealer_since": dealer_since,
        "dealer_months": _months_between(dealer_since, today) if dealer_since else 0,
        "dealers_in_a_row": dealers,
        "dealer_stints": sum(
            1 for index, entry in enumerate(history)
            if entry["owner"] == DEALER and (index == 0 or history[index - 1]["owner"] != DEALER)
        ),
    }


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


def compare(listing: CarListing, record: dict, history: list[dict] | None = None) -> dict:
    """Is the registry record the advertised car, and where do the ad and registry disagree?

    Ownership and hand are compared with the last owner before any dealers: dealers are not
    hands, so a car now with a dealer is not a mismatch.
    """
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

    summary = ownership_summary(history) if history else None
    if summary:
        last_owner = summary["last_owner"]
        ad_owner = owner_type(attributes.get("ownership"))
        if ad_owner and last_owner and ad_owner != last_owner and ad_owner != DEALER:
            discrepancies.append(
                f"Ad says {OWNER_LABELS.get(ad_owner, ad_owner).lower()} ownership, registry says "
                f"{OWNER_LABELS.get(last_owner, last_owner).lower()} (last owner before dealers)"
            )
        if listing.hand and summary["hands"] and listing.hand != summary["hands"]:
            discrepancies.append(
                f"Ad says hand {listing.hand}, registry shows {summary['hands']} "
                f"owner{'s' if summary['hands'] != 1 else ''} (not counting dealers)"
            )
        if last_owner == "rental":
            discrepancies.append("Was a rental car")
    elif owner_type(record.get("baalut")) == "rental":
        discrepancies.append("Registered as a rental car")
    current = owner_type(record.get("baalut"))
    if current == DEALER and attributes.get("seller_type") == "private":
        discrepancies.append("Advertised by a private seller, but a dealer owns the car now")

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
