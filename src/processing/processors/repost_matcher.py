"""Find earlier or parallel ads of the same car (reposts) and group them.

Compares an ad with every other ad of the same make and model:
- photos: perceptual hashes (`photo_hash`) within a few bits count as the same photo;
- description: text similarity of the seller's description.
A match needs 3+ shared photos, or a shared photo and a similar description, or a near-identical
description with the same subtype and almost the same mileage. Contradictions veto a match:
different licence plates, year, colour or hand, or a mileage that does not fit. Dealers reuse
one description template for all their cars, so a description shared by several other ads is
ignored. Same-plate ads are linked by `plate_registry`.
"""

import re
from difflib import SequenceMatcher

from sqlalchemy import Connection, Engine, and_, select

from processing.base import AdTarget, Processor
from processing.processors.photo_hash import distance
from storage import cars
from storage.schema import ads, analysis_results, photos

MAX_BITS = 6  # dHash bits that may differ for the "same" photo
MIN_PHOTOS = 3
SIMILAR_DESCRIPTION = 0.8
SAME_DESCRIPTION = 0.9
MIN_DESCRIPTION = 40  # characters; short descriptions are too generic to compare
TEMPLATE_USES = 3  # a description this similar to 3+ other ads is a dealer's template


def _description(attributes: dict | None) -> str:
    text = (attributes or {}).get("description") or ""
    return " ".join(re.sub(r"[^\w\s]", " ", text).split()).casefold()


def _km_fits(first: int | None, second: int | None) -> bool:
    if first is None or second is None:
        return True
    return abs(first - second) <= max(3000, 0.15 * max(first, second))


def _km_same(first: int | None, second: int | None) -> bool:
    if not first or not second:
        return False
    return abs(first - second) <= max(1000, 0.03 * max(first, second))


class RepostMatcherProcessor(Processor):
    name = "repost_matcher"
    version = "1"
    target = "ad"
    requires = ("photo_hash",)

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    async def run(self, target: AdTarget) -> dict:
        listing = target.listing
        own_hashes = [
            int(results["photo_hash"]["dhash"], 16)
            for results in target.photo_results.values()
            if (results.get("photo_hash") or {}).get("dhash")
        ]
        if not listing.manufacturer or not listing.model:
            return {"matches": [], "checked": 0}
        with self.engine.connect() as db:
            me = db.execute(select(ads).where(ads.c.token == target.token)).one()
            candidates = db.execute(
                select(ads).where(
                    ads.c.manufacturer == listing.manufacturer,
                    ads.c.model == listing.model,
                    ads.c.token != target.token,
                )
            ).all()
            hashes = self._hashes(db, [candidate.token for candidate in candidates])
        own_text = _description(me.attributes)
        similarities = {}
        if len(own_text) >= MIN_DESCRIPTION:
            for other in candidates:
                other_text = _description(other.attributes)
                if len(other_text) >= MIN_DESCRIPTION:
                    similarities[other.token] = SequenceMatcher(
                        None, own_text, other_text, autojunk=False
                    ).ratio()
        template = sum(1 for value in similarities.values() if value >= SAME_DESCRIPTION) >= TEMPLATE_USES
        matches = []
        for other in candidates:
            if self._contradicts(me, other):
                continue
            shared = sum(
                1 for mine in own_hashes
                if any(distance(mine, theirs) <= MAX_BITS for theirs in hashes.get(other.token, ()))
            )
            similarity = 0.0 if template else similarities.get(other.token, 0.0)
            reasons = []
            if shared:
                reasons.append(f"{shared} matching photo{'s' if shared != 1 else ''}")
            if similarity >= SIMILAR_DESCRIPTION:
                reasons.append(f"description {similarity:.0%} similar")
            same_details = (
                _km_same(me.km, other.km) and me.hand == other.hand and me.sub_model == other.sub_model
            )
            if (
                shared >= MIN_PHOTOS
                or (shared >= 1 and similarity >= SIMILAR_DESCRIPTION)
                or (similarity >= SAME_DESCRIPTION and same_details)
            ):
                if not shared and similarity >= SAME_DESCRIPTION:
                    reasons.append("same subtype, hand and mileage")
                matches.append({"token": other.token, "reason": ", ".join(reasons)})
        return {
            "matches": matches,
            "checked": len(candidates),
            "photos": len(own_hashes),
            "template_description": template,
        }

    @staticmethod
    def _contradicts(me, other) -> bool:
        if me.plate and other.plate and me.plate != other.plate:
            return True
        if me.year and other.year and me.year != other.year:
            return True
        if me.hand and other.hand and me.hand != other.hand:
            return True
        mine, theirs = (me.attributes or {}).get("color"), (other.attributes or {}).get("color")
        if mine and theirs and mine != theirs:
            return True
        return not _km_fits(me.km, other.km)

    @staticmethod
    def _hashes(db: Connection, tokens: list[str]) -> dict[str, list[int]]:
        if not tokens:
            return {}
        rows = db.execute(
            select(photos.c.token, analysis_results.c.result).join(
                analysis_results,
                and_(
                    analysis_results.c.target_type == "photo",
                    analysis_results.c.target_id == photos.c.sha256,
                    analysis_results.c.processor == "photo_hash",
                    analysis_results.c.status == "ok",
                ),
            ).where(photos.c.token.in_(tokens))
        ).all()
        found: dict[str, list[int]] = {}
        for token, result in rows:
            if result and result.get("dhash"):
                found.setdefault(token, []).append(int(result["dhash"], 16))
        return found

    def apply(self, db: Connection, target_id: str, result: dict) -> None:
        for match in result.get("matches", []):
            cars.link(db, target_id, match["token"], match["reason"])
