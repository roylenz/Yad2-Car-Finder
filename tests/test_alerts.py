import json
import tempfile
import unittest
from pathlib import Path

from bot.scanner import detect_changes, format_listing
from bot.state import BotState
from car.car_finder import Yad2CarFinder
from car.listing import CarListing
from car.search_url import normalize_search_url

EXAMPLE_URL = (
    "https://www.yad2.co.il/vehicles/cars?manufacturer=334&model=13190"
    "&year=2023--1&price=-1-90000&km=-1-50000"
)


class SearchUrlTests(unittest.TestCase):
    def test_example_filters_are_kept_and_page_is_removed(self):
        self.assertEqual(normalize_search_url(EXAMPLE_URL + "&page=3"), EXAMPLE_URL)

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


class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_cards_provide_canonical_url_title_and_price(self):
        class Tab:
            async def evaluate(self, _expression):
                return json.dumps(
                    [{"href": "item/abc?spot=standard", "title": "Test car", "price": "85,000 ₪"}]
                )

        listings = {}
        await Yad2CarFinder._fetch_page_results(Tab(), listings)
        listing = listings["https://www.yad2.co.il/vehicles/item/abc"]
        self.assertEqual((listing.title, listing.price), ("Test car", 85000))


if __name__ == "__main__":
    unittest.main()
