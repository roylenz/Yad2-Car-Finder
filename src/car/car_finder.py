import json
import os
import re
import time
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import nodriver as uc
from nodriver.core.connection import ProtocolException
from car.listing import CarListing
from car.search_url import BASE_URL
from common.exceptions import CustomException, ErrorCode
from common.logger import logger


class Yad2CarFinder:
    _CHROME_ARGUMENTS = [
        "--disable-gpu",
        "--window-size=1920,1080",
        "--mute-audio",
    ]
    _BASE_CAR_SEARCH_URL = BASE_URL
    _FETCHING_INTERVAL_IN_SECONDS = 1
    _PAGE_LOAD_TIMEOUT_IN_SECONDS = 15
    _CAPTCHA_TIMEOUT_IN_SECONDS = 120
    _POLL_INTERVAL_IN_SECONDS = 1

    _RESULTS_FEED_SELECTOR = (
        "a[data-nagish='private-item-link'], "
        "a[data-nagish='agent-item-link'], "
        "a[data-nagish='agent-item-no-footer-link']"
    )
    _PAGE_LOADED_SELECTOR = "a[data-nagish='feed-item-card-link']"
    _PAGINATION_SELECTOR = "nav[data-nagish='pagination-navbar']"

    @staticmethod
    async def _start_browser(chrome_arguments: list[str]) -> uc.Browser:
        try:
            headless = os.getenv("CHROME_HEADLESS", "false").lower() in ("1", "true", "yes")
            browser_path = os.getenv("CHROME_PATH") or None
            no_sandbox = os.getenv("CHROME_NO_SANDBOX", "false").lower() in (
                "1", "true", "yes"
            )
            return await uc.start(
                browser_args=chrome_arguments,
                browser_executable_path=browser_path,
                headless=headless,
                sandbox=not no_sandbox,
            )
        except Exception as e:
            raise CustomException(
                f"Failed to start browser: {e}",
                ErrorCode.BROWSER_START_FAILED,
            ) from e

    @staticmethod
    async def _query_elements(tab: uc.Tab, css_selector: str) -> list:
        """Run a CSS selector query via CDP and return matching elements."""
        try:
            return await tab.query_selector_all(css_selector)
        except ProtocolException:
            # The document can be replaced while the search page is loading.
            return []

    @staticmethod
    async def _poll_for_elements(
        tab: uc.Tab, css_selector: str, timeout: int, poll_interval: float = 1
    ) -> list:
        """Poll for elements matching a CSS selector until found or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elements = await Yad2CarFinder._query_elements(tab, css_selector)
            if elements:
                return elements
            await tab.sleep(poll_interval)
        return []

    @staticmethod
    async def _wait_for_page_load(
        tab: uc.Tab, timeout: int, captcha_timeout: int, poll_interval: float = 1
    ) -> bool:
        """Return whether results loaded; an empty search is a valid scan result."""
        deadline = time.monotonic() + timeout + captcha_timeout
        while time.monotonic() < deadline:
            if urlsplit(tab.url).hostname != "www.yad2.co.il":
                raise CustomException("Yad2 requested browser validation", ErrorCode.CAPTCHA_TIMEOUT)
            if await Yad2CarFinder._query_elements(tab, Yad2CarFinder._PAGE_LOADED_SELECTOR):
                return True
            try:
                no_results = await tab.evaluate(
                    "document.body?.innerText.includes(\"מה שחיפשת לא נמצא\")"
                )
            except ProtocolException:
                no_results = False
            if no_results:
                return False
            await tab.sleep(poll_interval)

        raise CustomException(
            "Yad2 results did not load before timeout",
            ErrorCode.CAPTCHA_TIMEOUT,
        )

    @staticmethod
    async def _scroll_to_results_feed(tab: uc.Tab) -> None:
        """Scroll down to trigger lazy loading of the actual results feed."""
        for _ in range(5):
            await tab.evaluate("window.scrollBy(0, 2000)")
            await tab.sleep(0.5)

    @staticmethod
    async def _fetch_page_results(tab: uc.Tab, listings: dict[str, CarListing]) -> None:
        expression = """JSON.stringify(Array.from(document.querySelectorAll(
            "a[data-nagish=private-item-link],a[data-nagish=agent-item-link],a[data-nagish=agent-item-no-footer-link]"
        )).map(a => ({
            href: a.getAttribute("href"),
            title: a.querySelector("h2")?.innerText || a.querySelector("[data-testid=heading-line]")?.innerText,
            price: a.querySelector("[data-testid=price]")?.innerText,
            text: a.innerText
        })))"""
        for item in json.loads(await tab.evaluate(expression)):
            href = item.get("href")
            if not href or "spot=look_alike" in href:
                continue
            if href.startswith(("/item/", "item/")):
                url = urljoin("https://www.yad2.co.il/vehicles/", href.lstrip("/"))
            else:
                url = urljoin(Yad2CarFinder._BASE_CAR_SEARCH_URL, href)
            url = url.split("?", 1)[0]
            if urlsplit(url).hostname == "www.yad2.co.il" and urlsplit(url).path.startswith(
                "/vehicles/item/"
            ):
                price_text = item.get("price") or ""
                digits = re.sub(r"\D", "", price_text)
                listings[url] = CarListing(
                    url=url,
                    title=(item.get("title") or "Car listing").strip(),
                    price=int(digits) if digits else None,
                    hand=Yad2CarFinder._parse_hand(item.get("text")),
                )

    @staticmethod
    def _parse_hand(text: str | None) -> int | None:
        match = re.search(r"(?<![\u0590-\u05FF])יד\s*(\d+)", text or "")
        return int(match.group(1)) if match else None

    @staticmethod
    async def _fetch_details(tab: uc.Tab, listing: CarListing) -> CarListing:
        """Read mileage and hand from the listing page, keeping known values as fallback."""
        km, hand = listing.km, listing.hand
        try:
            await tab.get(listing.url)
            expression = """(label => Array.from(document.querySelectorAll("span"))
                .filter(x => x.children.length === 0 && x.textContent.trim() === label)
                .map(x => x.parentElement.parentElement.querySelector("[data-testid=detail-card-value]")?.textContent)
                .find(Boolean) || null)"""
            for _ in range(10):
                km_value = await tab.evaluate(f"{expression}('ק״מ')")
                if km_value:
                    digits = re.sub(r"\D", "", km_value)
                    km = int(digits) if digits else km
                    hand_value = await tab.evaluate(f"{expression}('יד')")
                    if hand_value:
                        digits = re.sub(r"\D", "", hand_value)
                        hand = int(digits) if digits else hand
                    break
                await tab.sleep(0.5)
            else:
                title = await tab.evaluate("document.title") or ""
                match = re.search(r"([\d,]+)\s*ק[״\"]מ", title)
                if match:
                    km = int(match.group(1).replace(",", ""))
                hand = Yad2CarFinder._parse_hand(title) or hand
        except Exception as error:
            logger.warning("Could not read listing details", url=listing.url, error=str(error))
        return replace(listing, km=km, hand=hand)

    async def find(
        self, search_url: str, previous_prices: dict[str, int | None] | None = None
    ) -> list[CarListing]:
        browser = await Yad2CarFinder._start_browser(Yad2CarFinder._CHROME_ARGUMENTS)
        try:
            logger.info("Browser started")

            car_search_url = search_url

            logger.info("Loading page", url=car_search_url)
            tab = await browser.get(car_search_url)
            await tab.sleep(Yad2CarFinder._FETCHING_INTERVAL_IN_SECONDS)
            logger.info("Page loaded")

            try:
                popup_iframe = await Yad2CarFinder._poll_for_elements(
                    tab, "iframe[title='Modal Message']", timeout=3
                )
                if popup_iframe:
                    close_btn = await popup_iframe[0].query_selector(".bz-close-btn")
                    if close_btn:
                        await close_btn.click()
                    logger.info("Closed advertisement popup")
            except Exception:
                pass

            has_results = await Yad2CarFinder._wait_for_page_load(
                tab,
                Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
                Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
                Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
            )
            if not has_results:
                return []

            logger.info("Scrolling to load results feed")
            await Yad2CarFinder._scroll_to_results_feed(tab)

            results_found = await Yad2CarFinder._poll_for_elements(
                tab, Yad2CarFinder._RESULTS_FEED_SELECTOR, timeout=10
            )
            if not results_found:
                logger.warning("Results feed not found after scrolling")

            max_pages = 1
            try:
                pagination = await Yad2CarFinder._query_elements(
                    tab, f"{Yad2CarFinder._PAGINATION_SELECTOR} ol li a"
                )
                if pagination:
                    last_text = pagination[-1].text.strip()
                    if last_text.isdigit():
                        max_pages = int(last_text)
                        logger.info("Pagination detected", max_pages=max_pages)
            except (ValueError, Exception):
                logger.info("No pagination found, assuming single page")

            listings: dict[str, CarListing] = {}

            for page_number in range(1, max_pages + 1):
                if page_number > 1:
                    separator = "?" if car_search_url == Yad2CarFinder._BASE_CAR_SEARCH_URL else "&"
                    page_url = f"{car_search_url}{separator}page={page_number}"

                    logger.info("Loading page", page=page_number, url=page_url)
                    await tab.get(page_url)
                    await tab.sleep(Yad2CarFinder._FETCHING_INTERVAL_IN_SECONDS)

                    has_results = await Yad2CarFinder._wait_for_page_load(
                        tab,
                        Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
                        Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
                        Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
                    )
                    if not has_results:
                        break

                    await Yad2CarFinder._scroll_to_results_feed(tab)
                    await Yad2CarFinder._poll_for_elements(
                        tab, Yad2CarFinder._RESULTS_FEED_SELECTOR, timeout=10
                    )

                await Yad2CarFinder._fetch_page_results(tab, listings)
                logger.info(
                    "Page scraped",
                    page=f"{page_number}/{max_pages}",
                    total_results=len(listings),
                )

            previous_prices = previous_prices or {}
            for url, listing in list(listings.items()):
                previous_price = previous_prices.get(url)
                if url not in previous_prices or (
                    listing.price is not None
                    and previous_price is not None
                    and listing.price < previous_price
                ):
                    listings[url] = await Yad2CarFinder._fetch_details(tab, listing)

            logger.info("Scraping complete", results_collected=len(listings))
            return list(listings.values())
        finally:
            browser.stop()
