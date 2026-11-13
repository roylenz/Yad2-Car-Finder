"""Background lookup of seller numbers for ads that don't have one yet.

Scans read the number when they open an ad page (car_finder._fetch_details); older ads, and any
the scan missed, are opened here one by one between scans, with a pause between ads, and their
number read like a buyer does it (car/seller_phone.py). An ad gets MAX_ATTEMPTS tries; one with no "show number"
button (a dealer or leasing ad with a contact form instead) is not tried again.
A bot check ends the round: the next one starts after the pipeline's idle wait.
"""

import asyncio
import random
from collections.abc import Callable

from car import seller_phone
from car.car_finder import Yad2CarFinder
from common.logger import logger
from storage.ads import AdRepository

BATCH = 15
MAX_ATTEMPTS = 3
PAUSE_SECONDS = (4.0, 8.0)


class PhoneCollector:
    def __init__(self, ads: AdRepository, batch: int = BATCH) -> None:
        self.ads = ads
        self.batch = batch

    async def run(self, busy: Callable[[], bool] = lambda: False,
                  progress: Callable[[int, int], None] | None = None) -> int:
        """Look up one batch; returns how many numbers were found (0 also when nothing is waiting)."""
        pending = self.ads.ads_without_phone(self.batch, MAX_ATTEMPTS)
        if not pending or busy():
            return 0
        total = self.ads.count_without_phone(MAX_ATTEMPTS)
        found = 0
        browser = await Yad2CarFinder._start_browser(Yad2CarFinder._CHROME_ARGUMENTS)
        try:
            await browser.get("about:blank")
            for index, (token, url) in enumerate(pending):
                if busy():
                    break
                if progress:
                    progress(index, total)
                number, tab = await seller_phone.reveal_in_new_tab(browser, url)
                attempts = 1
                if number is None:
                    kind, state = await Yad2CarFinder._page_state(tab)
                    if kind == "bot_check":
                        logger.warning("Bot check while reading seller numbers; stopping this round")
                        return found
                    if kind == "yad2" and not await seller_phone.has_button(tab):
                        attempts = MAX_ATTEMPTS  # no number on this ad
                await tab.close()
                self.ads.save_phone(token, number, attempts=attempts)
                found += number is not None
                await asyncio.sleep(random.uniform(*PAUSE_SECONDS))
        finally:
            browser.stop()
        logger.info("Seller numbers read", found=found, tried=len(pending))
        return found
