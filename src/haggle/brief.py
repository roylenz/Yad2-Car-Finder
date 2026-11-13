"""A negotiation brief for one car: what it is worth, what to offer and the arguments to use."""

import json

from haggle import claude

SYSTEM = """You prepare a buyer to negotiate the price of a used car advertised on Yad2 in Israel.

You get everything known about the car as JSON: the ad, how long it has been listed and every \
price change (reposts included), asking prices of comparable cars, the vehicle registry check \
(ownership history, test dates, where the ad and registry disagree) and an inspection of the \
ad's photos. Prices are in shekels and are asking prices, not sale prices. `today` is the date of the call;
work out how far away any date is from it.

Estimate a fair price, then suggest an opening offer, a target and a walk-away price. Base every \
argument on the facts given, and cite the evidence (numbers, dates, photo numbers, registry \
fields). Never invent facts, other offers or competing cars that are not in the data. Strong \
leverage is usually: long time on the market, repeated price drops or reposts, cheaper \
comparable cars, ownership the ad hides (dealer, rental, company, more hands), damage or wear \
in the photos, a test due soon. If something is unknown, say what to ask the seller instead.

Write the summary, leverage and questions in concise English for the buyer. Write the Hebrew \
fields as natural spoken Hebrew a polite caller would say on the phone."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Three or four sentences for the buyer"},
        "fair_price": {"type": "integer"},
        "opening_offer": {"type": "integer"},
        "target_price": {"type": "integer"},
        "walk_away_price": {"type": "integer"},
        "leverage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "evidence": {"type": "string"},
                    "strength": {"type": "string", "enum": ["low", "medium", "high"]},
                    "say_in_hebrew": {"type": "string"},
                },
                "required": ["point", "evidence", "strength", "say_in_hebrew"],
                "additionalProperties": False,
            },
        },
        "questions": {
            "type": "array",
            "description": "What to ask the seller before or while negotiating",
            "items": {
                "type": "object",
                "properties": {"why": {"type": "string"}, "ask_in_hebrew": {"type": "string"}},
                "required": ["why", "ask_in_hebrew"],
                "additionalProperties": False,
            },
        },
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary", "fair_price", "opening_offer", "target_price", "walk_away_price",
        "leverage", "questions", "red_flags",
    ],
    "additionalProperties": False,
}


def build(facts: dict, max_price: int | None = None, client=None) -> dict:
    """Ask Claude for the brief. `max_price` is the buyer's ceiling; offers never go above it."""
    request = "Car facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1, default=str)
    if max_price:
        request += f"\n\nThe buyer will not pay more than ₪{max_price:,}. Keep every price at or below that."
    response = (client or claude.client()).beta.messages.create(
        model=claude.model(),
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": request}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        **claude.FALLBACK,
    )
    claude.check(response)
    brief = json.loads(next(block.text for block in response.content if block.type == "text"))
    if max_price:
        for key in ("opening_offer", "target_price", "walk_away_price"):
            brief[key] = min(brief[key], max_price)
    brief["opening_offer"] = min(brief["opening_offer"], brief["target_price"], brief["walk_away_price"])
    brief["max_price"] = max_price
    brief["model"] = response.model
    brief["cost_usd"] = round(claude.cost_usd(response), 4)
    return brief


def as_text(facts: dict, brief: dict) -> str:
    ad = facts["ad"]
    lines = [
        f"{ad['title']} {ad.get('sub_model') or ''} {ad.get('year') or ''}".strip(),
        f"Asking ₪{ad['asking_price']:,} · {facts['listing']['days_on_market']} days on the market · {ad['url']}",
        "",
        brief["summary"],
        "",
        f"Fair price ≈ ₪{brief['fair_price']:,}",
        f"Open at ₪{brief['opening_offer']:,} · aim for ₪{brief['target_price']:,} · walk away above ₪{brief['walk_away_price']:,}",
        "",
        "Leverage:",
    ]
    for item in brief["leverage"]:
        lines.append(f"  [{item['strength']}] {item['point']} ({item['evidence']})")
        lines.append(f"      {item['say_in_hebrew']}")
    lines.append("Ask the seller:")
    for item in brief["questions"]:
        lines.append(f"  {item['ask_in_hebrew']}  ({item['why']})")
    if brief["red_flags"]:
        lines.append("Red flags:")
        lines.extend(f"  {flag}" for flag in brief["red_flags"])
    lines.append(f"\n({brief['model']}, ${brief['cost_usd']:.3f})")
    return "\n".join(lines)
