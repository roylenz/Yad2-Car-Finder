import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import unittest.mock
from unittest.mock import AsyncMock

from bot.scanner import detect_changes, format_listing
from bot.state import BotState, Search
from tracking.tracker import AdTracker
from bot.access import AccessControl
from bot.scanner import Scanner
from bot.menu import ADMIN_COMMANDS, USER_COMMANDS
from telegram.ext import ApplicationHandlerStop
from car.car_finder import PageLoadError, Yad2CarFinder
from car.listing import CarListing, KnownAd, ScanOutput, ScanPlan
from car.search_url import normalize_search_url, price_range, split_price, with_price

EXAMPLE_URL = (
    "https://www.yad2.co.il/vehicles/cars?manufacturer=334&model=13190"
    "&year=2023--1&price=-1-90000&km=-1-50000"
)


class FakeFinder(Yad2CarFinder):
    """Serves a fixed set of ads by price, `per_page` at a time, without a browser."""

    def __init__(self, prices: dict[str, int], per_page: int = 40) -> None:
        self.prices, self.per_page, self.loaded = prices, per_page, []

    async def _load_results_page(self, tab, url):
        self.loaded.append(url)
        tab.url = url
        return True

    async def _collect_pages(self, tab, search_url, max_pages, listings, report):
        raise AssertionError("price slices should fit on one page")

    async def fetch(self, tab, listings, feed=None):
        bounds = price_range(tab.url)
        matching = sorted(token for token, price in self.prices.items() if bounds[0] <= price <= bounds[1])
        for token in matching[: self.per_page]:
            url = f"https://www.yad2.co.il/vehicles/item/{token}"
            listings[url] = CarListing(url, token, self.prices[token])
        if feed is not None:
            feed["total"] = len(matching)
        return max(-(-len(matching) // self.per_page), 1)


class FakeTab:
    """A browser tab that goes through scripted page states, one per navigation."""

    def __init__(self, navigations: list[list[str]]) -> None:
        self.navigations, self.current, self.visits = navigations, [], []
        self.url = "about:blank"

    async def get(self, url):
        self.visits.append(url)
        self.current = list(self.navigations.pop(0))

    def _href(self):
        return self.current[0] if self.current else "about:blank"

    async def evaluate(self, expression):
        href = self._href()
        if len(self.current) > 1:
            self.current.pop(0)  # the page moves on (e.g. a bot check redirecting back)
        if "location.href" in expression:
            return json.dumps({"href": href, "title": "t", "text": ""})
        return False

    async def query_selector_all(self, selector):
        return ["card"] if self._href().endswith("#results") else []

    async def sleep(self, seconds):
        pass

    async def save_screenshot(self, path):
        Path(path).write_bytes(b"png")


class PageLoadTests(unittest.TestCase):
    URL = "https://www.yad2.co.il/vehicles/cars?price=1-100000"
    OK = "https://www.yad2.co.il/vehicles/cars?price=1-100000#results"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = unittest.mock.patch.dict("os.environ", {"DATA_DIR": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.failures = Path(directory.name) / "scan-failures"
        for name, value in (("_CAPTCHA_TIMEOUT_IN_SECONDS", 0.05), ("_PAGE_LOAD_TIMEOUT_IN_SECONDS", 0.05)):
            patcher = unittest.mock.patch.object(Yad2CarFinder, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open(self, navigations):
        tab = FakeTab(navigations)
        finder = Yad2CarFinder()
        with unittest.mock.patch("asyncio.sleep", AsyncMock()):
            return tab, asyncio.run(finder._open(tab, self.URL))

    def test_a_bot_check_that_clears_itself_is_waited_out(self):
        tab, loaded = self.open([["https://validate.perfdrive.com/?ssa=1", self.OK]])
        self.assertTrue(loaded)
        self.assertEqual(len(tab.visits), 1)

    def test_network_errors_are_retried_and_told_apart_from_bot_checks(self):
        tab, loaded = self.open([["chrome-error://chromewebdata/"], ["chrome-error://chromewebdata/"], [self.OK]])
        self.assertTrue(loaded)
        self.assertEqual(len(tab.visits), 3)
        saved = sorted(path.name for path in self.failures.iterdir())
        self.assertEqual(len(saved), 4)  # screenshot + page state per failure
        self.assertTrue(all("network_error" in name for name in saved))

    def test_gives_up_after_three_attempts(self):
        with self.assertRaises(PageLoadError) as raised:
            self.open([["https://validate.perfdrive.com/"]] * 3)
        self.assertEqual(raised.exception.kind, "bot_check")
        self.assertIn("Yad2 requested browser validation (validate.perfdrive.com)", raised.exception.message)


class PriceSliceTests(unittest.TestCase):
    def test_price_range_and_slices(self):
        self.assertEqual(price_range(EXAMPLE_URL), (-1, 90000))
        self.assertEqual(price_range("https://www.yad2.co.il/vehicles/cars?price=1-100000"), (1, 100000))
        self.assertIsNone(price_range("https://www.yad2.co.il/vehicles/cars?price=50000--1"))
        self.assertIsNone(price_range("https://www.yad2.co.il/vehicles/cars?year=2023--1"))
        self.assertEqual(
            with_price(EXAMPLE_URL + "&page=2", -1, 45000),
            EXAMPLE_URL.replace("price=-1-90000", "price=-1-45000"),
        )
        self.assertEqual(split_price(1, 100000, [90000, 95000, 80000, 200]), [(1, 80000), (80001, 100000)])
        self.assertEqual(split_price(1, 100, []), [(1, 50), (51, 100)])
        self.assertEqual(split_price(10, 11, [11]), [(10, 10), (11, 11)])
        self.assertEqual(split_price(1, 100, [10, 20, 30, 40, 50, 60], 3), [(1, 20), (21, 40), (41, 100)])

    def test_every_ad_is_collected_through_price_slices(self):
        prices = {f"ad{n:03d}": 60000 + n * 300 for n in range(95)}
        finder = FakeFinder(prices)
        tab = SimpleNamespace(url=None)
        listings = {}
        url = "https://www.yad2.co.il/vehicles/cars?price=1-100000&km=-1-40000"
        # Prices known from earlier scans place the cuts; a few new ads are not known yet.
        plan = ScanPlan(known={f"https://www.yad2.co.il/vehicles/item/{token}": KnownAd(price, None)
                               for token, price in list(prices.items())[:85]})
        with unittest.mock.patch.object(Yad2CarFinder, "_fetch_page_results", staticmethod(finder.fetch)):
            asyncio.run(finder._collect_by_price(
                tab, url, (1, 100000), listings, plan, lambda _text: None, {"total": 95, "per_page": 40}
            ))
        self.assertEqual(len(listings), 95)
        self.assertEqual(len(finder.loaded), 4)
        # Nothing known: equal-width cuts still get everything, with a few more loads.
        finder, listings = FakeFinder(prices), {}
        with unittest.mock.patch.object(Yad2CarFinder, "_fetch_page_results", staticmethod(finder.fetch)):
            asyncio.run(finder._collect_by_price(
                tab, url, (1, 100000), listings, ScanPlan(), lambda _text: None, {"total": 95, "per_page": 40}
            ))
        self.assertEqual(len(listings), 95)
        self.assertLessEqual(len(finder.loaded), 8)
        self.assertTrue(all("km=-1-40000" in loaded for loaded in finder.loaded))


class SearchUrlTests(unittest.TestCase):
    def test_example_filters_are_kept_and_page_is_removed(self):
        self.assertEqual(normalize_search_url(EXAMPLE_URL + "&page=3&utm_source=test"), EXAMPLE_URL)

    def test_only_yad2_car_search_urls_are_accepted(self):
        for url in (
            "https://example.com/vehicles/cars?manufacturer=334",
            "https://www.yad2.co.il/vehicles/item/abc?manufacturer=334",
            "https://www.yad2.co.il/vehicles/cars",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_search_url(url)


class AlertTests(unittest.TestCase):
    def test_price_drop_and_new_listing_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot.sqlite3")
            state = BotState(path)
            search, created = state.add_search(123, EXAMPLE_URL, 10)
            self.assertTrue(created)
            old = CarListing("https://www.yad2.co.il/vehicles/item/abc", "Test car", 90000, 45000, 2, 2021)
            state.save_listings(search, [old])

            reloaded = BotState(path)
            self.assertEqual(reloaded.active_searches(), [Search(search.id, 123, EXAMPLE_URL, 10)])
            current = [
                CarListing(old.url, old.title, 85000),
                CarListing("https://www.yad2.co.il/vehicles/item/new", "New car", 80000, 30000),
            ]
            new, drops = detect_changes(reloaded.previous_listings(123), current)
            self.assertEqual(len(new), 1)
            self.assertEqual(drops[0][1], 90000)
            self.assertIn("45,000 km", format_listing(*drops[0]))
            self.assertIn("Hand: 2", format_listing(*drops[0]))
            self.assertIn("Year: 2021", format_listing(*drops[0]))
            self.assertIn("Hand: Not listed", format_listing(new[0]))
            self.assertIn("₪90,000 → ₪85,000", format_listing(*drops[0]))

    def test_a_chat_keeps_several_searches_and_is_alerted_once_per_ad(self):
        with tempfile.TemporaryDirectory() as directory:
            state = BotState(str(Path(directory) / "bot.sqlite3"))
            tracker = AdTracker(state.ads)
            electric, _ = state.add_search(123, EXAMPLE_URL, 10)
            other_url = EXAMPLE_URL.replace("manufacturer=334", "manufacturer=62")
            other, created = state.add_search(123, other_url, 15)
            self.assertTrue(created)
            self.assertEqual(state.add_search(123, other_url, 30), (Search(other.id, 123, other_url, 30), False))
            self.assertEqual([item.url for item in state.chat_searches(123)], [EXAMPLE_URL, other_url])

            shared = CarListing("https://www.yad2.co.il/vehicles/item/both", "Car", 90000)
            only_other = CarListing("https://www.yad2.co.il/vehicles/item/two", "Car", 80000)
            self.assertEqual(len(tracker.apply(123, electric.id, ScanOutput([shared])).new), 1)
            # The same ad in the chat's other search is not alerted again.
            self.assertEqual(
                [item.url for item in tracker.apply(123, other.id, ScanOutput([shared, only_other])).new],
                [only_other.url],
            )
            self.assertEqual((state.seen_count(electric.id), state.seen_count(other.id)), (1, 2))
            self.assertEqual(state.ads.search_urls(electric.id), frozenset({shared.url}))

            state.stop_search(electric.id)
            self.assertEqual(state.chat_searches(123), [Search(other.id, 123, other_url, 30)])
            self.assertEqual(len(state.previous_listings(123)), 2)  # still remembered for the rest
            state.stop_search(other.id)
            self.assertEqual(state.previous_listings(123), {})


class AccessTests(unittest.TestCase):
    def test_first_user_claims_admin_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot.sqlite3")
            state = BotState(path)
            self.assertTrue(state.claim_first_admin(123, False))
            self.assertFalse(BotState(path).claim_first_admin(456, False))
            self.assertTrue(BotState(path).is_admin(123))
            self.assertFalse(BotState(path).is_admin(456))

    def test_configured_admin_prevents_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            state = BotState(str(Path(directory) / "bot.sqlite3"))
            self.assertFalse(state.claim_first_admin(123, True))
            self.assertFalse(state.claim_first_admin(456, False))

    def test_request_approval_persists_and_promoted_admin_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot.sqlite3")
            state = BotState(path)
            state.request_access(456, "Jane Smith")
            self.assertEqual(state.pending_requests(), [(456, "Jane Smith")])
            self.assertTrue(state.resolve_request(456, True))
            self.assertFalse(state.resolve_request(456, True))
            access = AccessControl(Scanner(BotState(path)))
            self.assertTrue(access.is_allowed(456))
            state.set_admin(456, True)
            self.assertTrue(access.is_admin(456))
            state.set_admin(456, False)
            state.set_access(456, False)
            self.assertFalse(access.is_allowed(456))


class AccessCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_unapproved_user_gets_id_in_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(Scanner(BotState(str(Path(directory) / "bot.sqlite3"))))
            access.configured_admins = {123}
            message = SimpleNamespace(text="/start", reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=456),
                effective_chat=SimpleNamespace(type="private"),
                message=message,
                callback_query=None,
            )
            with self.assertRaises(ApplicationHandlerStop):
                await access.guard(update, SimpleNamespace())
            self.assertIn("456", message.reply_text.await_args.args[0])

    async def test_menu_lists_admin_commands_only_for_admins(self):
        self.assertIn("help", [command.command for command in USER_COMMANDS])
        self.assertNotIn("promote", [command.command for command in USER_COMMANDS])
        self.assertIn("promote", [command.command for command in ADMIN_COMMANDS])

        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(Scanner(BotState(str(Path(directory) / "bot.sqlite3"))))
            access.configured_admins = {123}
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message, callback_query=None)
            await access.help_command(update, SimpleNamespace())
            self.assertIn("/promote", message.reply_text.await_args.args[0])
            update.effective_user.id = 456
            await access.help_command(update, SimpleNamespace())
            self.assertNotIn("/promote", message.reply_text.await_args.args[0])
            self.assertIn("/request", message.reply_text.await_args.args[0])

    async def test_requests_command_reports_when_none_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            access = AccessControl(Scanner(BotState(str(Path(directory) / "bot.sqlite3"))))
            access.configured_admins = {123}
            message = SimpleNamespace(text="/requests", reply_text=AsyncMock())
            update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message)
            await access.admin_command(update, SimpleNamespace(args=[]))
            message.reply_text.assert_awaited_once_with("No pending requests.")


class SyncSearchesTests(unittest.IsolatedAsyncioTestCase):
    async def test_interval_changed_on_dashboard_reschedules_the_search(self):
        from telegram.ext import ApplicationBuilder

        from bot import bot as bot_module

        with tempfile.TemporaryDirectory() as directory:
            state = BotState(str(Path(directory) / "bot.sqlite3"))
            search, _ = state.add_search(101, EXAMPLE_URL, 10)
            app = ApplicationBuilder().token("123:test").build()
            scanner = Scanner(state)
            scanner.access_check = lambda chat_id: True
            with unittest.mock.patch.object(bot_module, "scanner", scanner):
                self.assertEqual(bot_module.sync_searches(app.job_queue), (1, []))
                self.assertEqual(bot_module.sync_searches(app.job_queue), (1, []))  # unchanged
                state.set_interval(search.id, 30)
                count, changed = bot_module.sync_searches(app.job_queue)
                self.assertEqual([item.interval_minutes for item in changed], [30])
                jobs = [job for job in app.job_queue.jobs() if job.data == search.id and not job.removed]
                self.assertEqual([bot_module._interval_seconds(job) for job in jobs], [1800])


class ScanQueueTests(unittest.IsolatedAsyncioTestCase):
    async def run_queue(self, searches, workers=2):
        """Queue a scan of each search (and a duplicate of the first); record overlaps."""
        started = asyncio.Event()
        release = asyncio.Event()
        running, peak, processed = set(), 0, []

        class TestScanner(Scanner):
            async def _run_scan(self, context, search):
                nonlocal peak
                running.add(search.chat_id)
                peak = max(peak, len(running))
                processed.append((search.id, context.job.chat_id))
                if len(running) == 2 or len(searches) == 2:
                    started.set()
                await release.wait()
                running.discard(search.chat_id)

        scanner = TestScanner(self.state)
        scanner.max_concurrent_scans = workers
        scanner.scan_gap_seconds = 0
        scanner.start_workers()
        try:
            for index, search in enumerate(searches):
                context = SimpleNamespace(
                    job=SimpleNamespace(chat_id=search.chat_id, data=search.id, next_t=None), chat_data={}
                )
                await scanner.scan_job(context)
                if index == 0:
                    await scanner.scan_job(context)  # Duplicate trigger is not queued.
            await asyncio.wait_for(started.wait(), 2)
            queued = scanner._queue.qsize()
            release.set()
            await asyncio.wait_for(scanner._queue.join(), 2)
            return peak, queued, processed
        finally:
            release.set()
            await scanner.stop_workers()

    async def test_two_workers_queue_third_scan_and_keep_chat_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            self.state = BotState(str(Path(directory) / "bot.sqlite3"))
            searches = [self.state.add_search(chat_id, EXAMPLE_URL, 10)[0] for chat_id in (101, 102, 103)]
            peak, queued, processed = await self.run_queue(searches)
            self.assertEqual((peak, queued), (2, 1))
            self.assertEqual(processed, [(search.id, search.chat_id) for search in searches])

    async def test_one_chats_searches_scan_one_at_a_time(self):
        with tempfile.TemporaryDirectory() as directory:
            self.state = BotState(str(Path(directory) / "bot.sqlite3"))
            searches = [
                self.state.add_search(101, EXAMPLE_URL, 10)[0],
                self.state.add_search(101, EXAMPLE_URL.replace("334", "62"), 10)[0],
            ]
            peak, _queued, processed = await self.run_queue(searches)
            self.assertEqual(peak, 1)
            self.assertEqual([search_id for search_id, _ in processed], [search.id for search in searches])


class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_dom_cards_skip_price_drop_badge(self):
        class Tab:
            async def evaluate(self, _expression):
                return json.dumps(
                    [{
                        "href": "/item/abc?spot=standard",
                        "title": "Test car",
                        "prices": ["ירד ב 3,000 ₪", "85,000 ₪"],
                        "text": "Test car\n2021 • יד 2\n85,000 ₪",
                    }]
                )

        listings = {}
        await Yad2CarFinder._fetch_dom_results(Tab(), listings)
        listing = listings["https://www.yad2.co.il/vehicles/item/abc"]
        self.assertEqual(
            (listing.title, listing.price, listing.hand, listing.year), ("Test car", 85000, 2, 2021)
        )

    def test_year_ignores_prices_and_mileage(self):
        self.assertEqual(Yad2CarFinder._parse_year("Car\n120,000 ₪\n2,019 km\n2019 • יד 1"), 2019)
        self.assertIsNone(Yad2CarFinder._parse_year("Car\n120,000 ₪"))


if __name__ == "__main__":
    unittest.main()
