import asyncio
import datetime
import json
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from sqlalchemy import func, select

from bot.scanner import format_listing
from bot.state import BotState
from car import yad2_json
from car.car_finder import Yad2CarFinder
from car.listing import CarListing, KnownAd, ScanOutput, ScanPlan
from processing.base import PhotoTarget, Processor
from processing.processors.photo_info import PhotoInfoProcessor
from processing.runner import ProcessingRunner
from storage.photos import LocalPhotoStore
from storage.schema import ad_snapshots, ads, analysis_results, photos
from tracking.photo_archiver import PhotoArchiver
from tracking.tracker import AdTracker, detect_changes, price_floor, search_max_price

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.yad2.co.il/vehicles/item/6rg1e7dp"
SEARCH = "https://www.yad2.co.il/vehicles/cars?manufacturer=19"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def jpeg(color: str = "red") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, "JPEG")
    return buffer.getvalue()


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.state = BotState(str(self.directory / "bot.sqlite3"))

    def tearDown(self):
        self._directory.cleanup()

    def count(self, table, *where) -> int:
        with self.state.engine.connect() as db:
            return db.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


class Yad2JsonTests(unittest.TestCase):
    def test_feed_json_gives_real_price_not_drop_amount(self):
        next_data = fixture("feed_next_data.json")
        listings = [yad2_json.listing_from_json(ad) for ad in yad2_json.feed_ads(next_data)]
        self.assertEqual([item.token for item in listings], ["6rg1e7dp", "p1atinum"])
        car = listings[0]
        self.assertEqual((car.price, car.hand, car.year, car.km), (86000, 2, 2019, None))
        self.assertEqual(car.attributes["price_before"], 89000)
        self.assertEqual(car.title, "טויוטה קורולה")
        self.assertEqual(len(car.images), 2)
        # 15:48 in Israel (UTC+2 in November) is 13:48 UTC.
        self.assertEqual(car.listed_at, datetime.datetime(2026, 11, 9, 13, 48, 38))
        self.assertEqual(yad2_json.feed_pages(next_data), 3)

    def test_feed_total_and_unknown_groups(self):
        data = fixture("feed_next_data.json")
        self.assertEqual(yad2_json.feed_total(data), 81)
        self.assertEqual(yad2_json.unknown_feed_groups(data), [])
        feed = yad2_json.feed_data(data)
        feed["sponsored"] = [{"token": "new00001"}]
        self.assertEqual(yad2_json.unknown_feed_groups(data), ["sponsored"])

    def test_older_feed_layout_is_read_too(self):
        next_data = fixture("feed_next_data.json")
        query = next_data["props"]["pageProps"]["dehydratedState"]["queries"][0]
        data = query["state"]["data"]
        query["queryKey"][0] = "feed"
        data["private"], data["commercial"] = data.pop("ads"), [{"token": "dealer01", "price": 1}]
        tokens = [ad["token"] for ad in yad2_json.feed_ads(next_data)]
        self.assertEqual(tokens, ["6rg1e7dp", "dealer01", "p1atinum"])

    def test_item_json_adds_mileage_and_parameters(self):
        car = yad2_json.listing_from_json(
            yad2_json.item_data(fixture("item_next_data.json")), detailed=True
        )
        self.assertEqual(car.km, 145000)
        self.assertTrue(car.detailed)
        self.assertEqual(car.attributes["color"], "לבן שנהב")
        self.assertEqual(car.attributes["city"], "בני ברק")
        self.assertEqual(car.attributes["gearbox"], "אוטומטי")
        attributes = car.attributes
        self.assertEqual(attributes["on_road_since"], "2019-10")
        self.assertEqual(attributes["last_test_date"], "2026-09-01")
        self.assertEqual(attributes["km_at_last_test"], 143094)
        self.assertEqual(attributes["km_since_last_test"], 145000 - 143094)
        self.assertEqual(attributes["test_valid_until"], "2027-10-01T00:00:00")
        self.assertEqual(attributes["powertrain"], "היברידי מלא")
        self.assertEqual(attributes["drive"], "4X2")
        self.assertEqual((attributes["safety_points"], attributes["pollution_level"]), (2, 3))
        self.assertEqual(attributes["airbags"], 7)
        self.assertEqual(attributes["equipment"], ["abs", "airConditioner", "reverseCamera"])
        self.assertNotIn("originalOwner", attributes["verified_by_ministry"])
        self.assertEqual(attributes["car_family"], ["משפחתיים"])
        # Unknown fields are kept; personal ones are not.
        self.assertEqual(attributes["warranty_months"], 12)
        self.assertEqual(attributes["fuel_type"], "בנזין")
        self.assertNotIn("customer", attributes)
        self.assertIsNone(yad2_json.item_data({"props": {"pageProps": {}}}))

    def test_card_price_skips_drop_badge(self):
        self.assertEqual(Yad2CarFinder._parse_card_price(["ירד ב 3,000 ₪", "86,000 ₪"]), 86000)
        self.assertEqual(Yad2CarFinder._parse_card_price(["86,000 ₪"]), 86000)
        self.assertIsNone(Yad2CarFinder._parse_card_price(["ירד ב 3,000 ₪"]))


class PriceDropTests(unittest.TestCase):
    def test_drop_alert_shows_new_price_and_difference(self):
        old = {URL: CarListing(URL, "Car", 89000, 50000)}
        new, drops = detect_changes(old, [CarListing(URL, "Car", 86000)])
        self.assertEqual(new, [])
        self.assertIn("₪89,000 → ₪86,000 (down ₪3,000)", format_listing(*drops[0]))
        self.assertIn("50,000 km", format_listing(*drops[0]))

    def test_implausible_drop_is_not_alerted(self):
        old = {URL: CarListing(URL, "Car", 89000)}
        self.assertEqual(detect_changes(old, [CarListing(URL, "Car", 3000)]), ([], []))

    def test_alert_shows_listing_age(self):
        listed = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(
            days=3, hours=1
        )
        text = format_listing(CarListing(URL, "Car", 1, listed_at=listed))
        self.assertIn("Listed: 3 days ago", text)


class NeedsDetailsTests(unittest.TestCase):
    def test_new_changed_and_capped_stale_ads_are_fetched(self):
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        fresh, stale = now - datetime.timedelta(hours=1), now - datetime.timedelta(days=2)
        listings = [CarListing(f"{URL[:-8]}ad{index}", "Car", 100) for index in range(5)]
        plan = ScanPlan(
            known={
                listings[1].url: KnownAd(100, fresh),  # unchanged, fresh: skip
                listings[2].url: KnownAd(90, fresh),  # price changed
                listings[3].url: KnownAd(100, stale),
                listings[4].url: KnownAd(100, stale - datetime.timedelta(days=1)),
            },
            max_refresh=1,
        )
        chosen = [item.url for item in Yad2CarFinder._needs_details(listings, plan)]
        self.assertEqual(chosen, [listings[0].url, listings[2].url, listings[4].url])


class TrackerTests(TempStateTestCase):
    def test_lifecycle_new_drop_removed_relisted(self):
        tracker = AdTracker(self.state.ads)
        self.search, _ = self.state.add_search(1, SEARCH, 10)
        detailed = yad2_json.listing_from_json(
            yad2_json.item_data(fixture("item_next_data.json")), detailed=True
        )
        t0 = datetime.datetime(2026, 11, 10, 12)

        result = tracker.apply(1, self.search.id, ScanOutput([detailed]), t0)
        self.assertEqual([item.token for item in result.new], ["6rg1e7dp"])
        self.assertEqual(self.state.ads.search_urls(self.search.id), frozenset({URL}))
        self.assertEqual(self.count(photos), 2)

        # Feed-only listing (no km) with a lower price and one photo gone.
        cheaper = yad2_json.listing_from_json(yad2_json.feed_ads(fixture("feed_next_data.json"))[0])
        cheaper = CarListing(**{**cheaper.__dict__, "price": 83000, "images": cheaper.images[:1]})
        result = tracker.apply(1, self.search.id, ScanOutput([cheaper]), t0 + datetime.timedelta(hours=1))
        self.assertEqual(result.new, [])
        listing, old_price = result.drops[0]
        self.assertEqual((old_price, listing.price, listing.km), (86000, 83000, 145000))
        self.assertEqual(self.count(photos, photos.c.removed_from_ad_at.is_not(None)), 1)

        result = tracker.apply(1, self.search.id, ScanOutput([], removed=[URL]), t0 + datetime.timedelta(hours=2))
        self.assertEqual(result.removed, [URL])
        with self.state.engine.connect() as db:
            self.assertIsNotNone(db.execute(select(ads.c.removed_at)).scalar_one())
        self.assertEqual(self.state.ads.search_urls(self.search.id), frozenset())

        tracker.apply(1, self.search.id, ScanOutput([cheaper]), t0 + datetime.timedelta(hours=3))
        with self.state.engine.connect() as db:
            self.assertIsNone(db.execute(select(ads.c.removed_at)).scalar_one())
            history = db.execute(
                select(ad_snapshots.c.price, ad_snapshots.c.changes).order_by(ad_snapshots.c.id)
            ).all()
        self.assertEqual(history[0].price, 86000)
        self.assertEqual(history[1].changes["price"], [86000, 83000])
        self.assertIn("removed", history[2].changes)
        self.assertIn("relisted", history[3].changes)

    def test_badge_price_from_old_scraper_is_corrected_silently(self):
        # Smart EQ ForFour: Yad2 shows 150,000 -> 100,000; the old scraper saved the 50,000 badge.
        tracker = AdTracker(self.state.ads)
        self.search, _ = self.state.add_search(1, SEARCH, 10)
        tracker.apply(1, self.search.id, ScanOutput([CarListing(URL, "Car", 50000)]))
        real = CarListing(URL, "Car", 100000, attributes={"price_before": 150000})
        result = tracker.apply(1, self.search.id, ScanOutput([real]))
        self.assertEqual((result.new, result.drops), ([], []))
        self.assertEqual(self.state.ads.get("6rg1e7dp").price, 100000)
        self.assertEqual(self.state.previous_listings(1)[URL].price, 100000)
        with self.state.engine.connect() as db:
            history = db.execute(select(ad_snapshots.c.price, ad_snapshots.c.changes)).all()
        self.assertEqual([(row.price, row.changes) for row in history], [(100000, None)])

    def test_prices_under_ten_percent_of_search_max_are_ignored(self):
        url = "https://www.yad2.co.il/vehicles/cars?year=2023--1&price=1-100000&km=-1-40000"
        self.assertEqual(search_max_price(url), 100000)
        self.assertIsNone(search_max_price("https://www.yad2.co.il/vehicles/cars?price=50000--1"))
        self.assertEqual(price_floor(None, [90000, 1, None]), 9000)
        tracker = AdTracker(self.state.ads)
        self.search, _ = self.state.add_search(1, url, 10)
        lease = CarListing(URL.replace("6rg1e7dp", "lease001"), "Lease", 5550)
        placeholder = CarListing(URL.replace("6rg1e7dp", "placehld"), "Ask", 1)
        real = CarListing(URL, "Car", 90000)
        result = tracker.apply(1, self.search.id, ScanOutput([lease, placeholder, real]), search_url=url)
        self.assertEqual(result.new, [real])
        dropped = CarListing(lease.url, "Lease", 4850)
        result = tracker.apply(1, self.search.id, ScanOutput([dropped, placeholder, real]), search_url=url)
        self.assertEqual(result.drops, [])
        self.assertEqual(self.state.ads.get("lease001").price, 4850)  # still archived

    def test_title_spacing_is_not_a_change(self):
        tracker = AdTracker(self.state.ads)
        self.search, _ = self.state.add_search(1, SEARCH, 10)
        tracker.apply(1, self.search.id, ScanOutput([CarListing(URL, "אי.וי איזי Limo", 90000)]))
        tracker.apply(1, self.search.id, ScanOutput([CarListing(URL, "אי.וי איזי  Limo", 90000)]))
        with self.state.engine.connect() as db:
            history = db.execute(select(ad_snapshots.c.changes)).scalars().all()
        self.assertEqual(history, [None])

    def test_big_drop_badge_does_not_alert(self):
        # 200,000 -> 90,000: the badge (110,000) is above the new price and looked like a drop.
        tracker = AdTracker(self.state.ads)
        self.search, _ = self.state.add_search(1, SEARCH, 10)
        tracker.apply(1, self.search.id, ScanOutput([CarListing(URL, "Car", 110000)]))
        real = CarListing(URL, "Car", 90000, attributes={"price_before": 200000})
        self.assertEqual(tracker.apply(1, self.search.id, ScanOutput([real])).drops, [])

    def test_ads_left_out_of_results_are_not_rechecked(self):
        tracker = AdTracker(self.state.ads)
        self.search, _ = self.state.add_search(1, SEARCH, 10)
        tracker.apply(1, self.search.id, ScanOutput([CarListing(URL, "Car", 90000)]))
        tracker.apply(1, self.search.id, ScanOutput([], left_search=[CarListing(URL, "Car", 150000, detailed=True)]))
        self.assertEqual(self.state.ads.search_urls(self.search.id), frozenset())
        self.assertEqual(self.state.ads.get("6rg1e7dp").price, 150000)

    def test_stop_keeps_ad_history(self):
        self.search, _ = self.state.add_search(1, SEARCH, 10)
        self.state.save_listings(self.search, [CarListing(URL, "Car", 90000)])
        self.state.stop_search(self.search.id)
        self.assertEqual(self.state.seen_count(self.search.id), 0)
        self.assertEqual(self.count(ads), 1)

    def test_known_ads_are_shared_between_chats(self):
        tracker = AdTracker(self.state.ads)
        first, _ = self.state.add_search(1, SEARCH, 10)
        second, _ = self.state.add_search(2, SEARCH, 10)
        tracker.apply(1, first.id, ScanOutput([CarListing(URL, "Car", 90000, 40000, detailed=True)]))
        result = tracker.apply(2, second.id, ScanOutput([CarListing(URL, "Car", 90000)]))
        self.assertEqual(result.new[0].km, 40000)
        self.assertIn(URL, tracker.plan(second.id).known)


class MigrationTests(unittest.TestCase):
    def test_old_listings_move_into_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE searches (chat_id INTEGER PRIMARY KEY, url TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL);
                CREATE TABLE listings (chat_id INTEGER NOT NULL, url TEXT NOT NULL,
                    title TEXT NOT NULL, price INTEGER, km INTEGER, PRIMARY KEY (chat_id, url));
                CREATE TABLE access (user_id INTEGER PRIMARY KEY, allowed INTEGER NOT NULL);
                CREATE TABLE admins (user_id INTEGER PRIMARY KEY);
                INSERT INTO searches VALUES (1, 'https://www.yad2.co.il/vehicles/cars?a=1', 10);
                INSERT INTO listings VALUES (1, 'https://www.yad2.co.il/vehicles/item/abc', 'A', 90000, 1000);
                INSERT INTO listings VALUES (2, 'https://www.yad2.co.il/vehicles/item/abc', 'A', 95000, 1000);
                INSERT INTO admins VALUES (7);
                """
            )
            db.commit()
            db.close()

            state = BotState(str(path))
            [search] = state.active_searches()
            self.assertEqual((search.chat_id, search.url, search.interval_minutes), (1, "https://www.yad2.co.il/vehicles/cars?a=1", 10))
            self.assertTrue(state.is_admin(7))
            self.assertEqual(state.previous_listings(1)[URL[:-8] + "abc"].price, 90000)
            self.assertEqual(state.previous_listings(2)[URL[:-8] + "abc"].price, 95000)
            self.assertEqual(state.ads.get("abc").km, 1000)


class PhotoTests(TempStateTestCase):
    def test_archiver_stores_originals_once_and_retries_failures(self):
        self.state.ads.record(
            [
                CarListing(URL, "Car", 1, images=("https://img.yad2.co.il/a.jpeg", "https://img.yad2.co.il/missing.jpeg")),
                CarListing(URL[:-8] + "other", "Car", 1, images=("https://img.yad2.co.il/copy.jpeg",)),
            ]
        )
        image = jpeg()

        def respond(request: httpx.Request) -> httpx.Response:
            if "missing" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, content=image, headers={"content-type": "image/jpeg"})

        store = LocalPhotoStore(self.directory / "photos")
        archiver = PhotoArchiver(self.state.ads, store, delay=0)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await archiver.run_pending(client=client)

        self.assertEqual(asyncio.run(run()), 2)
        files = list((self.directory / "photos").rglob("*.jpg"))
        self.assertEqual(len(files), 1)  # same bytes, stored once
        self.assertEqual(files[0].read_bytes(), image)
        self.assertEqual(self.state.ads.pending_photos(), [(2, "https://img.yad2.co.il/missing.jpeg")])
        with self.assertRaises(ValueError):
            store.path("../../etc/passwd")


class ProcessingTests(TempStateTestCase):
    def test_runner_processes_each_photo_once_per_version(self):
        store = LocalPhotoStore(self.directory / "photos")
        sha, key = asyncio.run(store.save(jpeg(), ".jpg"))
        self.state.ads.record([CarListing(URL, "Car", 1, images=("https://img/a.jpg",))])
        self.state.ads.photo_stored(1, sha, key, 10)

        runner = ProcessingRunner(self.state.engine, store, [PhotoInfoProcessor()])
        self.assertEqual(asyncio.run(runner.run_pending()), 1)
        self.assertEqual(asyncio.run(runner.run_pending()), 0)
        with self.state.engine.connect() as db:
            result = db.execute(select(analysis_results.c.result)).scalar_one()
        self.assertEqual((result["width"], result["height"]), (64, 48))

        class PhotoInfoV2(PhotoInfoProcessor):
            version = "2"

        self.assertEqual(asyncio.run(ProcessingRunner(self.state.engine, store, [PhotoInfoV2()]).run_pending()), 1)

    def test_processing_pauses_while_a_scan_runs(self):
        store = LocalPhotoStore(self.directory / "photos")
        sha, key = asyncio.run(store.save(jpeg(), ".jpg"))
        self.state.ads.record([CarListing(URL, "Car", 1, images=("https://img/a.jpg",))])
        self.state.ads.photo_stored(1, sha, key, 10)
        runner = ProcessingRunner(self.state.engine, store, [PhotoInfoProcessor()])
        self.assertEqual(asyncio.run(runner.run_pending(paused=lambda: True)), 0)
        self.assertEqual(asyncio.run(runner.run_pending(paused=lambda: False)), 1)

    def test_failing_processor_gives_up_after_three_attempts(self):
        store = LocalPhotoStore(self.directory / "photos")
        sha, key = asyncio.run(store.save(jpeg(), ".jpg"))
        self.state.ads.record([CarListing(URL, "Car", 1, images=("https://img/a.jpg",))])
        self.state.ads.photo_stored(1, sha, key, 10)

        class Broken(Processor):
            name, version, target = "broken", "1", "photo"

            async def run(self, target: PhotoTarget) -> dict:
                raise RuntimeError("model crashed")

        runner = ProcessingRunner(self.state.engine, store, [Broken()])
        self.assertEqual(sum(asyncio.run(runner.run_pending()) for _ in range(5)), 3)
        with self.state.engine.connect() as db:
            row = db.execute(select(analysis_results)).one()
        self.assertEqual((row.status, row.attempts), ("error", 3))



class RestartTimingTests(unittest.TestCase):
    def test_restart_keeps_the_scan_rhythm(self):
        from bot.scanner import first_delay

        now = datetime.datetime(2026, 11, 11, 21, 29)
        just_scanned = now - datetime.timedelta(seconds=30)
        self.assertEqual(first_delay(just_scanned, 10, now), 570)
        self.assertEqual(first_delay(now - datetime.timedelta(minutes=25), 10, now), 5)
        self.assertEqual(first_delay(None, 10, now), 5)


class ScanFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_scan_reports_reason_to_chat(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from bot.scanner import Scanner
        from common.exceptions import CustomException, ErrorCode

        with tempfile.TemporaryDirectory() as directory:
            state = BotState(str(Path(directory) / "bot.sqlite3"))
            search, _ = state.add_search(1, SEARCH, 10)
            scanner = Scanner(state)
            context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
            error = CustomException("Yad2 requested browser validation", ErrorCode.CAPTCHA_TIMEOUT)
            with patch("bot.scanner._run_scraper_sync", side_effect=error):
                await scanner._run_scan(context, search)
            text = context.bot.send_message.await_args.args[1]
            self.assertIn("Yad2 requested browser validation", text)


if __name__ == "__main__":
    unittest.main()
