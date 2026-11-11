"""Decide each ad's licence plate and check it against the government vehicle registry.

Votes over the `plate_reader` results of the ad's photos, looks the best candidates up on
data.gov.il, and keeps the first one the registry lists as the advertised car. The plate is
saved on the ad (`ads.plate`, `ads.plate_status`) so other ads of the same car can be found.
"""

import httpx
from sqlalchemy import Connection, update

from processing import plates
from processing.base import AdTarget, Processor
from storage.schema import ads


class PlateRegistryProcessor(Processor):
    name = "plate_registry"
    version = "1"
    target = "ad"
    requires = ("plate_reader",)

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def run(self, target: AdTarget) -> dict:
        readings = [
            reading
            for results in target.photo_results.values()
            for reading in (results.get("plate_reader") or {}).get("plates", [])
        ]
        candidates = plates.vote(readings)
        result = {
            "plate": None,
            "status": "unread",
            "candidates": [list(candidate) for candidate in candidates[:5]],
            "photos_with_plate": sum(
                1 for results in target.photo_results.values()
                if (results.get("plate_reader") or {}).get("plates")
            ),
        }
        trusted = [(plate, score) for plate, score in candidates if score >= plates.MIN_SCORE]
        if not trusted:
            return result
        client = self._client or httpx.AsyncClient(
            timeout=20, headers={"User-Agent": "yad2-car-finder"}
        )
        try:
            unverified = None
            for plate, score in trusted[: plates.MAX_CANDIDATES]:
                record = await plates.lookup(client, plate)
                if record is None:
                    unverified = unverified or (plate, score)
                    continue
                comparison = plates.compare(target.listing, record)
                if comparison["same_car"]:
                    return {
                        **result,
                        "plate": plate,
                        "score": score,
                        "status": "confirmed",
                        "registry": record,
                        **comparison,
                    }
                result.setdefault("rejected", []).append(
                    {"plate": plate, "registry_model": record.get("kinuy_mishari"), "year": record.get("shnat_yitzur")}
                )
        finally:
            if self._client is None:
                await client.aclose()
        if unverified:
            # Not in this registry dataset (e.g. some trucks); keep it, marked as unverified.
            return {**result, "plate": unverified[0], "score": unverified[1], "status": "unverified"}
        return {**result, "status": "mismatch"}

    def apply(self, db: Connection, target_id: str, result: dict) -> None:
        keep = result.get("status") in ("confirmed", "unverified")
        db.execute(
            update(ads)
            .where(ads.c.token == target_id)
            .values(plate=result["plate"] if keep else None, plate_status=result.get("status"))
        )
