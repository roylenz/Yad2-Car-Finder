import asyncio
import datetime
import json
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import nodriver as uc
from nodriver.core.connection import ProtocolException
from car import yad2_json
from car.listing import CarListing, ScanOutput, ScanPlan
from car.search_url import BASE_URL, price_range, split_price, with_price
from common.exceptions import CustomException, ErrorCode
from common.logger import logger


class PageLoadError(CustomException):
    """A Yad2 page did not load. `kind`: "bot_check", "network_error" or "timeout"."""

    MESSAGES = {
        "bot_check": "Yad2 requested browser validation",
        "network_error": "The Pi could not load Yad2 (network error)",
        "timeout": "Yad2 results did not load before timeout",
    }

    def __init__(self, kind: str, state: dict) -> None:
        self.kind, self.state = kind, state
        detail = urlsplit(state.get("href") or "").hostname or state.get("href") or "no page"
        code = ErrorCode.CAPTCHA_TIMEOUT if kind == "bot_check" else ErrorCode.PAGE_LOAD_FAILED
        super().__init__(f"{self.MESSAGES[kind]} ({detail})", code)


class Yad2CarFinder:
    _CHROME_ARGUMENTS = [
        "--disable-gpu",
        "--window-size=1920,1080",
        "--mute-audio",
    ]
    _BASE_CAR_SEARCH_URL = BASE_URL
    _FETCHING_INTERVAL_IN_SECONDS = 1
    _PAGE_LOAD_TIMEOUT_IN_SECONDS = 15
    _CAPTCHA_TIMEOUT_IN_SECONDS = 30  # a bot check that clears itself returns to Yad2 by then
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

    # Where the page really is (the tab's cached URL can lag behind), its title and a text sample.
    _PAGE_STATE_EXPRESSION = """JSON.stringify({
        href: location.href, title: document.title,
        text: (document.body?.innerText || "").slice(0, 300)
    })"""

    @staticmethod
    async def _page_state(tab: uc.Tab) -> tuple[str, dict]:
        """Classify the page: "yad2", "loading", "bot_check" (another site, e.g. the bot-check
        service) or "network_error" (Chromium's own error page: the Pi could not load it)."""
        try:
            state = json.loads(await tab.evaluate(Yad2CarFinder._PAGE_STATE_EXPRESSION))
        except (ProtocolException, TypeError, ValueError):
            state = {"href": tab.url or "", "title": None, "text": ""}
        href = state.get("href") or ""
        parts = urlsplit(href)
        if parts.scheme == "chrome-error":
            return "network_error", state
        if not href or href.startswith(("about:", "data:")):
            return "loading", state
        if parts.hostname == "www.yad2.co.il":
            return "yad2", state
        return "bot_check", state

    @staticmethod
    async def _wait_for_page_load(
        tab: uc.Tab, timeout: int, captcha_timeout: int, poll_interval: float = 1
    ) -> bool:
        """Return whether results loaded; an empty search is a valid scan result.

        A bot check (another site) often clears itself and returns to Yad2, so it is given
        `captcha_timeout` seconds before failing. Raises `PageLoadError` with what was seen.
        """
        deadline = time.monotonic() + timeout
        challenge_deadline = None
        state: dict = {}
        while True:
            kind, state = await Yad2CarFinder._page_state(tab)
            if kind == "network_error":
                raise PageLoadError("network_error", state)
            if kind == "bot_check":
                if challenge_deadline is None:
                    challenge_deadline = time.monotonic() + captcha_timeout
                    logger.warning(
                        "Bot check shown, waiting for it to clear",
                        host=urlsplit(state.get("href") or "").hostname, title=state.get("title"),
                    )
                if time.monotonic() > challenge_deadline:
                    raise PageLoadError("bot_check", state)
            elif kind == "yad2":
                if challenge_deadline is not None:
                    logger.info("Bot check cleared")
                    challenge_deadline = None
                    deadline = time.monotonic() + timeout
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
            if challenge_deadline is None and time.monotonic() > deadline:
                raise PageLoadError("timeout", state)
            await tab.sleep(poll_interval)

    async def _open(self, tab: uc.Tab, url: str, attempts: int = 3, wait: float = 10) -> bool:
        """Load a results page, retrying (after `wait` seconds) when it fails to load."""
        for attempt in range(1, attempts + 1):
            await tab.get(url)
            await tab.sleep(Yad2CarFinder._FETCHING_INTERVAL_IN_SECONDS)
            try:
                return await Yad2CarFinder._wait_for_page_load(
                    tab,
                    Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
                    Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
                    Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
                )
            except PageLoadError as error:
                await Yad2CarFinder._save_failure(tab, error, attempt)
                logger.warning(
                    "Page failed to load", kind=error.kind, attempt=f"{attempt}/{attempts}",
                    href=error.state.get("href"), title=error.state.get("title"),
                )
                if attempt == attempts:
                    raise
                await asyncio.sleep(wait)
        return False

    @staticmethod
    async def _save_failure(tab: uc.Tab, error: "PageLoadError", attempt: int) -> None:
        """Keep a screenshot and the page text of a failed load in DATA_DIR/scan-failures."""
        try:
            folder = Path(os.getenv("DATA_DIR", "data")) / "scan-failures"
            folder.mkdir(parents=True, exist_ok=True)
            stem = folder / f"{datetime.datetime.now(datetime.UTC):%Y%m%d-%H%M%S}-{error.kind}-{attempt}"
            stem.with_suffix(".json").write_text(json.dumps(error.state, ensure_ascii=False, indent=1))
            await tab.save_screenshot(str(stem.with_suffix(".png")))
            for old in sorted(folder.iterdir())[:-60]:  # keep the latest 30 failures
                old.unlink(missing_ok=True)
        except Exception as save_error:  # diagnostics are best effort
            logger.warning("Could not save the failed page", error=str(save_error))

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
    async def _fetch_page_results(
        tab: uc.Tab, listings: dict[str, CarListing], feed: dict | None = None
    ) -> int:
        """Collect the page's results; return the page count Yad2 reports (1 if unknown).

        `feed` receives Yad2's total result count, to check that no results were missed.
        """
        next_data = await Yad2CarFinder._read_next_data(tab)
        ads = yad2_json.feed_ads(next_data) if next_data else []
        if ads:
            if feed is not None and feed.get("total") is None:
                feed["total"] = yad2_json.feed_total(next_data)
                feed["per_page"] = yad2_json.feed_per_page(next_data)
            unknown = yad2_json.unknown_feed_groups(next_data)
            if unknown:
                logger.warning("Feed has ad lists the parser skips", groups=unknown)
            before = len(listings)
            for ad in ads:
                listing = yad2_json.listing_from_json(ad)
                listings[listing.url] = listing
            data = yad2_json.feed_data(next_data) or {}
            logger.info(
                "Feed page",
                groups={key: len(value) for key, value in data.items() if isinstance(value, list)},
                pagination=data.get("pagination"),
                new=len(listings) - before,
                repeated=len(ads) - (len(listings) - before),
            )
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

    _MAX_PRICE_SLICES = 12  # page loads per scan when splitting a search by price

    async def _load_results_page(self, tab: uc.Tab, url: str) -> bool:
        await Yad2CarFinder._pause(Yad2CarFinder._PAGE_PAUSE_IN_SECONDS)
        logger.info("Loading page", url=url)
        return await self._open(tab, url)

    async def _collect_pages(
        self, tab: uc.Tab, search_url: str, max_pages: int,
        listings: dict[str, CarListing], report: Callable[[str], None],
    ) -> None:
        separator = "&" if "?" in search_url else "?"
        for page_number in range(2, max_pages + 1):
            report(f"Results page {page_number}/{max_pages}")
            page_url = f"{search_url}{separator}page={page_number}"
            if not await self._load_results_page(tab, page_url):
                break
            await Yad2CarFinder._fetch_page_results(tab, listings)
            logger.info("Page scraped", page=f"{page_number}/{max_pages}", total_results=len(listings))

    async def _collect_by_price(
        self, tab: uc.Tab, search_url: str, bounds: tuple[int, int],
        listings: dict[str, CarListing], plan: ScanPlan, report: Callable[[str], None],
        first_page: dict,
    ) -> None:
        def known_prices() -> list[int]:
            # Prices seen before and in this scan so far: cut where the ads actually are.
            return [ad.price for ad in plan.known.values() if ad.price] + [
                listing.price for listing in listings.values() if listing.price
            ]

        def slices(low: int, high: int, total: int, per_page: int) -> list[tuple[int, int]]:
            # Aim for slices about 3/4 full, so a few new ads don't spill onto a second page.
            parts = -(-total * 4 // (per_page * 3))
            return split_price(low, high, known_prices(), parts)

        per_page = first_page.get("per_page") or 40
        pending = slices(*bounds, first_page["total"], per_page)
        loads = 0
        while pending and loads < self._MAX_PRICE_SLICES:
            low, high = pending.pop(0)
            loads += 1
            report(f"Results {low:,}-{high:,} ₪ ({len(listings)} so far)")
            if not await self._load_results_page(tab, with_price(search_url, low, high)):
                break
            feed: dict = {}
            pages = await Yad2CarFinder._fetch_page_results(tab, listings, feed)
            logger.info(
                "Price slice scraped", prices=f"{low}-{high}", slice_total=feed.get("total"),
                pages=pages, total_results=len(listings),
            )
            if pages > 1:
                if high - low > 1 and feed.get("total"):
                    pending[:0] = slices(low, high, feed["total"], feed.get("per_page") or per_page)
                else:
                    await self._collect_pages(tab, with_price(search_url, low, high), pages, listings, report)

    async def find(
        self,
        search_url: str,
        plan: ScanPlan | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ScanOutput:
        plan = plan or ScanPlan()
        report = progress or (lambda _text: None)
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

            try:
                has_results = await Yad2CarFinder._wait_for_page_load(
                    tab,
                    Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
                    Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
                    Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
                )
            except PageLoadError as error:
                await Yad2CarFinder._save_failure(tab, error, 1)
                logger.warning("Page failed to load", kind=error.kind, attempt="1/3",
                               href=error.state.get("href"), title=error.state.get("title"))
                await asyncio.sleep(10)
                has_results = await self._open(tab, search_url, attempts=2)

            listings: dict[str, CarListing] = {}
            feed: dict = {}
            report("Results page 1")
            if has_results:
                max_pages = await Yad2CarFinder._fetch_page_results(tab, listings, feed)
                if max_pages == 0:
                    max_pages = await Yad2CarFinder._dom_page_count(tab)
                logger.info("Page scraped", page=f"1/{max_pages}", total_results=len(listings))
                bounds = price_range(search_url)
                if max_pages > 1 and bounds and feed.get("total"):
                    # Yad2 serves each page in one of two layouts (A/B test) that order ads
                    # differently, so page 2 repeats some of page 1 and skips others. Instead,
                    # split the price range into slices that each fit on one page.
                    await self._collect_by_price(tab, search_url, bounds, listings, plan, report, feed)
                else:
                    await self._collect_pages(tab, search_url, max_pages, listings, report)
                if feed.get("total") and len(listings) < feed["total"]:
                    logger.warning(
                        "Fewer results than Yad2 reports",
                        collected=len(listings),
                        yad2_total=feed["total"],
                    )

            to_fetch = Yad2CarFinder._needs_details(list(listings.values()), plan)
            for index, listing in enumerate(to_fetch, 1):
                report(f"Ad pages {index}/{len(to_fetch)} ({len(listings)} results)")
                details = await Yad2CarFinder._fetch_details(tab, listing)
                if details is not None:
                    listings[listing.url] = details

            removed, left_search = [], []
            missing = sorted(plan.previous_urls - listings.keys())
            for index, url in enumerate(missing, 1):
                report(f"Checking ads that left the results {index}/{len(missing)}")
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
            return ScanOutput(
                list(listings.values()), removed, left_search, len(to_fetch) + len(missing)
            )
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
