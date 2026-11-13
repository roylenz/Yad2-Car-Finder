"""Online research for a model before the brief: what it costs new now, price guides, known faults.

The database only knows Yad2's asking prices. A buyer also weighs what the same model costs new or
0 km (importer clearance deals can come close to a used car's price), so Claude searches the web
and writes short notes with a source for every figure. The brief reads them as one more input.

Research is per model and year, not per ad: it is saved in DATA_DIR/haggle/research/ and reused
by every ad of that model for MAX_AGE_DAYS.
"""

import datetime
import json
import re
from pathlib import Path

from haggle import claude
from storage.db import data_dir

SYSTEM = """You research the Israeli car market for a model that a buyer is about to buy used on \
Yad2. Today is {today}. Search the web, in Hebrew and in English, for:

1. What the same model costs new in Israel now: the importer's list price, and current new, \
0 km, stock-clearance or "0 ק"מ מליסינג" deals (with the price on the road, the model year and \
the date of each offer).
2. Openly published used-car prices for this model and year, if any.
3. Known faults, recalls or common complaints about this model.
4. Anything else that changes its value now: price cuts, a new model year, a change of importer.

Search in Hebrew first, with the model's Hebrew name, and include these searches: \
"<model> 0 ק"מ", "<model> חיסול מלאי" and "<model> ריקול". Clearance deals for 0 km cars are \
often far below the list price and matter most: look for them.

Report only what the sources say: give the date and the URL for every figure, prefer recent \
sources, and say plainly when you found nothing. Write concise English bullet points under the \
four headings, then one line on what it means for the used price of this model and year."""

# The basic search, without page fetches: the newer tools filter results by running code and
# fetched whole pages, which took 210k input tokens ($1) for one car. Search snippets are enough.
TOOLS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
WEB_SEARCH_USD = 0.01  # per search
MAX_CONTINUATIONS = 4
MAX_AGE_DAYS = 30


def model_key(facts: dict) -> str:
    """Make, model and year, e.g. "דונגפנג-בוקס-2025": the research is shared by their ads."""
    ad = facts["ad"]
    parts = [ad.get("manufacturer"), ad.get("model")] if ad.get("model") else [ad["title"]]
    text = "-".join(str(part) for part in (*parts, ad.get("year")) if part)
    return re.sub(r"[\s/\\]+", "-", text).strip("-")


def _path(facts: dict) -> Path:
    return data_dir() / "haggle" / "research" / f"{model_key(facts)}.json"


def for_model(facts: dict, refresh: bool = False, client=None) -> tuple[dict, bool]:
    """The model's saved research if it is recent enough, else new research (saved).

    Returns (research, is_new); research is {"model", "date", "notes", "cost_usd"}.
    """
    path = _path(facts)
    if path.exists() and not refresh:
        saved = json.loads(path.read_text())
        age = datetime.date.fromisoformat(facts["today"]) - datetime.date.fromisoformat(saved["date"])
        if age.days <= MAX_AGE_DAYS:
            return saved, False
    research = {"model": model_key(facts), "date": facts["today"], **run(facts, client)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(research, ensure_ascii=False, indent=1))
    path.with_suffix(".md").write_text(research["notes"] + "\n")
    return research, True


def run(facts: dict, client=None) -> dict:
    """Returns {"notes": text, "cost_usd": float}. Notes are plain text with source links."""
    client = client or claude.client()
    ad = facts["ad"]
    car = ", ".join(str(part) for part in (
        f"{ad.get('manufacturer') or ''} {ad.get('model') or ''}".strip() or ad["title"],
        ad.get("sub_model"), ad.get("year"),
    ) if part)
    messages = [{"role": "user", "content": f"The model: {car}."}]
    notes, cost = [], 0.0
    for _ in range(MAX_CONTINUATIONS + 1):
        response = client.beta.messages.create(
            model=claude.model(),
            max_tokens=16000,
            system=SYSTEM.format(today=facts["today"]),
            messages=messages,
            tools=TOOLS,
            output_config={"effort": "low"},
            **claude.FALLBACK,
        )
        cost += claude.cost_usd(response) + _searches(response) * WEB_SEARCH_USD
        notes.extend(block.text for block in response.content if block.type == "text")
        if response.stop_reason != "pause_turn":
            claude.check(response)
            break
        # The server paused its search loop: send the turn back as it is and it carries on.
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    return {"notes": "".join(notes).strip(), "cost_usd": round(cost, 4)}


def _searches(response) -> int:
    server = getattr(response.usage, "server_tool_use", None)
    return (getattr(server, "web_search_requests", 0) or 0) if server else 0
