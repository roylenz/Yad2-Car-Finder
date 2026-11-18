import asyncio
import datetime
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from bot.state import BotState
from car.listing import CarListing
from dashboard import auth
from dashboard.app import create_app
from processing import plates
from processing.base import PhotoTarget, Processor
from processing.processors.claude_condition import ClaudeConditionProcessor
from processing.processors.plate_registry import PlateRegistryProcessor
from processing.registry import enabled_processors
from processing.runner import ProcessingRunner
from storage.photos import LocalPhotoStore
from storage.schema import ads, analysis_results

FIRST = "https://www.yad2.co.il/vehicles/item/limo0001"
SECOND = "https://www.yad2.co.il/vehicles/item/limo0002"
RECORD = {
    "mispar_rechev": 13107604,
    "tozeret_nm": "איויאיסי סין",
    "kinuy_mishari": "LIMO",
    "degem_nm": "EV4",
    "ramat_gimur": "PREMIUM TECH",
    "shnat_yitzur": 2024,
    "moed_aliya_lakvish": "2024-1",
    "mivchan_acharon_dt": "2024-01-04",
    "tokef_dt": "2027-01-03",
    "baalut": "סוחר",
    "tzeva_rechev": "לבן",
    "sug_delek_nm": "חשמל",
    "misgeret": "LJ1234567890",
}


def limo(url: str, **changes) -> CarListing:
    values = dict(
        url=url,
        title="אי.וי איזי Limo",
        price=95000,
        year=2024,
        manufacturer="אי.וי איזי",
        model="Limo",
        images=(f"{url}/a.jpg", f"{url}/b.jpg"),
        attributes={"seller_type": "private", "ownership": "פרטית", "on_road_since": "2024-01"},
    )
    return CarListing(**{**values, **changes})


def jpeg(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, "JPEG")
    return buffer.getvalue()


HISTORY = [
    {"mispar_rechev": 13107604, "baalut_dt": 202401, "baalut": "פרטי"},
    {"mispar_rechev": 13107604, "baalut_dt": 202509, "baalut": "סוחר"},
]


def registry_transport(
    records: dict[int, dict], calls: list | None = None, histories: dict[int, list] | None = None,
    changes: dict[int, dict] | None = None, recalls: dict[int, list] | None = None,
    prices: list[dict] | None = None,
) -> httpx.MockTransport:
    histories = {13107604: HISTORY} if histories is None else histories
    changes = changes or {}
    recalls = recalls or {}

    def handle(request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.url.params["filters"])
        plate = filters.get("mispar_rechev") or filters.get("MISPAR_RECHEV")
        resource = request.url.params["resource_id"]
        if resource == plates.PRICE_RESOURCE:
            found = [row for row in prices or [] if all(row.get(key) == value for key, value in filters.items())]
            return httpx.Response(200, json={"success": True, "result": {"records": found}})
        if resource == plates.OWNERSHIP_RESOURCE:
            found = histories.get(plate, [])
        elif resource == plates.HISTORY_RESOURCE:
            found = [changes[plate]] if plate in changes else []
        elif resource == plates.RECALL_RESOURCE:
            found = recalls.get(plate, [])
        else:
            if calls is not None:
                calls.append(plate)
            found = [records[plate]] if plate in records else []
        return httpx.Response(200, json={"success": True, "result": {"records": found}})

    return httpx.MockTransport(handle)


class FakePlateReader(Processor):
    """Stands in for the ONNX plate models: each photo 'shows' a fixed reading."""

    name, version, target = "plate_reader", "1", "photo"

    def __init__(self, readings: dict[str, list[dict]]) -> None:
        self.readings = readings

    async def run(self, target: PhotoTarget) -> dict:
        return {"plates": self.readings.get(target.sha256, [])}


def reading(digits: str, confidence: float = 1.0) -> dict:
    return {"digits": digits, "ocr_conf": confidence, "valid": True, "box": [0, 0, 10, 5]}


class PlateLogicTests(unittest.TestCase):
    def test_format_and_vote(self):
        self.assertEqual(plates.format_plate("13107604"), "131-07-604")
        self.assertEqual(plates.format_plate("1234567"), "12-345-67")
        votes = plates.vote(
            [reading("51111503"), reading("51111503", 0.98), reading("51111603", 0.6),
             {"digits": "3332", "ocr_conf": 1.0, "valid": False}]
        )
        self.assertEqual(votes, [("51111503", 1.98), ("51111603", 0.6)])

    def test_jac_is_the_same_make_under_its_registry_name(self):
        # Yad2 spells JAC letter by letter; the registry calls the E-JS4 "JAC 40".
        ad = CarListing(
            "https://www.yad2.co.il/vehicles/item/x9i3q75i", "ג׳יי.איי.סי E-JS4", 128000, 2500, year=2025,
            manufacturer="ג׳יי.איי.סי", model="E-JS4", sub_model="Flagship חשמלי אוט׳ (190 כ״ס)",
            attributes={"on_road_since": "2025-10", "engine_type": "חשמלי", "color": "לבן", "ownership": "פרטית"},
        )
        record = {"tozeret_nm": "ג'אק סין", "kinuy_mishari": "JAC 40", "ramat_gimur": "FLAGSHIP",
                  "shnat_yitzur": 2025, "moed_aliya_lakvish": "2025-10", "baalut": "פרטי",
                  "tzeva_rechev": "שנהב לבן", "sug_delek_nm": "חשמל"}
        comparison = plates.compare(ad, record)
        self.assertTrue(comparison["same_car"])
        differs = {row["field"]: row["differs"] for row in comparison["fields"]}
        self.assertEqual(differs, {
            "Make": False, "Model": True, "Trim": False, "Year": False, "On the road since": False,
            "Last test": None, "Ownership": False, "Fuel": False, "Colour": False,
        })
        self.assertFalse(plates.compare(ad, {**record, "kinuy_mishari": "JUNEYAO JY AIR", "tozeret_nm": "ג'וניאו"})["same_car"])

    def test_ex_lease_car_sold_as_first_private_hand(self):
        # x9i3q75i: advertised as hand 1, private from new; the registry has a lease first.
        ad = CarListing(
            "https://www.yad2.co.il/vehicles/item/x9i3q75i", "ג׳יי.איי.סי E-JS4", 128000, 2500, year=2025,
            hand=1, manufacturer="ג׳יי.איי.סי", model="E-JS4",
            attributes={"ownership": "פרטית", "original_owner": "פרטית", "on_road_since": "2025-10"},
        )
        record = {"tozeret_nm": "ג'אק סין", "kinuy_mishari": "JAC 40", "shnat_yitzur": 2025,
                  "moed_aliya_lakvish": "2025-10", "baalut": "פרטי"}
        history = [{"month": "2025-10", "owner": "lease"}, {"month": "2026-01", "owner": "dealer"},
                   {"month": "2026-01", "owner": "dealer"}, {"month": "2026-01", "owner": "private"}]
        comparison = plates.compare(ad, record, history)
        self.assertEqual(comparison["discrepancies"], [
            "Ad says hand 1, registry shows 2 owners (not counting dealers): lease from 2025-10, private from 2026-01",
            "Ad says the original owner was private, registry's first owner was lease (from 2025-10)",
        ])
        rows = {row["field"]: row for row in comparison["fields"]}
        self.assertTrue(rows["Hand"]["differs"])
        self.assertEqual(rows["Hand"]["registry"], "2 (lease from 2025-10, private from 2026-01)")
        self.assertTrue(rows["Original owner"]["differs"])
        self.assertEqual(rows["Original owner"]["registry"], "Lease")
        self.assertFalse(rows["Ownership"]["differs"])  # the current owner is private, as the ad says

    def test_compare_flags_differences_from_the_ad(self):
        ad = limo(FIRST, year=2025, attributes={
            "seller_type": "private", "on_road_since": "2024-03", "last_test_date": "2026-01-01",
        })
        comparison = plates.compare(ad, RECORD)
        self.assertTrue(comparison["same_car"])
        self.assertEqual(
            comparison["discrepancies"],
            [
                "Ad says 2025, registry says 2024",
                "Ad says on the road since 2024-03, registry says 2024-01",
                "Advertised by a private seller, but a dealer owns the car now",
                "Ad says last test 01/2026, registry says 04/01/2024",
            ],
        )
        # Dealers are not hands or owners: compare with the last owner before them.
        leased = limo(FIRST, attributes={"seller_type": "dealer", "ownership": "החכר (ליסינג)", "hand": 1})
        history = [{"month": "2024-01", "owner": "lease"}, {"month": "2025-06", "owner": "dealer"},
                   {"month": "2025-08", "owner": "dealer"}]
        self.assertEqual(plates.compare(leased, RECORD, history)["discrepancies"], [])
        private = limo(FIRST, attributes={"seller_type": "dealer", "ownership": "פרטית", "hand": 1})
        self.assertIn(
            "Ad says private ownership, registry says lease (last owner before dealers)",
            plates.compare(private, RECORD, history)["discrepancies"],
        )
        nissan = {**RECORD, "tozeret_nm": "ניסאן הודו", "kinuy_mishari": "MICRA", "shnat_yitzur": 2016}
        self.assertFalse(plates.compare(ad, nissan)["same_car"])

    def test_lookup_returns_known_fields(self):
        async def run():
            async with httpx.AsyncClient(transport=registry_transport({13107604: RECORD})) as client:
                return await plates.lookup(client, "13107604"), await plates.lookup(client, "1234567")

        found, missing = asyncio.run(run())
        self.assertEqual((found["kinuy_mishari"], found["misgeret"]), ("LIMO", "LJ1234567890"))
        self.assertNotIn("mispar_rechev", found)
        self.assertIsNone(missing)

    def test_ownership_summary_skips_dealers(self):
        history = [
            {"month": "2019-03", "owner": "lease"},
            {"month": "2022-05", "owner": "dealer"},
            {"month": "2022-07", "owner": "private"},
            {"month": "2025-09", "owner": "dealer"},
            {"month": "2026-03", "owner": "dealer"},
        ]
        summary = plates.ownership_summary(history, datetime.date(2026, 11, 12))
        self.assertEqual(summary["hands"], 2)
        self.assertEqual(summary["last_owner"], "private")
        self.assertTrue(summary["with_dealer"])
        self.assertEqual(summary["dealer_since"], "2025-09")
        self.assertEqual(summary["dealer_months"], 14)
        self.assertEqual(summary["dealers_in_a_row"], 2)
        self.assertEqual(summary["dealer_stints"], 2)
        self.assertFalse(plates.ownership_summary(history[:3])["with_dealer"])
        # A month registered to a company before the first owner is the importer, not a hand.
        agency = [{"month": "2024-10", "owner": "company"}, {"month": "2024-11", "owner": "private"}]
        self.assertEqual(plates.ownership_summary(agency)["hands"], 1)
        self.assertTrue(plates.ownership_summary(agency)["agency_first"])
        company = [{"month": "2024-01", "owner": "company"}, {"month": "2025-09", "owner": "private"}]
        self.assertEqual(plates.ownership_summary(company)["hands"], 2)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        directory = Path(self._directory.name)
        self.state = BotState(str(directory / "bot.sqlite3"))
        self.store = LocalPhotoStore(directory / "photos")
        self.addCleanup(self._directory.cleanup)
        patcher = patch.dict("os.environ", {"DATA_DIR": str(directory)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def store_photos(self, colors: list[str]) -> list[str]:
        hashes = []
        with self.state.engine.connect() as db:
            from storage.schema import photos

            ids = db.execute(select(photos.c.id).order_by(photos.c.id)).scalars().all()
        for photo_id, color in zip(ids, colors, strict=True):
            sha, key = asyncio.run(self.store.save(jpeg(color), ".jpg"))
            self.state.ads.photo_stored(photo_id, sha, key, 10)
            hashes.append(sha)
        return hashes


class PlatePipelineTests(StoreTestCase):
    def test_plate_is_confirmed_saved_and_links_ads_of_the_same_car(self):
        self.state.ads.record([limo(FIRST), limo(SECOND, price=93000)])
        red, blue, green, white = self.store_photos(["red", "blue", "green", "white"])
        reader = FakePlateReader({
            red: [reading("13107604")], blue: [reading("13107604", 0.97), reading("4417444", 0.5)],
            green: [reading("13107604")], white: [],
        })
        calls = []
        client = httpx.AsyncClient(transport=registry_transport({13107604: RECORD}, calls))
        runner = ProcessingRunner(self.state.engine, self.store, [reader, PlateRegistryProcessor(client)])
        asyncio.run(runner.run_pending())

        with self.state.engine.connect() as db:
            rows = db.execute(select(ads.c.token, ads.c.plate, ads.c.plate_status).order_by(ads.c.token)).all()
        self.assertEqual([tuple(row) for row in rows], [("limo0001", "13107604", "confirmed"), ("limo0002", "13107604", "confirmed")])
        self.assertEqual(calls, [13107604, 13107604])

        web = TestClient(create_app(self.state.engine, self.store, auth.NoAuth()))
        page = web.get("/ads/limo0001").text
        for expected in ("131-07-604", "Confirmed in the vehicle registry", "a dealer owns the car now",
                         "Same car, other ads", "Same licence plate 131-07-604", "With a dealer since 2025-09",
                         "1 hand not counting dealers", "/ads/limo0002", "PREMIUM TECH", "LJ1234567890"):
            self.assertIn(expected, page)
        listing = web.get("/ads", params={"q": "131-07-604", "status": "all"}).text
        # One row per car: the newest ad, marked as a repost.
        self.assertNotIn("/ads/limo0001", listing)
        self.assertIn("/ads/limo0002", listing)
        self.assertIn("Reposted ×1", listing)

    def test_registry_change_history_and_open_recalls_become_warnings(self):
        self.state.ads.record([limo(FIRST, images=(f"{FIRST}/a.jpg",), km=30000)])
        (red,) = self.store_photos(["red"])
        changes = {
            "mispar_rechev": 13107604, "kilometer_test_aharon": 41250, "shinui_mivne_ind": 1,
            "gapam_ind": 0, "shnui_zeva_ind": 1, "shinui_zmig_ind": 0,
            "rishum_rishon_dt": "2024-03-01", "mkoriut_nm": "החכר",
        }
        recall = {
            "MISPAR_RECHEV": 13107604, "RECALL_ID": 15781, "SUG_RECALL": "תקלה סידרתית בטיחותית",
            "SUG_TAKALA": "בלמים", "TEUR_TAKALA": "נוזל  בלמים עלול   לדלוף", "TAARICH_PTICHA": "2025-02-03",
        }
        prices = [
            {"tozeret_cd": 1321, "degem_cd": 16, "sug_degem": "P", "shnat_yitzur": year, "mehir": price,
             "shem_yevuan": "יבואן"}
            for year, price in ((2023, 157990), (2024, 159990), (2025, 169500))
        ]
        record = {**RECORD, "tozeret_cd": 1321, "degem_cd": 16, "sug_degem": "P"}
        client = httpx.AsyncClient(transport=registry_transport(
            {13107604: record}, changes={13107604: changes}, recalls={13107604: [recall]}, prices=prices))
        runner = ProcessingRunner(
            self.state.engine, self.store,
            [FakePlateReader({red: [reading("13107604")]}), PlateRegistryProcessor(client)],
        )
        asyncio.run(runner.run_pending())
        with self.state.engine.connect() as db:
            result = db.execute(
                select(analysis_results.c.result).where(analysis_results.c.processor == "plate_registry")
            ).scalar_one()
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["vehicle_history"]["structural_change"])
        self.assertEqual(result["recalls"][0]["fault"], "נוזל בלמים עלול לדלוף")
        warnings = result["warnings"]
        self.assertEqual(len(warnings), 5)
        self.assertIn("structural change", warnings[0])
        self.assertIn("repainted", warnings[1])
        self.assertEqual("Registry origin: came from a leasing company (החכר)", warnings[2])
        self.assertIn("Ad says 30,000 km, but the registry recorded 41,250 km", warnings[3])
        self.assertIn("Recall not done yet (opened 2025-02-03)", warnings[4])
        # Rounded down to the thousand is not a warning.
        rounded = plates.registry_warnings(limo(FIRST, km=41000), result["vehicle_history"], [])
        self.assertFalse(any("km" in warning for warning in rounded))

        page = TestClient(create_app(self.state.engine, self.store, auth.NoAuth())).get("/ads/limo0001").text
        self.assertIn("Registry warnings", page)
        self.assertIn("41,250 km", page)
        # The list price for the registry's model year (2024), not another year's.
        self.assertEqual((result["new_price"]["price"], result["new_price"]["exact_year"]), (159990, True))
        self.assertIn("Price when new: <strong>₪159,990</strong>", page)
        self.assertIn("₪159,990 new", page)

    def test_a_clean_registry_history_says_so(self):
        self.state.ads.record([limo(FIRST, images=(f"{FIRST}/a.jpg",))])
        (red,) = self.store_photos(["red"])
        clean = {"mispar_rechev": 13107604, "kilometer_test_aharon": 0, "shinui_mivne_ind": 0,
                 "gapam_ind": 0, "shnui_zeva_ind": 0, "shinui_zmig_ind": 0, "mkoriut_nm": ""}
        client = httpx.AsyncClient(transport=registry_transport({13107604: RECORD}, changes={13107604: clean}))
        asyncio.run(ProcessingRunner(
            self.state.engine, self.store,
            [FakePlateReader({red: [reading("13107604")]}), PlateRegistryProcessor(client)],
        ).run_pending())
        page = TestClient(create_app(self.state.engine, self.store, auth.NoAuth())).get("/ads/limo0001").text
        self.assertNotIn("Registry warnings", page)
        self.assertIn("no structural change, gas conversion, colour or tyre change, and no open recall", page)

    def test_ad_is_checked_again_when_the_seller_adds_photos(self):
        from storage.db import utcnow
        from storage.schema import photos

        self.state.ads.record([limo(FIRST, images=(f"{FIRST}/stock.jpg",))])
        (stock,) = self.store_photos(["red"])
        client = httpx.AsyncClient(transport=registry_transport({13107604: RECORD}))
        reader = FakePlateReader({stock: []})
        registry = PlateRegistryProcessor(client)
        asyncio.run(ProcessingRunner(self.state.engine, self.store, [reader, registry]).run_pending())
        self.assertEqual(asyncio.run(ProcessingRunner(self.state.engine, self.store, [reader, registry]).run_pending()), 0)

        # The seller swaps the stock picture for real photos showing the plate.
        self.state.ads.record([limo(FIRST, images=(f"{FIRST}/real.jpg",))], utcnow())
        with self.state.engine.connect() as db:
            rows = db.execute(select(photos.c.id, photos.c.removed_from_ad_at).order_by(photos.c.id)).all()
        self.assertIsNotNone(rows[0].removed_from_ad_at)
        sha, key = asyncio.run(self.store.save(jpeg("blue"), ".jpg"))
        self.state.ads.photo_stored(rows[1].id, sha, key, 10)
        reader.readings[sha] = [reading("13107604")]

        asyncio.run(ProcessingRunner(self.state.engine, self.store, [reader, registry]).run_pending())
        with self.state.engine.connect() as db:
            row = db.execute(select(ads.c.plate, ads.c.plate_status)).one()
            result = db.execute(
                select(analysis_results.c.result).where(analysis_results.c.processor == "plate_registry")
            ).scalar_one()
        self.assertEqual(tuple(row), ("13107604", "confirmed"))
        self.assertEqual(result["photos_with_plate"], 1)  # the replaced stock picture is left out
        self.assertEqual(asyncio.run(ProcessingRunner(self.state.engine, self.store, [reader, registry]).run_pending()), 0)

        # The swap shows in the overview's recent changes and the ad's history.
        web = TestClient(create_app(self.state.engine, self.store, auth.NoAuth()))
        self.assertIn("Photos changed: 1 new, 1 removed", web.get("/").text)
        self.assertIn("Photos changed: 1 new, 1 removed", web.get("/ads/limo0001").text)
        self.state.ads.record([limo(FIRST, images=(f"{FIRST}/real.jpg",))], utcnow())  # unchanged
        self.assertEqual(web.get("/").text.count("Photos changed"), 1)

    def test_ad_waits_for_photo_results_and_rejects_a_different_car(self):
        self.state.ads.record([limo(FIRST, images=(f"{FIRST}/a.jpg",))])
        (red,) = self.store_photos(["red"])
        client = httpx.AsyncClient(transport=registry_transport({3332233: {**RECORD, "tozeret_nm": "ניסאן", "kinuy_mishari": "MICRA", "shnat_yitzur": 2016}}))
        registry = PlateRegistryProcessor(client)

        # Without plate_reader results the ad is not ready.
        self.assertEqual(asyncio.run(ProcessingRunner(self.state.engine, self.store, [registry]).run_pending()), 0)

        reader = FakePlateReader({red: [reading("3332233")]})
        asyncio.run(ProcessingRunner(self.state.engine, self.store, [reader, registry]).run_pending())
        with self.state.engine.connect() as db:
            row = db.execute(select(ads.c.plate, ads.c.plate_status)).one()
            result = db.execute(
                select(analysis_results.c.result).where(analysis_results.c.processor == "plate_registry")
            ).scalar_one()
        self.assertEqual(tuple(row), (None, "mismatch"))
        self.assertEqual(result["rejected"][0]["registry_model"], "MICRA")
        # The rejected car is kept whole and shown next to the ad, the differences highlighted.
        rejected = result["rejected"][0]
        self.assertEqual(rejected["registry"]["kinuy_mishari"], "MICRA")
        differs = {row["field"]: row["differs"] for row in rejected["fields"]}
        self.assertEqual((differs["Make"], differs["Model"], differs["Year"]), (True, True, True))
        self.assertFalse(differs["On the road since"])
        page = TestClient(create_app(self.state.engine, self.store, auth.NoAuth())).get("/ads/limo0001").text
        self.assertIn("33-322-33", page)
        self.assertIn("Registry: <bdi>MICRA</bdi> 2016", page)
        self.assertEqual(page.count('<tr class="differs">'), 3)
        self.assertIn("ניסאן", page)

    def test_registry_needs_the_reader(self):
        with patch.dict("os.environ", {"PROCESSORS": "plate_registry,photo_info", "ANTHROPIC_API_KEY": ""}):
            self.assertEqual([processor.name for processor in enabled_processors()], ["photo_info"])
        with patch.dict("os.environ", {"PROCESSORS": "claude_condition", "ANTHROPIC_API_KEY": ""}):
            self.assertEqual(enabled_processors(), [])


class FakeMessages:
    def __init__(self, answer: dict, model: str = "claude-sonnet-5-5") -> None:
        self.answer, self.model, self.requests = answer, model, []

    async def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            model=self.model,
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(self.answer))],
            usage=SimpleNamespace(input_tokens=9000, output_tokens=1500, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )


ANSWER = {
    "overall_condition": "fair",
    "summary": "Clean interior, but the front bumper was repaired.",
    "damage": [{"photo": 2, "location": "front bumper", "kind": "scratch", "severity": "moderate", "description": "Deep scratches on the left corner"}],
    "price_factors": [
        {"issue": "Low tyre tread", "evidence": "photo 1", "impact": "low"},
        {"issue": "Repainted front bumper", "evidence": "photos 1-2", "impact": "high"},
    ],
    "positives": ["Clean seats"],
    "odometer_km": 61200,
    "odometer_photo": 1,
    "photos_match_ad": "yes",
    "match_notes": "",
}


class ClaudeConditionTests(StoreTestCase):
    def test_claude_condition_is_saved_shown_and_budgeted(self):
        self.state.ads.record([limo(FIRST), limo(SECOND)])
        self.state.ads.mark_removed([SECOND])
        self.store_photos(["red", "blue", "green", "white"])
        messages = FakeMessages(ANSWER)
        processor = ClaudeConditionProcessor(SimpleNamespace(beta=SimpleNamespace(messages=messages)))
        processor.budget = 1.0
        runner = ProcessingRunner(self.state.engine, self.store, [processor])
        self.assertEqual(asyncio.run(runner.run_pending()), 1)  # the removed ad is skipped

        request = messages.requests[0]
        self.assertEqual(request["model"], "claude-sonnet-5-5")
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(request["system"][0]["cache_control"], {"type": "ephemeral"})
        images = [block for block in request["messages"][0]["content"] if block["type"] == "image"]
        self.assertEqual(len(images), 2)
        self.assertIn("Advertised car: אי.וי איזי Limo", request["messages"][0]["content"][-1]["text"])
        self.assertAlmostEqual(processor.spent_this_month(), 0.033, places=3)

        web = TestClient(create_app(self.state.engine, self.store, auth.NoAuth()))
        page = web.get("/ads/limo0001").text
        for expected in ("Fair condition", "Repainted front bumper", "front bumper", "61,200 km", "Clean seats"):
            self.assertIn(expected, page)
        self.assertLess(page.index("Repainted front bumper"), page.index("Low tyre tread"))
        self.assertIn("Fair · 2 issues", web.get("/ads").text)

        # Once the month's budget is spent, ads wait instead of failing.
        processor.budget = 0.01
        self.state.ads.record([limo(SECOND)])  # relisted, so active again
        self.assertEqual(asyncio.run(runner.run_pending()), 0)
        self.assertEqual(len(messages.requests), 1)
