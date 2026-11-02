import os
import time

import nodriver as uc
from car.car_search_filter import CarSearchFilter
from common.exceptions import CustomException, ErrorCode
from common.logger import logger


class Yad2CarFinder:
    _CHROME_ARGUMENTS = [
        "--disable-gpu",
        "--window-size=1920,1080",
        "--mute-audio",
    ]
    _BASE_CAR_SEARCH_URL = "https://www.yad2.co.il/vehicles/cars"
    _BASE_URL = "https://www.yad2.co.il"
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
    def _get_car_search_url(base_car_search_url: str, car_search_filter: CarSearchFilter) -> str:
        car_search_url = base_car_search_url
        car_search_url_parameters = car_search_filter.get_url_parameters()
        if len(car_search_url_parameters) > 0:
            car_search_url += f'?{"&".join(car_search_url_parameters)}'
        return car_search_url

    @staticmethod
    async def _query_elements(tab: uc.Tab, css_selector: str) -> list:
        """Run a CSS selector query via CDP and return matching elements."""
        return await tab.query_selector_all(css_selector)

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
    ) -> None:
        """Wait for page content to appear, with a fallback for manual captcha solving."""
        elements = await Yad2CarFinder._poll_for_elements(
            tab, Yad2CarFinder._PAGE_LOADED_SELECTOR, timeout, poll_interval
        )
        if elements:
            return

        logger.warning(
            "Feed items not found, possible CAPTCHA",
            timeout=timeout,
            captcha_timeout=captcha_timeout,
        )
        elements = await Yad2CarFinder._poll_for_elements(
            tab, Yad2CarFinder._PAGE_LOADED_SELECTOR, captcha_timeout, poll_interval
        )
        if elements:
            logger.info("Feed items appeared after captcha was solved")
            return

        raise CustomException(
            f"Feed items not found after {captcha_timeout}s captcha timeout",
            ErrorCode.CAPTCHA_TIMEOUT,
        )

    @staticmethod
    async def _scroll_to_results_feed(tab: uc.Tab) -> None:
        """Scroll down to trigger lazy loading of the actual results feed."""
        for _ in range(5):
            await tab.evaluate("window.scrollBy(0, 2000)")
            await tab.sleep(0.5)

    @staticmethod
    async def _fetch_page_results(tab: uc.Tab, result_urls: set[str]) -> None:
        feed_links = await Yad2CarFinder._query_elements(tab, Yad2CarFinder._RESULTS_FEED_SELECTOR)
        for link_element in feed_links:
            href = link_element.attrs.get("href")
            if not href:
                continue
            if "spot=look_alike" in href:
                continue
            clean_href = href.split("?")[0]
            if not clean_href.startswith("http"):
                clean_href = Yad2CarFinder._BASE_URL + "/" + clean_href.lstrip("/")
            result_urls.add(clean_href)

    async def find(self, car_search_filter: CarSearchFilter) -> set[str]:
        browser = await Yad2CarFinder._start_browser(Yad2CarFinder._CHROME_ARGUMENTS)
        try:
            logger.info("Browser started")

            car_search_url = Yad2CarFinder._get_car_search_url(
                Yad2CarFinder._BASE_CAR_SEARCH_URL, car_search_filter
            )

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

            await Yad2CarFinder._wait_for_page_load(
                tab,
                Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
                Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
                Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
            )

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

            result_urls: set[str] = set()

            for page_number in range(1, max_pages + 1):
                if page_number > 1:
                    separator = "?" if car_search_url == Yad2CarFinder._BASE_CAR_SEARCH_URL else "&"
                    page_url = f"{car_search_url}{separator}page={page_number}"

                    logger.info("Loading page", page=page_number, url=page_url)
                    await tab.get(page_url)
                    await tab.sleep(Yad2CarFinder._FETCHING_INTERVAL_IN_SECONDS)

                    await Yad2CarFinder._wait_for_page_load(
                        tab,
                        Yad2CarFinder._PAGE_LOAD_TIMEOUT_IN_SECONDS,
                        Yad2CarFinder._CAPTCHA_TIMEOUT_IN_SECONDS,
                        Yad2CarFinder._POLL_INTERVAL_IN_SECONDS,
                    )

                    await Yad2CarFinder._scroll_to_results_feed(tab)
                    await Yad2CarFinder._poll_for_elements(
                        tab, Yad2CarFinder._RESULTS_FEED_SELECTOR, timeout=10
                    )

                await Yad2CarFinder._fetch_page_results(tab, result_urls)
                logger.info(
                    "Page scraped",
                    page=f"{page_number}/{max_pages}",
                    total_results=len(result_urls),
                )

            logger.info("Scraping complete", results_collected=len(result_urls))
            return result_urls
        finally:
            browser.stop()
