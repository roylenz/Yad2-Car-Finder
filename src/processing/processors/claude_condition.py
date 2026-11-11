"""Ask Claude to inspect an ad's photos for damage and anything else that could lower the price.

One request per active ad: up to MAX_PHOTOS photos plus the ad's facts and description, answered
as structured JSON (damage, price factors, odometer reading, whether the photos match the ad).
Needs ANTHROPIC_API_KEY. Spending is tracked from each response's token usage; once
CLAUDE_MONTHLY_BUDGET_USD is spent in a calendar month (UTC), ads wait for the next month.
"""

import base64
import datetime
import json
import os

import anthropic
from sqlalchemy import Engine, select

from common.logger import logger
from processing.base import AdTarget, Deferred, Processor
from storage.db import utcnow
from storage.schema import analysis_results

MAX_PHOTOS = 16
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_BUDGET_USD = 20.0
# $ per million tokens (input, output). The fallback model may answer instead of the requested one.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

SYSTEM = """You inspect photos of a used car advertised for sale in Israel on Yad2, for a buyer \
who wants to know what could lower the price or deserves a closer look before buying.

Report only what the photos or the ad text actually show, and cite photo numbers. Do not guess \
about things that are not visible; if the photos are too few or too poor to judge, say so. \
Look for: dents, scratches, rust, cracked glass or lights, mismatched paint or panel gaps \
(signs of repair), curb-rashed wheels, worn or damaged tyres, interior wear, stains, damaged \
seats or steering wheel, dashboard warning lights, missing parts, aftermarket modifications, \
signs of commercial use (taxi, leasing, rental), and stock or dealer photos that do not show \
this specific car. If the odometer is visible, read it. Check that the photos show the \
advertised make, model and colour. The description is usually in Hebrew; note anything in it \
that could lower the price (accidents, repairs, chassis damage, defects, urgency to sell).

Write in concise English. Use an empty list when there is nothing to report."""

SCHEMA = {
    "type": "object",
    "properties": {
        "overall_condition": {"type": "string", "enum": ["excellent", "good", "fair", "poor", "unclear"]},
        "summary": {"type": "string", "description": "Two or three sentences for the buyer"},
        "damage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "photo": {"type": "integer"},
                    "location": {"type": "string"},
                    "kind": {"type": "string"},
                    "severity": {"type": "string", "enum": ["minor", "moderate", "major"]},
                    "description": {"type": "string"},
                },
                "required": ["photo", "location", "kind", "severity", "description"],
                "additionalProperties": False,
            },
        },
        "price_factors": {
            "type": "array",
            "description": "Observations a buyer could use to negotiate the price down",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "evidence": {"type": "string", "description": "Photo numbers or a quote from the ad"},
                    "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["issue", "evidence", "impact"],
                "additionalProperties": False,
            },
        },
        "positives": {"type": "array", "items": {"type": "string"}},
        "odometer_km": {"type": ["integer", "null"], "description": "Reading visible in a photo"},
        "odometer_photo": {"type": ["integer", "null"]},
        "photos_match_ad": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "match_notes": {"type": "string"},
    },
    "required": [
        "overall_condition", "summary", "damage", "price_factors", "positives",
        "odometer_km", "odometer_photo", "photos_match_ad", "match_notes",
    ],
    "additionalProperties": False,
}

_AD_FACTS = (
    ("price", "Price (₪)"), ("km", "Advertised mileage (km)"), ("year", "Year"), ("hand", "Hand"),
)
_ATTRIBUTE_FACTS = (
    ("color", "Colour"), ("ownership", "Ownership"), ("on_road_since", "On the road since"),
    ("last_test_date", "Last test"), ("km_at_last_test", "Km at last test"),
    ("seller_type", "Seller"), ("engine_type", "Engine"), ("body_type", "Body"),
)


class ClaudeConditionProcessor(Processor):
    name = "claude_condition"
    version = "1"
    target = "ad"
    active_only = True

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._client = client
        self.model = os.getenv("CLAUDE_CONDITION_MODEL", DEFAULT_MODEL)
        self.budget = float(os.getenv("CLAUDE_MONTHLY_BUDGET_USD", DEFAULT_BUDGET_USD))
        self.engine: Engine | None = None

    @staticmethod
    def available() -> str | None:
        return None if os.getenv("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY is not set"

    def setup(self, engine: Engine) -> None:
        self.engine = engine

    def spent_this_month(self) -> float:
        if self.engine is None:
            return 0.0
        month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self.engine.connect() as db:
            rows = db.execute(
                select(analysis_results.c.result).where(
                    analysis_results.c.processor == self.name,
                    analysis_results.c.created_at >= month_start,
                )
            ).scalars()
            return sum(((row or {}).get("usage") or {}).get("cost_usd", 0) for row in rows)

    async def run(self, target: AdTarget) -> dict:
        if not target.photos:
            return {"skipped": "No photos"}
        spent = self.spent_this_month()
        if spent >= self.budget:
            raise Deferred(f"Monthly Claude budget spent (${spent:.2f} of ${self.budget:.2f})")
        photos = target.photos[:MAX_PHOTOS]
        content = []
        for number, photo in enumerate(photos, 1):
            content.append({"type": "text", "text": f"Photo {number}:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _media_type(photo.path.suffix),
                        "data": base64.standard_b64encode(photo.path.read_bytes()).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": _describe(target)})

        client = self._client or anthropic.AsyncAnthropic()
        response = await client.beta.messages.create(
            model=self.model,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            # On a safety decline, retry on the model Anthropic picks for that category.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        usage = _usage(response)
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined ({usage['cost_usd']:.4f} USD spent)")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's answer was cut off")
        text = next(block.text for block in response.content if block.type == "text")
        result = json.loads(text)
        logger.info("Claude inspected ad", token=target.token, cost_usd=usage["cost_usd"])
        return {**result, "model": response.model, "photos_sent": len(photos), "photos_total": len(target.photos), "usage": usage}


def _media_type(suffix: str) -> str:
    return {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(suffix.lower(), "image/jpeg")


def _describe(target: AdTarget) -> str:
    listing = target.listing
    attributes = listing.attributes or {}
    lines = [f"Advertised car: {listing.title}" + (f" ({listing.sub_model})" if listing.sub_model else "")]
    for field, label in _AD_FACTS:
        value = getattr(listing, field)
        if value is not None:
            lines.append(f"{label}: {value}")
    for key, label in _ATTRIBUTE_FACTS:
        if attributes.get(key) not in (None, ""):
            lines.append(f"{label}: {attributes[key]}")
    if attributes.get("description"):
        lines.append(f"Seller's description:\n{attributes['description']}")
    lines.append("Inspect the photos above and report as specified.")
    return "\n".join(lines)


def _usage(response) -> dict:
    usage = response.usage
    price_in, price_out = PRICES.get(response.model, PRICES[DEFAULT_MODEL])
    input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (input_tokens * price_in + cached * price_in * 0.1 + (usage.output_tokens or 0) * price_out) / 1e6
    return {
        "input_tokens": input_tokens,
        "cache_read_tokens": cached,
        "output_tokens": usage.output_tokens,
        "cost_usd": round(cost, 5),
        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
