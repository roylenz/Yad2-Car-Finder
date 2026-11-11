import asyncio
import datetime
import json
import os
import random
import re
import time
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import nodriver as uc
from nodriver.core.connection import ProtocolException
from car import yad2_json
from car.listing import CarListing, ScanOutput, ScanPlan
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
    # Randomised pauses between page loads; loading pages back to back triggers Yad2's bot check.
    _PAGE_PAUSE_IN_SECONDS = (2.5, 5.0)
    _ITEM_PAUSE_IN_SECONDS = (1.5, 3.5)

    @staticmethod
    async def _pause(bounds: tuple[float, float]) -> None:
        await asyncio.sleep(random.uniform(*bounds))

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

    # Reads only the Next.js queries we parse (the full page JSON is several hundred KB).
    _NEXT_DATA_EXPRESSION = """(() => {
        const element = document.getElementById("__NEXT_DATA__");
        let queries = null;
        if (element) {
            const all = JSON.parse(element.textContent)?.props?.pageProps?.dehydratedState?.queries || [];
            queries = all.filter(query => ["feed-mix", "feed", "vehicles"].includes((query.queryKey || [])[0]));
        }
        return JSON.stringify({path: location.pathname, ready: document.readyState, queries});
    })()"""

    @staticmethod
    async def _read_next_data(tab: uc.Tab, expected_path: str | None = None) -> dict | None:
        """Return the page's Next.js data, or None if it is missing or from another page."""
        try:
            page = json.loads(
                await tab.evaluate(Yad2CarFinder._NEXT_DATA_EXPRESSION, return_by_value=True)
            )
            if expected_path and (
                page.get("path", "").rstrip("/") != expected_path.rstrip("/")
                or page.get("ready") == "loading"
            ):
                return None
            if page.get("queries") is None:
                return None
            return {"props": {"pageProps": {"dehydratedState": {"queries": page["queries"]}}}}
        except (ProtocolException, ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    async def _fetch_page_results(tab: uc.Tab, listings: dict[str, CarListing]) -> int:
        """Collect the page's results; return the page count Yad2 reports (1 if unknown)."""
        next_data = await Yad2CarFinder._read_next_data(tab)
        ads = yad2_json.feed_ads(next_data) if next_data else []
        if ads:
            for ad in ads:
                listing = yad2_json.listing_from_json(ad)
                listings[listing.url] = listing
            return yad2_json.feed_pages(next_data)
        logger.warning("Feed JSON not found, reading result cards instead")
        await Yad2CarFinder._scroll_to_results_feed(tab)
        await Yad2CarFinder._fetch_dom_results(tab, listings)
        return 0

    @staticmethod
    async def _fetch_dom_results(tab: uc.Tab, listings: dict[str, CarListing]) -> None:
        expression = """JSON.stringify(Array.from(document.querySelectorAll(
            "a[data-nagish=private-item-link],a[data-nagish=agent-item-link],a[data-nagish=agent-item-no-footer-link]"
        )).map(a => ({
            href: a.getAttribute("href"),
            title: a.querySelector("h2")?.innerText || a.querySelector("[data-testid=heading-line]")?.innerText,
            prices: Array.from(a.querySelectorAll("[data-testid=price]")).map(e => e.innerText),
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
                listings[url] = CarListing(
                    url=url,
                    title=(item.get("title") or "Car listing").strip(),
                    price=Yad2CarFinder._parse_card_price(item.get("prices") or []),
                    hand=Yad2CarFinder._parse_hand(item.get("text")),
                    year=Yad2CarFinder._parse_year(item.get("text")),
                )

    @staticmethod
    def _parse_card_price(price_texts: list[str]) -> int | None:
        """Cards with a price drop show the drop amount first ("ירד ב 3,000 ₪"); skip it."""
        for text in reversed(price_texts):
            if "ירד" in text:
                continue
            digits = re.sub(r"\D", "", text)
            if digits:
                return int(digits)
        return None

    @staticmethod
    def _parse_hand(text: str | None) -> int | None:
        match = re.search(r"(?<![\u0590-\u05FF])יד\s*(\d+)", text or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_year(text: str | None) -> int | None:
        """Find a model year, preferring the card line that also shows the hand."""
        lines = (text or "").splitlines()
        lines = [line for line in lines if Yad2CarFinder._parse_hand(line)] + lines
        latest = datetime.date.today().year + 1
        for line in lines:
            for match in re.finditer(r"(?<![\d,.])(19[5-9]\d|20\d\d)(?![\d,.])", line):
                if int(match.group(1)) <= latest:
                    return int(match.group(1))
        return None

    @staticmethod
    async def _fetch_details(tab: uc.Tab, listing: CarListing) -> CarListing | None:
        """Read the full ad from its page. Return None when Yad2 no longer has the ad."""
        try:
            await Yad2CarFinder._pause(Yad2CarFinder._ITEM_PAUSE_IN_SECONDS)
            await tab.get(listing.url)
            for _ in range(20):
                if urlsplit(tab.url).hostname != "www.yad2.co.il":
                    break  # Bot check: keep what we know.
                next_data = await Yad2CarFinder._read_next_data(
                    tab, urlsplit(listing.url).path
                )
                if next_data is not None:
                    item = yad2_json.item_data(next_data)
                    if item is None:
                        return None
                    return Yad2CarFinder._merge(listing, yad2_json.listing_from_json(item, True))
                await tab.sleep(0.5)
            return await Yad2CarFinder._fetch_details_from_dom(tab, listing)
        except Exception as error:
            logger.warning("Could not read listing details", url=listing.url, error=str(error))
            return listing

    @staticmethod
    def _merge(old: CarListing, new: CarListing) -> CarListing:
        """Prefer the item page's values, keeping known ones it leaves out."""
        return replace(
            new,
            price=new.price if new.price is not None else old.price,
            km=new.km if new.km is not None else old.km,
            hand=new.hand if new.hand is not None else old.hand,
            year=new.year if new.year is not None else old.year,
            images=new.images or old.images,
            listed_at=new.listed_at or old.listed_at,
            attributes={**old.attributes, **new.attributes},
        )

    @staticmethod
    async def _fetch_details_from_dom(tab: uc.Tab, listing: CarListing) -> CarListing:
        """Fallback when the page has no JSON: read mileage, hand and year from the page text."""
        km, hand, year = listing.km, listing.hand, listing.year
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
                if year is None:
                    for label in ("שנה", "שנת יצור"):
                        year = Yad2CarFinder._parse_year(
                            await tab.evaluate(f"{expression}('{label}')")
                        )
                        if year:
                            break
                break
            await tab.sleep(0.5)
        else:
            title = await tab.evaluate("document.title") or ""
            match = re.search(r"([\d,]+)\s*ק[״\"]מ", title)
            if match:
                km = int(match.group(1).replace(",", ""))
            hand = Yad2CarFinder._parse_hand(title) or hand
            year = year or Yad2CarFinder._parse_year(title)
        return replace(listing, km=km, hand=hand, year=year)

    @staticmethod
    def _needs_details(listings: list[CarListing], plan: ScanPlan) -> list[CarListing]:
        """New ads, ads whose price changed, then a capped number of stale ones."""
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        urgent, stale = [], []
        for listing in listings:
            known = plan.known.get(listing.url)
            if known is None or known.details_fetched_at is None or known.price != listing.price:
                urgent.append(listing)
            elif now - known.details_fetched_at > plan.refresh_after:
                stale.append(listing)
        stale.sort(key=lambda item: plan.known[item.url].details_fetched_at)
        return urgent + stale[: plan.max_refresh]

    async def _load_results_page(self, tab: uc.Tab, url: str) -> bool:
        await Yad2CarFinder._pause(Yad2CarFinder._PAGE_PAUSE_IN_SECONDS)
        logger.info("Loading page", url=url)
        await tab.get(url)
        await tab.sleep(Yad2CarFinder._FETCHING_INTERVAL_IN_SECONDS)
        return await Yad2CarFinder._wait_for_page_load(
            tab,
            Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
            Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
            Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
        )

    async def find(self, search_url: str, plan: ScanPlan | None = None) -> ScanOutput:
        plan = plan or ScanPlan()
        browser = await Yad2CarFinder._start_browser(Yad2CarFinder._CHROME_ARGUMENTS)
        try:
            logger.info("Browser started")
            tab = await browser.get(search_url)
            await tab.sleep(Yad2CarFinder._FETCHING_INTERVAL_IN_SECONDS)

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

            listings: dict[str, CarListing] = {}
            if has_results:
                max_pages = await Yad2CarFinder._fetch_page_results(tab, listings)
                if max_pages == 0:
                    max_pages = await Yad2CarFinder._dom_page_count(tab)
                logger.info("Page scraped", page=f"1/{max_pages}", total_results=len(listings))
                separator = "&" if "?" in search_url else "?"
                for page_number in range(2, max_pages + 1):
                    page_url = f"{search_url}{separator}page={page_number}"
                    if not await self._load_results_page(tab, page_url):
                        break
                    await Yad2CarFinder._fetch_page_results(tab, listings)
                    logger.info(
                        "Page scraped",
                        page=f"{page_number}/{max_pages}",
                        total_results=len(listings),
                    )

            for listing in Yad2CarFinder._needs_details(list(listings.values()), plan):
                details = await Yad2CarFinder._fetch_details(tab, listing)
                if details is not None:
                    listings[listing.url] = details

            removed, left_search = [], []
            for url in sorted(plan.previous_urls - listings.keys()):
                known = plan.known.get(url)
                details = await Yad2CarFinder._fetch_details(
                    tab, CarListing(url, "Car listing", known.price if known else None)
                )
                if details is None:
                    removed.append(url)
                elif details.detailed:
                    left_search.append(details)

            logger.info(
                "Scraping complete",
                results_collected=len(listings),
                removed=len(removed),
                left_search=len(left_search),
            )
            return ScanOutput(list(listings.values()), removed, left_search)
        finally:
            browser.stop()

    @staticmethod
    async def _dom_page_count(tab: uc.Tab) -> int:
        try:
            pagination = await Yad2CarFinder._query_elements(
                tab, f"{Yad2CarFinder._PAGINATION_SELECTOR} ol li a"
            )
            if pagination and pagination[-1].text.strip().isdigit():
                return int(pagination[-1].text.strip())
        except Exception:
            logger.info("No pagination found, assuming single page")
        return 1
