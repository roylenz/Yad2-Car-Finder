"""Try the haggling agent from a PC.

    PYTHONPATH=src python -m haggle list
    PYTHONPATH=src python -m haggle brief <token> --budget 80000 [--no-research]
    PYTHONPATH=src python -m haggle rehearse <token> --budget 80000            # you play the seller
    PYTHONPATH=src python -m haggle rehearse <token> --budget 80000 --auto-seller [--lowest 76000]
    PYTHONPATH=src python -m haggle call <token> --budget 80000 [--to +9725...]  # real phone call
    PYTHONPATH=src python -m haggle phone <token>                               # the seller's number

Reads the database at BOT_DB_PATH (or DATA_DIR/bot.sqlite3) and never writes to it; briefs and
call transcripts are saved under DATA_DIR/haggle/<token>/. Before the first brief Claude
researches the model online (new and 0 km prices, known faults), saved per model and year in
DATA_DIR/haggle/research/ and reused for 30 days. Needs ANTHROPIC_API_KEY (or an
`ant auth login` profile). HAGGLE_MODEL picks the Claude model.
"""

import argparse
import datetime
import json
import sys

from dotenv import load_dotenv
from sqlalchemy import select

from haggle import brief as briefs
from haggle import facts as car_facts
from haggle import research as online
from haggle.call import CallAgent, SimulatedSeller
from storage.db import data_dir, default_database_url, get_engine
from storage.schema import ads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haggle", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Cars on Yad2 now, cheapest first")
    phone = commands.add_parser("phone", help="The seller's number, read from the ad like a buyer does")
    phone.add_argument("token")
    phone.add_argument("--refresh", action="store_true", help="Look it up again")
    for name in ("brief", "rehearse", "call"):
        command = commands.add_parser(name)
        command.add_argument("token", help="Yad2 ad ID (the end of the ad's link)")
        command.add_argument("--budget", "--max", dest="budget", type=int, required=True,
                             help="The most you would pay; the brief is built around it")
        command.add_argument("--refresh", action="store_true", help="Write a new brief (and research) even if one is saved")
        command.add_argument("--no-research", action="store_true", help="Write the brief without searching the web")
    rehearse = commands.choices["rehearse"]
    rehearse.add_argument("--auto-seller", action="store_true", help="Claude plays the seller too")
    rehearse.add_argument("--lowest", type=int, help="Simulated seller's secret lowest price (default 5%% under asking)")
    rehearse.add_argument("--persona", help="Simulated seller's personality")
    rehearse.add_argument("--turns", type=int, default=24, help="Stop after this many caller turns")
    call = commands.choices["call"]
    call.add_argument("--to", help="Number to call, e.g. +97254...; default: the number on the ad")
    args = parser.parse_args(argv)
    load_dotenv()

    engine = get_engine(default_database_url())
    if args.command == "list":
        return _list(engine)
    if args.command == "phone":
        print(_seller_phone(args.token, args.refresh) or "")
        return 0
    facts = car_facts.gather(engine, args.token)
    if facts is None:
        print(f"No ad {args.token} in the database", file=sys.stderr)
        return 1
    folder = data_dir() / "haggle" / args.token
    folder.mkdir(parents=True, exist_ok=True)
    brief = _brief(facts, args, folder)
    if args.command == "brief":
        print(briefs.as_text(facts, brief))
        return 0
    if args.command == "call":
        return _call(facts, brief, args, folder)
    return _rehearse(facts, brief, args, folder)


def _call(facts: dict, brief: dict, args, folder) -> int:
    from haggle import voice

    to = args.to or _seller_phone(args.token)
    if not to:
        return 1
    placed = voice.place_call(facts, brief, to)
    conversation_id = placed.get("conversation_id")
    print(f"Calling {to} ({placed.get('message')}); conversation {conversation_id}")
    if not conversation_id:
        return 1
    conversation = voice.wait_for_result(conversation_id)
    path = voice.save_result(conversation, folder)
    for turn in conversation.get("transcript") or []:
        if turn.get("message"):
            speaker = "Caller" if turn["role"] == "agent" else "Seller"
            print(f"{speaker} ({turn.get('time_in_call_secs', 0)}s): {turn['message']}")
    print("\nOutcome:", json.dumps(voice.outcome(conversation), ensure_ascii=False, indent=1))
    print(f"Saved {path}")
    return 0


def _seller_phone(token: str, refresh: bool = False) -> str | None:
    from haggle import phone

    try:
        return phone.seller_phone(token, refresh)
    except phone.PhoneError as error:
        print(f"Couldn't get the seller's number: {error}", file=sys.stderr)
        return None


def _list(engine) -> int:
    with engine.connect() as db:
        rows = db.execute(
            select(ads.c.token, ads.c.title, ads.c.sub_model, ads.c.year, ads.c.km, ads.c.price)
            .where(ads.c.removed_at.is_(None), ads.c.price.is_not(None))
            .order_by(ads.c.price)
        ).all()
    for row in rows:
        print(f"{row.token:10} ₪{row.price:>8,}  {row.year or '':4}  {row.km or 0:>7,} km  {row.title} {row.sub_model or ''}")
    return 0


def _brief(facts: dict, args, folder) -> dict:
    path = folder / "brief.json"
    if path.exists() and not args.refresh:
        saved = json.loads(path.read_text())
        if saved.get("budget") == args.budget:
            return saved
    research = None
    if not args.no_research:
        print(f"Online research for {online.model_key(facts)}…", file=sys.stderr)
        research, new = online.for_model(facts, refresh=args.refresh)
        if not new:
            print(f"  (reusing the research from {research['date']})", file=sys.stderr)
    print("Preparing the negotiation brief…", file=sys.stderr)
    brief = briefs.build(facts, args.budget, research=research["notes"] if research else None)
    if research and new:
        brief["cost_usd"] = round(brief["cost_usd"] + research["cost_usd"], 4)
    path.write_text(json.dumps(brief, ensure_ascii=False, indent=1))
    (folder / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=1, default=str))
    return brief


def _rehearse(facts: dict, brief: dict, args, folder) -> int:
    print(briefs.as_text(facts, brief), "\n")
    agent = CallAgent(facts, brief)
    seller = None
    if args.auto_seller:
        lowest = args.lowest or int(facts["ad"]["asking_price"] * 0.95 // 500 * 500)
        seller = SimulatedSeller(facts, lowest, **({"persona": args.persona} if args.persona else {}))
        print(f"Simulated seller (secret lowest ₪{lowest:,})\n")
    else:
        print("You are the seller. Type what you say; an empty line ends the call.\n")
    said = agent.start()
    _show("Caller", said, agent.transcript[-1].seconds)
    turns = 1
    try:
        while not agent.ended and turns < args.turns:
            if seller:
                reply = seller.hear(said)
                _show("Seller", reply)
            else:
                reply = input("Seller> ").strip()
                if not reply:
                    break
            said = agent.hear(reply)
            _show("Caller", said, agent.transcript[-1].seconds)
            turns += 1
    except (KeyboardInterrupt, EOFError):
        print()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    record = {
        "brief": brief,
        "transcript": [turn.__dict__ for turn in agent.transcript],
        "outcome": agent.outcome,
        "simulated_seller": {"lowest": seller.lowest, "persona": seller.persona} if seller else None,
        "cost_usd": round(agent.cost_usd + (seller.cost_usd if seller else 0), 4),
    }
    path = folder / f"call-{stamp}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    print("\nOutcome:", json.dumps(agent.outcome, ensure_ascii=False, indent=1) if agent.outcome else "call ended without a report")
    seconds = [turn.seconds for turn in agent.transcript if turn.speaker == "caller"]
    print(f"Caller reply time: median {sorted(seconds)[len(seconds) // 2]:.1f}s · cost ${record['cost_usd']:.3f} · saved {path}")
    return 0


def _show(speaker: str, text: str, seconds: float | None = None) -> None:
    timing = f"  ({seconds:.1f}s)" if seconds else ""
    print(f"{speaker}: {text}{timing}")


if __name__ == "__main__":
    sys.exit(main())
