import asyncio
import datetime
from urllib.parse import unquote_plus
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from PIL import Image

from bot.state import BotState
from car import yad2_json
from car.listing import CarListing, ScanOutput
from dashboard import auth
from dashboard.app import create_app
from storage.photos import LocalPhotoStore
from tracking.tracker import AdTracker

FIXTURES = Path(__file__).parent / "fixtures"


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        directory = Path(self._directory.name)
        self.state = BotState(str(directory / "bot.sqlite3"))
        self.store = LocalPhotoStore(directory / "photos")
        item = yad2_json.item_data(json.loads((FIXTURES / "item_next_data.json").read_text()))
        listing = yad2_json.listing_from_json(item, detailed=True)
        tracker = AdTracker(self.state.ads)
        start = datetime.datetime(2026, 11, 1)
        search, _ = self.state.add_search(1, "https://www.yad2.co.il/vehicles/cars?manufacturer=19", 10)
        tracker.apply(1, search.id, ScanOutput([listing]), start)
        cheaper = CarListing(**{**listing.__dict__, "price": 82000})
        tracker.apply(1, search.id, ScanOutput([cheaper]), start + datetime.timedelta(days=2))
        tracker.apply(1, search.id, ScanOutput([], removed=[listing.url]), start + datetime.timedelta(days=5))

        buffer = BytesIO()
        Image.new("RGB", (640, 480), "blue").save(buffer, "JPEG")
        self.sha, key = asyncio.run(self.store.save(buffer.getvalue(), ".jpg"))
        self.state.ads.photo_stored(1, self.sha, key, len(buffer.getvalue()))

        # A developer's .env may set the key; these tests expect the Claude check to be off.
        patcher = patch.dict("os.environ", {"DATA_DIR": str(directory), "ANTHROPIC_API_KEY": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(create_app(self.state.engine, self.store, auth.NoAuth()))

    def tearDown(self):
        self._directory.cleanup()

    def test_pages_render(self):
        for path, expected in (
            ("/", "Price ▼ ₪86,000 → ₪82,000"),
            ("/ads?status=all", "טויוטה קורולה"),
            ("/ads?status=removed&sort=price", "Removed"),
            ("/ads/6rg1e7dp", "Removed from Yad2"),
            ("/market?x=year", "Median days until removed"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.text)
        self.assertEqual(self.client.get("/ads/missing").status_code, 404)

    def test_scans_page_shows_next_scan_progress_and_history(self):
        from storage.db import utcnow
        from storage.scans import ScanLog

        url = "https://www.yad2.co.il/vehicles/cars?carTag=6&price=1-100000"
        search, _ = self.state.add_search(1, url, 10)
        log = ScanLog(self.state.engine)
        done = log.queued(1, url, search.id)
        log.started(done)
        log.finished(done, results=84, new_ads=3, price_drops=1, removed=2, details_fetched=12)
        broken = log.queued(1, url, search.id)
        log.started(broken)
        log.failed(broken, "Yad2 asked for validation")
        running = log.queued(1, url, search.id)
        log.started(running)
        log.progress(running, "Ad pages 5/40 (84 results)")
        log.set_next(search.id, utcnow() + datetime.timedelta(minutes=7))

        page = self.client.get("/scans").text
        for expected in ("Ad pages 5/40", "Yad2 asked for validation", "in 7 min", ">84<", "Scanning",
                         "Electric · up to ₪100,000", "Stop this search", "Add a search"):
            self.assertIn(expected, page)
        self.assertIn("Ad pages 5/40", self.client.get("/").text)
        self.assertIn("in 7 min", self.client.get("/").text)

        self.assertEqual(log.interrupt_unfinished(), 1)
        page = self.client.get("/scans?chat=1").text
        self.assertIn("Bot restarted before the scan finished", page)
        self.assertNotIn("Scanning", page)

    def test_searches_are_added_and_stopped_on_the_scans_page(self):
        url = "https://www.yad2.co.il/vehicles/cars?manufacturer=19&price=-1-120000"
        response = self.client.post(
            "/searches", data={"url": url + "&page=2", "chat": "1", "interval": "15"}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("Added", unquote_plus(response.headers["location"]))
        added = self.state.chat_searches(1)[-1]
        self.assertEqual((added.url, added.interval_minutes), (url, 15))
        self.assertEqual(self.state.searches_added_on_dashboard(), [added])
        self.assertEqual(self.state.searches_added_on_dashboard(), [])  # announced once

        again = self.client.post("/searches", data={"url": url, "chat": "1", "interval": "15"})
        self.assertIn("already tracks this search", again.text)
        bad = self.client.post("/searches", data={"url": "https://example.com/x", "chat": "1", "interval": "10"})
        self.assertIn("HTTPS Yad2 car search URL", bad.text)
        foreign = self.client.post(
            "/searches", data={"url": url, "chat": "1", "interval": "10"},
            headers={"origin": "https://evil.example"},
        )
        self.assertEqual(foreign.status_code, 403)

        changed = self.client.post(f"/searches/{added.id}/interval", data={"interval": "30"})
        self.assertIn("now every 30 minutes", changed.text)
        self.assertEqual(self.state.get_search(added.id).interval_minutes, 30)
        self.assertIn("listed intervals", self.client.post(f"/searches/{added.id}/interval", data={"interval": "7"}).text)
        self.assertEqual(self.client.post("/searches/999/interval", data={"interval": "30"}).status_code, 404)

        stopped = self.client.post(f"/searches/{added.id}/stop")
        self.assertIn("Stopped", stopped.text)
        self.assertNotIn(added, self.state.chat_searches(1))

    def test_pages_can_be_limited_to_one_search(self):
        corolla = self.state.chat_searches(1)[0]
        electric, _ = self.state.add_search(1, "https://www.yad2.co.il/vehicles/cars?carTag=6&price=1-100000", 10)
        car = CarListing(
            "https://www.yad2.co.il/vehicles/item/tesla1", "טסלה מודל 3", 95000, 30000,
            year=2023, manufacturer="טסלה", model="מודל 3",
        )
        AdTracker(self.state.ads).apply(1, electric.id, ScanOutput([car]))

        everything = self.client.get("/ads?status=all").text
        self.assertIn("טסלה מודל 3", everything)
        self.assertIn("טויוטה קורולה", everything)
        only_electric = self.client.get(f"/ads?status=all&search={electric.id}").text
        self.assertIn("טסלה מודל 3", only_electric)
        self.assertNotIn("טויוטה קורולה", only_electric)
        self.assertIn(f'name="search" value="{electric.id}"', only_electric)  # kept when filtering
        self.assertIn(f'href="/market?search={electric.id}"', only_electric)  # and across pages
        self.assertIn("Electric · up to ₪100,000", only_electric)  # the picker

        market = self.client.get(f"/market?search={corolla.id}").text
        self.assertIn("טויוטה", market)
        self.assertNotIn("טסלה", market)
        self.assertIn(f"search={corolla.id}", market)

        overview = self.client.get(f"/?search={electric.id}").text
        self.assertIn("טסלה מודל 3", overview)
        self.assertNotIn("טויוטה קורולה", overview)
        self.assertIn(f"/scans?search={electric.id}", overview)

        # The choice is remembered on pages opened without `?search=`, until "All searches".
        self.assertNotIn("טויוטה קורולה", self.client.get("/ads?status=all").text)
        self.assertNotIn("טויוטה", self.client.get("/market").text)
        self.assertNotIn("טויוטה קורולה", self.client.get("/").text)
        self.assertIn("טויוטה קורולה", self.client.get("/ads?status=all&search=").text)
        self.assertIn("טויוטה קורולה", self.client.get("/ads?status=all").text)

        self.client.get(f"/?search={electric.id}")
        self.state.stop_search(electric.id)
        self.assertIn("טויוטה קורולה", self.client.get("/ads?status=all").text)  # stopped: back to all

    def test_background_page_shows_live_status_and_backlog(self):
        from storage.background_status import StatusBoard
        from tracking.background import BackgroundPipeline

        page = self.client.get("/background").text
        self.assertIn("No status yet", page)
        for name in ("photo_info", "plate_reader", "plate_registry", "claude_condition"):
            self.assertIn(name, page)
        self.assertIn("Off: ANTHROPIC_API_KEY is not set", page)

        StatusBoard(self.state.engine).set("processing", "plate_reader", 12, 40)
        page = self.client.get("/background").text
        self.assertIn("plate_reader</strong>", page)
        self.assertIn("12 of 40", page)
        self.assertIn("width: 30%", page)

        pipeline = BackgroundPipeline(self.state.ads, self.store)
        pipeline.busy = lambda: True
        asyncio.run(pipeline.run_once())
        self.assertIn("Waiting for the running scan to finish", self.client.get("/background").text)

    def test_ad_page_shows_test_and_registration_details(self):
        text = self.client.get("/ads/6rg1e7dp").text
        for expected in (
            "On the road since", "10/2019", "Mileage at last test", "143,094 km",
            "Last test", "09/2026", "Test valid until", "10/2027", "Driven since last test",
            "1,906 km", "2 of 8", "Reversing camera", "Not fitted", "Warranty months",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("6467260", text)  # seller's customer ID is not shown

    def test_models_and_subtypes(self):
        second = CarListing(
            "https://www.yad2.co.il/vehicles/item/second1", "טויוטה קורולה", 70000, 90000,
            year=2020, manufacturer="טויוטה", model="קורולה", sub_model="Sun היברידי",
        )
        search = self.state.chat_searches(1)[0]
        AdTracker(self.state.ads).apply(1, search.id, ScanOutput([second]))
        listing = self.client.get("/ads?status=all&sort=model&model=קורולה").text
        self.assertEqual(listing.count('class="group-row"'), 2)
        market = self.client.get("/market?manufacturer=טויוטה&model=קורולה").text
        self.assertIn("Subtypes by price", market)
        self.assertIn("Sun היברידי", market)
        self.assertIn("Space היברידי", market)
        self.assertEqual(market.count('class="strip-dot'), 2)
        only = self.client.get("/market?manufacturer=טויוטה&model=קורולה&sub_model=Sun היברידי").text
        self.assertEqual(only.count('"token": '), 1)  # scatter limited to the subtype
        self.assertIn("Models by price", self.client.get("/market").text)

    def test_photos_and_thumbnails(self):
        self.assertEqual(self.client.get(f"/photos/{self.sha}").status_code, 200)
        thumb = self.client.get(f"/thumbs/{self.sha}")
        self.assertEqual(thumb.headers["content-type"], "image/webp")
        self.assertEqual(Image.open(BytesIO(thumb.content)).width, 480)
        self.assertEqual(self.client.get("/photos/../../etc/passwd").status_code, 404)
        self.assertEqual(self.client.get("/photos/" + "0" * 64).status_code, 404)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.state = BotState(str(Path(self._directory.name) / "bot.sqlite3"))

    def tearDown(self):
        self._directory.cleanup()

    def test_unconfigured_auth_fails_closed(self):
        with patch.dict("os.environ", {"DASHBOARD_AUTH": "cloudflare"}, clear=False):
            authenticator = auth.from_env() if not _cf_env() else auth.Unconfigured()
        client = TestClient(create_app(self.state.engine, authenticator=authenticator))
        self.assertEqual(client.get("/").status_code, 403)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_cloudflare_token_is_verified(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        access = auth.CloudflareAccess("team.cloudflareaccess.com", "aud-tag")

        class Key:
            def __init__(self, public_key):
                self.key = public_key

        access._keys = type("Keys", (), {"get_signing_key_from_jwt": lambda _self, _t: Key(key.public_key())})()
        claims = {"aud": "aud-tag", "iss": "https://team.cloudflareaccess.com", "email": "a@example.com"}
        good = jwt.encode(claims, key, algorithm="RS256")
        wrong_audience = jwt.encode({**claims, "aud": "other"}, key, algorithm="RS256")

        client = TestClient(create_app(self.state.engine, authenticator=access))
        self.assertEqual(client.get("/").status_code, 403)
        self.assertEqual(client.get("/", headers={"Cf-Access-Jwt-Assertion": wrong_audience}).status_code, 403)
        self.assertEqual(client.get("/", headers={"Cf-Access-Jwt-Assertion": good}).status_code, 200)


def _cf_env() -> bool:
    import os

    return bool(os.getenv("CF_ACCESS_TEAM_DOMAIN") and os.getenv("CF_ACCESS_AUD"))


if __name__ == "__main__":
    unittest.main()
