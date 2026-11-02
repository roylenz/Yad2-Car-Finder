import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.scanner import detect_changes, format_listing
from bot.state import BotState
from bot.access import AccessControl
from bot.scanner import Scanner
from bot.menu import ADMIN_COMMANDS, USER_COMMANDS
from telegram.ext import ApplicationHandlerStop
from car.car_finder import Yad2CarFinder
from car.listing import CarListing
from car.search_url import normalize_search_url

EXAMPLE_URL = (
    "https://www.yad2.co.il/vehicles/cars?manufacturer=334&model=13190"
    "&year=2023--1&price=-1-90000&km=-1-50000"
)


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
            state.set_search(123, EXAMPLE_URL, 10)
            old = CarListing("https://www.yad2.co.il/vehicles/item/abc", "Test car", 90000, 45000)
            state.save_listings(123, [old])

            reloaded = BotState(path)
            self.assertEqual(reloaded.active_searches(), [(123, EXAMPLE_URL, 10)])
            current = [
                CarListing(old.url, old.title, 85000),
                CarListing("https://www.yad2.co.il/vehicles/item/new", "New car", 80000, 30000),
            ]
            new, drops = detect_changes(reloaded.previous_listings(123), current)
            self.assertEqual(len(new), 1)
            self.assertEqual(drops[0][1], 90000)
            self.assertIn("45,000 km", format_listing(*drops[0]))
            self.assertIn("₪90,000 → ₪85,000", format_listing(*drops[0]))

    def test_new_search_clears_previous_listing_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            state = BotState(str(Path(directory) / "bot.sqlite3"))
            state.set_search(123, EXAMPLE_URL, 10)
            state.save_listings(123, [CarListing("https://www.yad2.co.il/vehicles/item/abc", "Car", 1)])
            state.set_search(123, EXAMPLE_URL.replace("manufacturer=334", "manufacturer=62"), 10)
            self.assertEqual(state.seen_count(123), 0)


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


class ScanQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_workers_queue_third_scan_and_keep_chat_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            state = BotState(str(Path(directory) / "bot.sqlite3"))
            for chat_id in (101, 102, 103):
                state.set_search(chat_id, EXAMPLE_URL, 10)

            started = asyncio.Event()
            release = asyncio.Event()
            running = 0
            peak = 0
            processed = []

            class TestScanner(Scanner):
                async def _run_scan(self, context, chat_id, url, one_time):
                    nonlocal running, peak
                    running += 1
                    peak = max(peak, running)
                    processed.append((chat_id, context.job.chat_id))
                    if running == 2:
                        started.set()
                    await release.wait()
                    running -= 1

            scanner = TestScanner(state)
            scanner.max_concurrent_scans = 2
            scanner.start_workers()
            try:
                for chat_id in (101, 102, 103):
                    context = SimpleNamespace(
                        job=SimpleNamespace(chat_id=chat_id, data=EXAMPLE_URL), chat_data={}
                    )
                    await scanner.scan_job(context)
                    if chat_id == 101:
                        await scanner.scan_job(context)  # Duplicate trigger is not queued.
                await asyncio.wait_for(started.wait(), 2)
                self.assertEqual(peak, 2)
                self.assertEqual(scanner._queue.qsize(), 1)
                release.set()
                await asyncio.wait_for(scanner._queue.join(), 2)
                self.assertEqual(processed, [(101, 101), (102, 102), (103, 103)])
            finally:
                release.set()
                await scanner.stop_workers()


class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_cards_provide_canonical_url_title_and_price(self):
        class Tab:
            async def evaluate(self, _expression):
                return json.dumps(
                    [{"href": "/item/abc?spot=standard", "title": "Test car", "price": "85,000 ₪"}]
                )

        listings = {}
        await Yad2CarFinder._fetch_page_results(Tab(), listings)
        listing = listings["https://www.yad2.co.il/vehicles/item/abc"]
        self.assertEqual((listing.title, listing.price), ("Test car", 85000))


if __name__ == "__main__":
    unittest.main()
