"""Scheduled listing scans and Telegram change alerts."""

import asyncio
from dataclasses import replace
import os
from typing import Callable

from car.car_finder import Yad2CarFinder
from car.listing import CarListing
from common.exceptions import CustomException
from common.logger import logger
from telegram.ext import ContextTypes, JobQueue

from bot.state import BotState

LISTINGS_PER_MESSAGE = 5


def _run_scraper_sync(search_url: str, previous_prices: dict[str, int | None]) -> list[CarListing]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(Yad2CarFinder().find(search_url, previous_prices))
    finally:
        loop.close()


def detect_changes(
    previous: dict[str, CarListing], current: list[CarListing]
) -> tuple[list[CarListing], list[tuple[CarListing, int]]]:
    new = []
    drops = []
    for listing in current:
        old = previous.get(listing.url)
        if old is None:
            new.append(listing)
        elif old.price is not None and listing.price is not None and listing.price < old.price:
            listing = replace(
                listing,
                km=listing.km if listing.km is not None else old.km,
                hand=listing.hand if listing.hand is not None else old.hand,
            )
            drops.append((listing, old.price))
    return new, drops


def format_listing(listing: CarListing, previous_price: int | None = None) -> str:
    heading = "📉 Price drop" if previous_price is not None else "🚗 New car"
    price = f"₪{listing.price:,}" if listing.price is not None else "Not listed"
    if previous_price is not None and listing.price is not None:
        price = f"₪{previous_price:,} → {price} (down ₪{previous_price - listing.price:,})"
    mileage = f"{listing.km:,} km" if listing.km is not None else "Not listed"
    hand = str(listing.hand) if listing.hand is not None else "Not listed"
    return (
        f"{heading}: {listing.title}\nPrice: {price}\nMileage: {mileage}\nHand: {hand}\n{listing.url}"
    )


def schedule_scan(
    job_queue: JobQueue, scanner: "Scanner", chat_id: int, url: str, interval: int, first: int = 2
) -> None:
    for suffix in ("", "_once"):
        for job in job_queue.get_jobs_by_name(f"scan_{chat_id}{suffix}"):
            job.schedule_removal()
    if interval > 0:
        job_queue.run_repeating(
            scanner.scan_job,
            interval=interval * 60,
            first=first,
            chat_id=chat_id,
            name=f"scan_{chat_id}",
            data=url,
        )
    else:
        job_queue.run_once(
            scanner.scan_job,
            when=first,
            chat_id=chat_id,
            name=f"scan_{chat_id}_once",
            data=url,
        )


class Scanner:
    def __init__(self, state: BotState | None = None) -> None:
        self.state = state or BotState()
        self._scan_locks: dict[int, asyncio.Lock] = {}
        self.access_check: Callable[[int], bool] = lambda _chat_id: True
        self.max_concurrent_scans = int(os.getenv("MAX_CONCURRENT_SCANS", "2"))
        if self.max_concurrent_scans < 1:
            raise ValueError("MAX_CONCURRENT_SCANS must be at least 1")
        self._queue: asyncio.Queue[tuple[ContextTypes.DEFAULT_TYPE, int, str]] = asyncio.Queue()
        self._pending: set[tuple[int, str]] = set()
        self._running: set[tuple[int, str]] = set()
        self._workers: list[asyncio.Task] = []

    def start_workers(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(), name=f"scan-worker-{index + 1}")
            for index in range(self.max_concurrent_scans)
        ]
        logger.info("Scan workers started", max_concurrent_scans=self.max_concurrent_scans)

    async def stop_workers(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def queue_status(self, chat_id: int) -> str:
        if any(key[0] == chat_id for key in self._running):
            return "Scanning now"
        if any(key[0] == chat_id for key in self._pending):
            return f"Queued ({self._queue.qsize()} waiting)"
        return "Waiting for next check"

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._scan_locks:
            self._scan_locks[chat_id] = asyncio.Lock()
        return self._scan_locks[chat_id]

    def get_seen_count(self, chat_id: int) -> int:
        return self.state.seen_count(chat_id)

    async def scan_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = context.job.chat_id
        url = context.job.data
        active = self.state.get_search(chat_id)
        if active is None or active[0] != url or not self.access_check(chat_id):
            return

        key = (chat_id, url)
        if key in self._pending:
            logger.info("Scan already queued or running, skipping", chat_id=chat_id)
            return

        self._pending.add(key)
        self._queue.put_nowait((context, chat_id, url))
        logger.info("Scan queued", chat_id=chat_id, queue_length=self._queue.qsize())

    async def _worker(self) -> None:
        while True:
            context, chat_id, url = await self._queue.get()
            key = (chat_id, url)
            self._running.add(key)
            try:
                active = self.state.get_search(chat_id)
                if active is None or active[0] != url or not self.access_check(chat_id):
                    continue
                async with self._get_lock(chat_id):
                    active = self.state.get_search(chat_id)
                    if active is None or active[0] != url or not self.access_check(chat_id):
                        continue
                    try:
                        await self._run_scan(context, chat_id, url, active[1] == 0)
                    finally:
                        if active[1] == 0 and self.state.get_search(chat_id) == active:
                            self.state.stop_search(chat_id)
                            context.chat_data["scan_active"] = False
            except Exception as error:
                logger.error("scan worker failed", chat_id=chat_id, error=str(error))
            finally:
                self._running.discard(key)
                self._pending.discard(key)
                self._queue.task_done()

    async def _run_scan(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, url: str, one_time: bool
    ) -> None:
        previous = self.state.previous_listings(chat_id)
        try:
            logger.info("Scan started", chat_id=chat_id, search_url=url)
            results = await asyncio.to_thread(
                _run_scraper_sync, url, {link: item.price for link, item in previous.items()}
            )
            logger.info("Scan completed", chat_id=chat_id, results_count=len(results))
        except CustomException as error:
            logger.error("Scan failed", chat_id=chat_id, error_code=error.error_code, message=error.message)
            await self._send_safe(context, chat_id, f"⚠️ Scan failed: {error.message}")
            return
        except Exception as error:
            logger.error("Unexpected scan error", chat_id=chat_id, error=str(error))
            await self._send_safe(context, chat_id, f"⚠️ Scan failed unexpectedly: {error}")
            return

        active = self.state.get_search(chat_id)
        if active is None or active[0] != url or not self.access_check(chat_id):
            return

        new, drops = detect_changes(previous, results)
        if new:
            await self._send_safe(context, chat_id, f"🚗 {len(new)} new car(s) found")
            await self._send_listings(context, chat_id, [format_listing(item) for item in new])
        if drops:
            await self._send_safe(context, chat_id, f"📉 {len(drops)} price drop(s) found")
            await self._send_listings(
                context, chat_id, [format_listing(item, old_price) for item, old_price in drops]
            )
        if one_time and not new and not drops:
            await self._send_safe(context, chat_id, "No new cars or price drops found.")

        self.state.save_listings(chat_id, results)

    async def _send_listings(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, messages: list[str]
    ) -> None:
        for index in range(0, len(messages), LISTINGS_PER_MESSAGE):
            await self._send_safe(
                context, chat_id, "\n\n".join(messages[index : index + LISTINGS_PER_MESSAGE])
            )

    @staticmethod
    async def _send_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
        await context.bot.send_message(chat_id, text)
