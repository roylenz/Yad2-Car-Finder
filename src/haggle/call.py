"""The phone call, as text: Claude calls the seller for the buyer; the seller is you or a simulation.

The same `CallAgent` will later get speech-to-text in and text-to-speech out. Each turn returns
what the caller says next, and the call ends when Claude reports the outcome with `end_call`.
"""

import json
import time
from dataclasses import dataclass, field

from haggle import claude

AGENT_SYSTEM = """You are an AI assistant phoning a private seller in Israel about the used car \
they advertised on Yad2, on behalf of a buyer. Speak natural, casual, spoken Hebrew. Say only \
what the caller says aloud, with no stage directions or notes.

Be brief and get to the point: this is a phone call, and the seller's time matters.
- One short sentence per turn, two at most. No small talk, no recaps of what the seller said.
- Ask exactly ONE question per turn, then stop and wait. Never put two questions in one turn, \
not even with "and".
- Don't explain why you ask, and don't announce what you are about to do ("I have a few questions").

Introduce yourself in one short sentence: an AI assistant calling for a buyer. Never pretend \
to be human; if asked, confirm you are an AI. If the seller does not want to talk to an AI, \
apologise, say the buyer may call personally, and end the call.

The order of the call:
1. Is the price negotiable? (If the car is sold, thank them and end the call.)
2. Is there anything the buyer should know about the car: damage, accidents, faults?
3. At most one or two of the brief's questions, only those that matter for the price.
4. Negotiate: start at the opening offer and work towards the target, using one piece of \
leverage from the brief at a time, in a few words. Raise your offer in small steps and only when \
the seller moves too. When the seller names a lowest price, try once for a little less; then \
accept it if it is within the limit, or thank them and end the call if not.

Hard limits:
- Never offer or agree to more than the walk-away price ({walk_away}).
- Never commit to buying, to a deposit, a payment, or a meeting time. Say the buyer will call \
back to confirm and arrange a viewing.
- Use only facts from the brief and the car data. Never invent other offers, other cars, a \
deadline or anything else.
- Do not share anything about the buyer (name, phone, address, finances).
- If the seller is rude, busy or clearly asks to stop, thank them and end the call.

When the conversation is over, say a short goodbye and call end_call with the outcome.

The negotiation brief:
{brief}

The car data:
{facts}"""

END_CALL = {
    "name": "end_call",
    "description": "Hang up and report the outcome to the buyer. Call it after your goodbye line.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "enum": [
                    "price_agreed_in_principle", "seller_lowest_known", "no_agreement",
                    "already_sold", "declined_ai_call", "callback_requested", "other",
                ],
            },
            "seller_lowest_price": {"type": ["integer", "null"], "description": "Lowest price the seller said they would accept"},
            "last_offer_made": {"type": ["integer", "null"]},
            "answers": {"type": "array", "items": {"type": "string"}, "description": "What the seller said about the buyer's questions"},
            "summary": {"type": "string", "description": "Two or three sentences in English for the buyer"},
            "next_step": {"type": "string"},
        },
        "required": ["result", "seller_lowest_price", "last_offer_made", "answers", "summary", "next_step"],
        "additionalProperties": False,
    },
}

SELLER_SYSTEM = """You play a private seller in Israel who advertised this used car on Yad2 and \
now answers a phone call. Speak natural spoken Hebrew in short turns, like a real phone call. \
Stay in character; say only what the seller says aloud.

Personality: {persona}
You want as much as possible, but you would accept ₪{lowest:,} at the very lowest, and you do not \
reveal that number unless the buyer gets close and you are convinced. Answer questions about the \
car consistently with the ad below; invent ordinary, plausible details only where the ad is \
silent (service history, why you are selling). If the caller pushes hard with real facts \
(long time on the market, cheaper cars, damage), concede a little. When the call ends, say goodbye.

The ad:
{ad}"""


def essentials(facts: dict) -> dict:
    """The car data the caller needs on the phone: the brief already carries the analysis.

    A shorter prompt answers sooner, so the market's comparables, the ownership history and the
    photo inspection's details are left to the brief.
    """
    ad = {key: value for key, value in facts["ad"].items() if key not in ("token", "url") and value not in (None, "")}
    market = {key: facts["market"].get(key) for key in ("comparables", "median_price", "min_price", "cheaper_than_this")
              if facts["market"].get(key) is not None}
    registry = {key: value for key, value in facts.get("registry", {}).items() if key != "ownership"}
    ownership = facts.get("registry", {}).get("ownership")
    if ownership:
        registry["ownership"] = {key: value for key, value in ownership.items() if key != "history"}
    condition = facts.get("condition") or {}
    return {
        "today": facts["today"],
        "ad": ad,
        "listing": facts["listing"],
        "market": market,
        "registry": registry,
        "condition": {key: condition[key] for key in ("overall_condition", "damage") if condition.get(key)},
    }


@dataclass
class Turn:
    speaker: str  # "caller" or "seller"
    text: str
    seconds: float = 0.0


@dataclass
class CallAgent:
    facts: dict
    brief: dict
    client: object = None
    messages: list = field(default_factory=list)
    transcript: list[Turn] = field(default_factory=list)
    outcome: dict | None = None
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        self.client = self.client or claude.client()
        brief = {key: value for key, value in self.brief.items() if key not in ("model", "cost_usd")}
        self.system = AGENT_SYSTEM.format(
            walk_away=f"₪{self.brief['walk_away_price']:,}",
            brief=json.dumps(brief, ensure_ascii=False, indent=1),
            facts=json.dumps(essentials(self.facts), ensure_ascii=False, indent=1, default=str),
        )

    @property
    def ended(self) -> bool:
        return self.outcome is not None

    def start(self) -> str:
        """The seller picked up: the caller's first line."""
        return self._respond("[The seller answers the phone: \"הלו?\"]")

    def hear(self, seller_text: str) -> str:
        self.transcript.append(Turn("seller", seller_text))
        return self._respond(seller_text)

    def _respond(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        started = time.monotonic()
        response = self.client.beta.messages.create(
            model=claude.model(),
            max_tokens=4000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=list(self.messages),
            tools=[END_CALL],
            output_config={"effort": "low"},
            **claude.FALLBACK,
        )
        claude.check(response)
        self.cost_usd += claude.cost_usd(response)
        # Keep the whole reply (thinking and tool blocks too): the history stays append-only.
        self.messages.append({"role": "assistant", "content": response.content})
        said = " ".join(block.text.strip() for block in response.content if block.type == "text").strip()
        for block in response.content:
            if block.type == "tool_use" and block.name == "end_call":
                self.outcome = dict(block.input)
        if self.outcome and self.outcome.get("last_offer_made") and self.outcome["last_offer_made"] > self.brief["walk_away_price"]:
            self.outcome["warning"] = "The caller offered more than the walk-away price"
        self.transcript.append(Turn("caller", said, round(time.monotonic() - started, 1)))
        return said


@dataclass
class SimulatedSeller:
    facts: dict
    lowest: int
    persona: str = "Friendly but firm; in no hurry to sell; drops the price only for good reasons."
    client: object = None
    messages: list = field(default_factory=list)
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        self.client = self.client or claude.client()
        self.system = SELLER_SYSTEM.format(
            persona=self.persona, lowest=self.lowest,
            ad=json.dumps(self.facts["ad"], ensure_ascii=False, indent=1, default=str),
        )

    def hear(self, caller_text: str) -> str:
        self.messages.append({"role": "user", "content": caller_text or "..."})
        response = self.client.beta.messages.create(
            model=claude.model(),
            max_tokens=2000,
            system=self.system,
            messages=list(self.messages),
            output_config={"effort": "low"},
            **claude.FALLBACK,
        )
        claude.check(response)
        self.cost_usd += claude.cost_usd(response)
        self.messages.append({"role": "assistant", "content": response.content})
        return " ".join(block.text.strip() for block in response.content if block.type == "text").strip()
