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
# Per car: structural change, gas conversion, colour and tyre changes, mileage at the last test.
HISTORY_RESOURCE = "56063a99-8a3e-4ff4-912e-5966c0279bad"
RECALL_RESOURCE = "36bf1404-0be4-49d2-82dc-2f1ead4a8b93"  # cars with a recall not done yet
PRICE_RESOURCE = "39f455bf-6db0-4926-859d-017f34eacbcb"  # importers' new-car list prices
# Kept from the registry record (not shown) to find the model in other datasets.
CODE_FIELDS = ("tozeret_cd", "degem_cd", "sug_degem")
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
    return {key: records[0].get(key) for key in (*REGISTRY_FIELDS, *CODE_FIELDS)}


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
        "first_owner": owners[0]["owner"] if owners else None,
        "first_owner_since": owners[0]["month"] if owners else None,
        "owners": [(entry["owner"], entry["month"]) for entry in owners],
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


# Makes Yad2 spells out letter by letter in Hebrew while the registry uses another name, by the
# ad's make (Hebrew letters only, see _hebrew) -> the Latin name the registry's model starts with.
MAKE_ALIASES = {
    "גיאיסי": "JAC",  # Yad2 "ג׳יי.איי.סי", registry "ג'אק סין" with models like "JAC 40"
}


def _make_matches(listing: CarListing, record: dict) -> bool:
    ad_make, registry_make = _hebrew(listing.manufacturer), _hebrew(record.get("tozeret_nm"))
    if len(ad_make) >= 2 and ad_make[:3] == registry_make[:3]:
        return True
    alias = MAKE_ALIASES.get(ad_make)
    return bool(alias) and _latin(record.get("kinuy_mishari")).startswith(alias)


def _month(value) -> str | None:
    """'2023-9', '2023-09' or '2023-09-01T..' -> '2023-09'."""
    match = re.match(r"(\d{4})-(\d{1,2})", str(value or ""))
    return f"{match[1]}-{int(match[2]):02d}" if match else None


def _date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def _records(client: httpx.AsyncClient, resource: str, filters: dict, limit: int = 100) -> list[dict]:
    response = await client.get(
        REGISTRY_URL,
        params={"resource_id": resource, "filters": json.dumps(filters), "limit": limit},
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Registry lookup failed: {payload.get('error')}")
    return payload["result"]["records"]


async def vehicle_history(client: httpx.AsyncClient, plate: str) -> dict | None:
    """What the registry recorded about changes to the car since it was registered."""
    records = await _records(client, HISTORY_RESOURCE, {"mispar_rechev": int(plate)}, limit=1)
    if not records:
        return None
    record = records[0]
    return {
        "structural_change": bool(record.get("shinui_mivne_ind")),
        "gas_conversion": bool(record.get("gapam_ind")),
        "colour_change": bool(record.get("shnui_zeva_ind")),
        "tyre_change": bool(record.get("shinui_zmig_ind")),
        "km_at_last_test": record.get("kilometer_test_aharon") or None,
        "first_registered": record.get("rishum_rishon_dt") or None,
        "origin": (record.get("mkoriut_nm") or "").strip() or None,
    }


async def open_recalls(client: httpx.AsyncClient, plate: str) -> list[dict]:
    """Manufacturer recalls this car has not had done yet, oldest first."""
    records = await _records(client, RECALL_RESOURCE, {"MISPAR_RECHEV": int(plate)})
    recalls = [
        {
            "id": record.get("RECALL_ID"),
            "kind": record.get("SUG_RECALL"),
            "system": record.get("SUG_TAKALA"),
            "fault": " ".join(str(record.get("TEUR_TAKALA") or "").split()),
            "opened": record.get("TAARICH_PTICHA"),
        }
        for record in records
    ]
    return sorted(recalls, key=lambda recall: recall["opened"] or "")


async def new_price(client: httpx.AsyncClient, record: dict) -> dict | None:
    """The importer's list price for the car's model code when new: for its model year, else
    the nearest year listed. None when the model is not in the price list."""
    if record.get("tozeret_cd") is None or record.get("degem_cd") is None:
        return None
    filters = {"tozeret_cd": record["tozeret_cd"], "degem_cd": record["degem_cd"]}
    if record.get("sug_degem"):
        filters["sug_degem"] = record["sug_degem"]
    prices = [row for row in await _records(client, PRICE_RESOURCE, filters) if row.get("mehir")]
    if not prices:
        return None
    year = record.get("shnat_yitzur") or 0
    best = min(prices, key=lambda row: (abs((row.get("shnat_yitzur") or 0) - year), -(row.get("shnat_yitzur") or 0)))
    return {
        "price": best["mehir"],
        "year": best.get("shnat_yitzur"),
        "exact_year": best.get("shnat_yitzur") == year,
        "importer": best.get("shem_yevuan"),
    }


def registry_warnings(listing: CarListing, history: dict | None, recalls: list[dict]) -> list[str]:
    """Red flags from the registry's change history and open recalls, worded for the buyer."""
    warnings = []
    if history:
        if history["structural_change"]:
            warnings.append("Registry records a structural change (שינוי מבנה): usually a major repair or a modification")
        if history["gas_conversion"]:
            warnings.append("Registry records a conversion to run on gas (LPG)")
        if history["colour_change"]:
            warnings.append("Registry records a colour change: the car was repainted")
        if history["tyre_change"]:
            warnings.append("Registry records a tyre change from the original specification")
        if history["origin"]:
            warnings.append(f"Registry origin: {history['origin']}")
        registry_km = history["km_at_last_test"]
        if registry_km and listing.km is not None and listing.km < registry_km:
            warnings.append(
                f"Ad says {listing.km:,} km, but the registry recorded {registry_km:,} km at the last "
                "test: the odometer was turned back or the ad is wrong"
            )
    for recall in recalls:
        warnings.append(f"Recall not done yet (opened {recall['opened']}): {recall['fault']}")
    return warnings


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
    make_matches = _make_matches(listing, record)

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
                f"owner{'s' if summary['hands'] != 1 else ''} (not counting dealers): {_owner_list(summary)}"
            )
        ad_original = owner_type(attributes.get("original_owner"))
        first_owner = summary["first_owner"]
        if ad_original and first_owner and ad_original != first_owner and DEALER not in (ad_original, first_owner):
            discrepancies.append(
                f"Ad says the original owner was {OWNER_LABELS.get(ad_original, ad_original).lower()}, "
                f"registry's first owner was {OWNER_LABELS.get(first_owner, first_owner).lower()} "
                f"(from {summary['first_owner_since']})"
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
        "fields": side_by_side(listing, record, make_matches, model_matches, summary),
    }


def _owner_list(summary: dict) -> str:
    """"lease from 2025-10, private from 2026-01"."""
    return ", ".join(
        f"{OWNER_LABELS.get(owner, owner).lower()} from {month}" for owner, month in summary["owners"]
    )


def _shares_word(first: str | None, second: str | None) -> bool:
    """Hebrew values that name the same thing differently ("לבן" / "שנהב לבן", "חשמלי" / "חשמל")."""
    words = lambda text: {word[:4] for word in re.findall(r"[א-תA-Za-z]{2,}", text or "")}
    return bool(words(first) & words(second))


def side_by_side(
    listing: CarListing, record: dict, make_matches: bool, model_matches: bool, summary: dict | None = None
) -> list[dict]:
    """The ad and the registry record field by field, for a person to judge: each row is
    {"field", "ad", "registry", "differs"}; "differs" is None when either side is unknown."""
    attributes = listing.attributes or {}
    trim = record.get("ramat_gimur")
    ad_test, registry_test = _date(attributes.get("last_test_date")), _date(record.get("mivchan_acharon_dt"))
    ad_owner, registry_owner = owner_type(attributes.get("ownership")), owner_type(record.get("baalut"))
    rows = [
        ("Make", listing.manufacturer, record.get("tozeret_nm"), not make_matches),
        ("Model", " ".join(filter(None, (listing.model, listing.sub_model))) or listing.title,
         record.get("kinuy_mishari"), not model_matches),
        ("Trim", listing.sub_model, trim,
         _latin(trim) not in _latin(listing.sub_model) if trim and listing.sub_model else None),
        ("Year", listing.year, record.get("shnat_yitzur"), listing.year != record.get("shnat_yitzur")),
        ("On the road since", _month(attributes.get("on_road_since")), _month(record.get("moed_aliya_lakvish")),
         _month(attributes.get("on_road_since")) != _month(record.get("moed_aliya_lakvish"))),
        ("Last test", ad_test and f"{ad_test:%d/%m/%Y}", registry_test and f"{registry_test:%d/%m/%Y}",
         abs((ad_test - registry_test).days) > 45 if ad_test and registry_test else None),
        ("Ownership", attributes.get("ownership"), record.get("baalut"),
         ad_owner != registry_owner and DEALER not in (ad_owner, registry_owner)
         if ad_owner and registry_owner else None),
        *((
            ("Hand", listing.hand, f"{summary['hands']} ({_owner_list(summary)})",
             listing.hand != summary["hands"] if listing.hand and summary["hands"] else None),
            ("Original owner", attributes.get("original_owner"),
             OWNER_LABELS.get(summary["first_owner"], summary["first_owner"]) if summary["first_owner"] else None,
             owner_type(attributes.get("original_owner")) != summary["first_owner"]
             if owner_type(attributes.get("original_owner")) and summary["first_owner"] else None),
        ) if summary else ()),
        ("Fuel", attributes.get("engine_type"), record.get("sug_delek_nm"),
         not _shares_word(attributes.get("engine_type"), record.get("sug_delek_nm"))),
        ("Colour", attributes.get("color"), record.get("tzeva_rechev"),
         not _shares_word(attributes.get("color"), record.get("tzeva_rechev"))),
    ]
    return [
        {
            "field": field, "ad": ad, "registry": registry,
            "differs": None if ad in (None, "") or registry in (None, "") else bool(differs),
        }
        for field, ad, registry, differs in rows
    ]
