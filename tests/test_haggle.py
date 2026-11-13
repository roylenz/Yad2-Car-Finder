import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.state import BotState
from car.listing import CarListing, ScanOutput
from haggle import brief as briefs
from haggle import facts as car_facts
from haggle.call import CallAgent, SimulatedSeller
from tracking.tracker import AdTracker

SEARCH = "https://www.yad2.co.il/vehicles/cars?manufacturer=19&price=-1-120000"


def _response(*blocks, model="claude-opus-5-5"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason="end_turn", model=model, usage=usage)


def _text(text):
    return SimpleNamespace(type="text", text=text)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


BRIEF = {
    "summary": "Listed for 50 days with cheaper twins on the market.",
    "fair_price": 86000, "opening_offer": 82000, "target_price": 85000, "walk_away_price": 88000,
    "leverage": [{"point": "Long on the market", "evidence": "50 days", "strength": "high", "say_in_hebrew": "הרכב מפורסם כבר 50 יום"}],
    "questions": [{"why": "Service history", "ask_in_hebrew": "היו טיפולים במוסך מורשה?"}],
    "red_flags": [],
}


class HaggleTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.state = BotState(str(Path(self._directory.name) / "bot.sqlite3"))
        search, _ = self.state.add_search(1, SEARCH, 10)
        tracker = AdTracker(self.state.ads)
        start = datetime.datetime(2026, 9, 1)

        def car(token, price, km, year=2024):
            return CarListing(
                f"https://www.yad2.co.il/vehicles/item/{token}", "טויוטה קורולה", price, km,
                year=year, manufacturer="טויוטה", model="קורולה", sub_model="Sun היברידי",
                attributes={"description": "שמור מאוד"},
            )

        tracker.apply(1, search.id, ScanOutput([car("target01", 99000, 30000), car("other001", 90000, 40000)]), start)
        tracker.apply(1, search.id, ScanOutput([car("target01", 95000, 30000), car("other001", 90000, 40000),
                                                car("other002", 93000, 20000)]), start + datetime.timedelta(days=20))

    def tearDown(self):
        self._directory.cleanup()

    def test_facts_cover_history_and_market(self):
        facts = car_facts.gather(self.state.engine, "target01", now=datetime.datetime(2026, 10, 1))
        self.assertEqual(facts["ad"]["asking_price"], 95000)
        self.assertEqual(facts["today"], "2026-10-01")
        self.assertEqual(facts["listing"]["days_on_market"], 30)
        self.assertEqual(facts["listing"]["total_drop"], 4000)
        self.assertEqual(facts["listing"]["price_changes"], [{"date": "2026-09-21", "from": 99000, "to": 95000}])
        self.assertEqual(facts["market"]["comparables"], 2)
        self.assertEqual(facts["market"]["cheaper_than_this"], 2)
        self.assertEqual(facts["market"]["same_sub_model"]["median_price"], 91500)
        self.assertIsNone(car_facts.gather(self.state.engine, "missing"))

    def test_brief_never_goes_above_the_buyers_maximum(self):
        facts = car_facts.gather(self.state.engine, "target01")
        answer = {**BRIEF, "opening_offer": 89000, "target_price": 90000, "walk_away_price": 92000}
        client = FakeClient(_response(_text(json.dumps(answer, ensure_ascii=False))))
        brief = briefs.build(facts, max_price=88000, client=client)
        self.assertEqual((brief["opening_offer"], brief["target_price"], brief["walk_away_price"]), (88000, 88000, 88000))
        request = client.requests[0]
        self.assertIn("₪88,000", request["messages"][0]["content"])
        self.assertEqual(request["output_config"]["format"]["schema"], briefs.SCHEMA)
        self.assertIn("Fair price", briefs.as_text(facts, brief))

    def test_call_runs_until_the_caller_reports_the_outcome(self):
        facts = car_facts.gather(self.state.engine, "target01")
        outcome = {
            "result": "seller_lowest_known", "seller_lowest_price": 90000, "last_offer_made": 87000,
            "answers": ["Serviced at the dealer"], "summary": "Seller would take 90,000.", "next_step": "Call back",
        }
        client = FakeClient(
            _response(_text("שלום, אני עוזר בינה מלאכותית שמתקשר בשם קונה.")),
            _response(_text("תודה, להתראות."), SimpleNamespace(type="tool_use", name="end_call", input=outcome, id="t1")),
        )
        agent = CallAgent(facts, {**BRIEF, "model": "x", "cost_usd": 0}, client=client)
        self.assertIn("בינה מלאכותית", agent.start())
        self.assertFalse(agent.ended)
        self.assertEqual(agent.hear("90 אלף, לא פחות"), "תודה, להתראות.")
        self.assertEqual(agent.outcome, outcome)
        self.assertEqual([turn.speaker for turn in agent.transcript], ["caller", "seller", "caller"])
        system = client.requests[0]["system"][0]["text"]
        self.assertIn("₪88,000", system)  # walk-away limit
        self.assertIn("AI assistant", system)
        self.assertNotIn("cost_usd", system)
        # The history is append-only: the second request starts with the first exchange unchanged.
        self.assertEqual(len(client.requests[1]["messages"]), 3)
        self.assertGreater(agent.cost_usd, 0)

    def test_offer_above_walk_away_is_flagged(self):
        facts = car_facts.gather(self.state.engine, "target01")
        outcome = {"result": "no_agreement", "seller_lowest_price": None, "last_offer_made": 91000,
                   "answers": [], "summary": "", "next_step": ""}
        client = FakeClient(_response(SimpleNamespace(type="tool_use", name="end_call", input=outcome, id="t1")))
        agent = CallAgent(facts, BRIEF, client=client)
        agent.start()
        self.assertIn("warning", agent.outcome)

    def test_simulated_seller_keeps_its_lowest_price_secret_in_the_prompt(self):
        facts = car_facts.gather(self.state.engine, "target01")
        client = FakeClient(_response(_text("כן, הרכב עדיין למכירה")))
        seller = SimulatedSeller(facts, 90000, client=client)
        self.assertEqual(seller.hear("שלום"), "כן, הרכב עדיין למכירה")
        self.assertIn("₪90,000", client.requests[0]["system"])


class VoiceTests(unittest.TestCase):
    def test_agent_config_keeps_the_rehearsal_rules_and_speaks_hebrew(self):
        from haggle import voice

        config = voice.agent_config()
        agent = config["conversation_config"]["agent"]
        self.assertEqual(agent["language"], "he")
        self.assertTrue(agent["disable_first_message_interruptions"])
        self.assertIn("בינה מלאכותית", agent["first_message"])
        self.assertIn("מוקלטת", agent["first_message"])  # recording notice
        prompt = agent["prompt"]["prompt"]
        for placeholder in ("{{walk_away}}", "{{brief}}", "{{facts}}"):
            self.assertIn(placeholder, prompt)
        self.assertIn("Never commit to buying", prompt)
        self.assertIn("ONE question per turn", prompt)
        self.assertIn("המחיר גמיש", agent["first_message"])
        self.assertTrue(config["conversation_config"]["turn"]["speculative_turn"])
        self.assertTrue(config["conversation_config"]["vad"]["background_voice_detection"])
        self.assertIn("end_call", agent["prompt"]["built_in_tools"])
        self.assertEqual(config["conversation_config"]["tts"]["model_id"], "eleven_v4_turbo")
        self.assertIn("seller_lowest_price", config["platform_settings"]["data_collection"])

    def test_dynamic_variables_and_outcome(self):
        from haggle import voice

        facts = {"today": "2026-11-13", "ad": {"title": "דונגפנג בוקס", "year": 2025, "url": "u"}, "listing": {},
                 "market": {"comparables": 3, "median_price": 80000, "cheapest_on_yad2_now": []},
                 "registry": {"plate_confirmed": True, "ownership": {"owners_not_counting_dealers": 1, "history": [1, 2]}},
                 "condition": None}
        variables = voice.dynamic_variables(facts, {**BRIEF, "model": "x", "cost_usd": 1})
        self.assertEqual(variables["car_title"], "דונגפנג בוקס")
        self.assertEqual(variables["walk_away"], "₪88,000")
        self.assertNotIn("cost_usd", variables["brief"])
        sent = json.loads(variables["facts"])
        self.assertEqual(sent["market"], {"comparables": 3, "median_price": 80000})
        self.assertNotIn("history", sent["registry"]["ownership"])
        self.assertNotIn("url", sent["ad"])
        conversation = {
            "status": "done",
            "analysis": {"transcript_summary": "s", "data_collection_results": {
                "seller_lowest_price": {"value": 76000}, "result": {"value": "seller_lowest_known"}}},
            "metadata": {"call_duration_secs": 120, "phone_call": {"call_result": "answered"}},
        }
        self.assertEqual(voice.outcome(conversation)["seller_lowest_price"], 76000)
        self.assertEqual(voice.outcome(conversation)["call_result"], "answered")


if __name__ == "__main__":
    unittest.main()
