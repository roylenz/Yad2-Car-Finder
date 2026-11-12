"""Scheduled listing scans and Telegram change alerts."""

import asyncio
import os
from typing import Callable

from car.car_finder import Yad2CarFinder
from car.listing import CarListing, ScanOutput, ScanPlan
from common.exceptions import CustomException
from common.logger import logger
from telegram.ext import ContextTypes, JobQueue

import datetime
import time

from bot.state import BotState, Search
from car.search_url import describe_search

from storage.db import utcnow
from storage.scans import ScanLog
from tracking.tracker import AdTracker, detect_changes  # noqa: F401 (re-exported)

LISTINGS_PER_MESSAGE = 5


def _run_scraper_sync(
    search_url: str, plan: ScanPlan, progress: Callable[[str], None] | None = None
) -> ScanOutput:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(Yad2CarFinder().find(search_url, plan, progress))
    finally:
        loop.close()


def _listed_for(listing: CarListing) -> str | None:
    if listing.listed_at is None:
        return None
    days = (utcnow() - listing.listed_at).days
    return "today" if days < 1 else f"{days} day{'s' if days != 1 else ''} ago"


def format_listing(listing: CarListing, previous_price: int | None = None) -> str:
    heading = "📉 Price drop" if previous_price is not None else "🚗 New car"
    price = f"₪{listing.price:,}" if listing.price is not None else "Not listed"
    if previous_price is not None and listing.price is not None:
        price = f"₪{previous_price:,} → {price} (down ₪{previous_price - listing.price:,})"
    mileage = f"{listing.km:,} km" if listing.km is not None else "Not listed"
    hand = str(listing.hand) if listing.hand is not None else "Not listed"
    year = str(listing.year) if listing.year is not None else "Not listed"
    lines = [
        f"{heading}: {listing.title}",
        f"Year: {year}",
        f"Price: {price}",
        f"Mileage: {mileage}",
        f"Hand: {hand}",
    ]
    listed = _listed_for(listing)
    if listed:
        lines.append(f"Listed: {listed}")
    lines.append(listing.url)
    dashboard_url = os.getenv("DASHBOARD_URL", "").rstrip("/")
    if dashboard_url:
        lines.append(f"History: {dashboard_url}/ads/{listing.token}")
    return "\n".join(lines)


def _job_name(search_id: int) -> str:
    return f"scan_{search_id}"


def unschedule_scan(job_queue: JobQueue, search_id: int) -> None:
    for suffix in ("", "_once"):
        for job in job_queue.get_jobs_by_name(f"{_job_name(search_id)}{suffix}"):
            job.schedule_removal()


def schedule_scan(job_queue: JobQueue, scanner: "Scanner", search: Search, first: int = 2) -> None:
    unschedule_scan(job_queue, search.id)
    scanner.scan_log.set_next(search.id, utcnow() + datetime.timedelta(seconds=first))
    if search.interval_minutes > 0:
        job_queue.run_repeating(
            scanner.scan_job,
            interval=search.interval_minutes * 60,
            first=first,
            chat_id=search.chat_id,
            name=_job_name(search.id),
            data=search.id,
        )
    else:
        job_queue.run_once(
            scanner.scan_job,
            when=first,
            chat_id=search.chat_id,
            name=f"{_job_name(search.id)}_once",
            data=search.id,
        )


def first_delay(last_started: datetime.datetime | None, interval_minutes: int, now: datetime.datetime | None = None) -> int:
    """Seconds until a restored search should scan: keep its regular rhythm across restarts.

    Scanning right after a restart, seconds after the previous scan, trips Yad2's bot check.
    """
    if last_started is None or interval_minutes <= 0:
        return 5
    elapsed = ((now or utcnow()) - last_started).total_seconds()
    return max(5, int(interval_minutes * 60 - elapsed))


def _next_run(job, interval_minutes: int) -> datetime.datetime | None:
    """When this repeating job fires next (naive UTC), or None for a one-time scan."""
    if interval_minutes <= 0:
        return None
    next_t = getattr(job, "next_t", None)
    if isinstance(next_t, datetime.datetime) and next_t > datetime.datetime.now(datetime.UTC):
        return next_t.astimezone(datetime.UTC).replace(tzinfo=None)
    return utcnow() + datetime.timedelta(minutes=interval_minutes)


class Scanner:
    def __init__(self, state: BotState | None = None) -> None:
        self.state = state or BotState()
        self.tracker = AdTracker(self.state.ads)
        self.scan_log = ScanLog(self.state.engine)
        self._scan_ids: dict[int, int] = {}  # search id -> scan log id
        self.on_scan_complete: Callable[[], None] = lambda: None
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self.access_check: Callable[[int], bool] = lambda _chat_id: True
        self.max_concurrent_scans = int(os.getenv("MAX_CONCURRENT_SCANS", "1"))
        if self.max_concurrent_scans < 1:
            raise ValueError("MAX_CONCURRENT_SCANS must be at least 1")
        # Back-to-back page loads trip Yad2's bot check, so scans start this long apart.
        self.scan_gap_seconds = float(os.getenv("SCAN_GAP_SECONDS", "45"))
        self._gap_lock = asyncio.Lock()
        self._last_scan_end: float | None = None
        self._queue: asyncio.Queue[tuple[ContextTypes.DEFAULT_TYPE, int]] = asyncio.Queue()
        self._pending: set[int] = set()
        self._running: set[int] = set()
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

    def queue_status(self, search_id: int) -> str:
        if search_id in self._running:
            return "Scanning now"
        if search_id in self._pending:
            return f"Queued ({self._queue.qsize()} waiting)"
        return "Waiting for next check"

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    def get_seen_count(self, search_id: int) -> int:
        return self.state.seen_count(search_id)

    def _current(self, search_id: int) -> Search | None:
        search = self.state.get_search(search_id)
        if search is None or not self.access_check(search.chat_id):
            return None
        return search

    async def scan_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        search = self._current(context.job.data)
        if search is None:
            return
        self.scan_log.set_next(search.id, _next_run(context.job, search.interval_minutes))
        if search.id in self._pending:
            logger.info("Scan already queued or running, skipping", search_id=search.id)
            return
        self._pending.add(search.id)
        self._scan_ids[search.id] = self.scan_log.queued(search.chat_id, search.url, search.id)
        self._queue.put_nowait((context, search.id))
        logger.info("Scan queued", chat_id=search.chat_id, search_id=search.id, queue_length=self._queue.qsize())

    async def _wait_for_gap(self) -> None:
        async with self._gap_lock:
            if self._last_scan_end is not None:
                remaining = self.scan_gap_seconds - (time.monotonic() - self._last_scan_end)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_scan_end = time.monotonic()  # with several workers, staggers their starts

    async def _worker(self) -> None:
        while True:
            context, search_id = await self._queue.get()
            scan_id = self._scan_ids.get(search_id)
            try:
                search = self._current(search_id)
                if search is None:
                    self._log_cancelled(scan_id)
                    continue
                # One chat's searches scan one after another: they share its alert state.
                async with self._chat_lock(search.chat_id):
                    await self._wait_for_gap()
                    self._running.add(search_id)
                    search = self._current(search_id)
                    if search is None:
                        self._log_cancelled(scan_id)
                        continue
                    if scan_id is not None:
                        self.scan_log.started(scan_id)
                    try:
                        await self._run_scan(context, search)
                    finally:
                        self._last_scan_end = time.monotonic()
                        if search.interval_minutes == 0 and self.state.get_search(search.id) == search:
                            self.state.stop_search(search.id)
            except Exception as error:
                logger.error("scan worker failed", search_id=search_id, error=str(error))
                if scan_id is not None:
                    self.scan_log.failed(scan_id, f"Internal error: {error}")
            finally:
                self._scan_ids.pop(search_id, None)
                self._running.discard(search_id)
                self._pending.discard(search_id)
                self._queue.task_done()

    async def _run_scan(self, context: ContextTypes.DEFAULT_TYPE, search: Search) -> None:
        chat_id, url = search.chat_id, search.url
        plan = self.tracker.plan(search.id)
        scan_id = self._scan_ids.get(search.id)

        def progress(text: str) -> None:
            if scan_id is not None:
                try:
                    self.scan_log.progress(scan_id, text)
                except Exception as error:  # progress is best effort
                    logger.warning("Could not record scan progress", error=str(error))

        label = self._label(search)
        try:
            logger.info("Scan started", chat_id=chat_id, search_id=search.id, search_url=url)
            output = await asyncio.to_thread(_run_scraper_sync, url, plan, progress)
            logger.info("Scan completed", chat_id=chat_id, search_id=search.id, results_count=len(output.listings))
        except CustomException as error:
            logger.error(
                "Scan failed", chat_id=chat_id, search_id=search.id,
                error_code=int(error.error_code), reason=error.message,
            )
            self._log_failed(scan_id, error.message)
            await self._send_safe(context, chat_id, f"⚠️ Scan failed{label}: {error.message}")
            return
        except Exception as error:
            logger.error("Unexpected scan error", chat_id=chat_id, search_id=search.id, error=str(error))
            self._log_failed(scan_id, f"Unexpected error: {error}")
            await self._send_safe(context, chat_id, f"⚠️ Scan failed unexpectedly{label}: {error}")
            return

        if self._current(search.id) is None:
            # Keep what the scan learned about the ads, without alert state for this chat.
            self.state.ads.record(output.listings + output.left_search)
            self.state.ads.mark_removed(output.removed)
            self.on_scan_complete()
            self._log_cancelled(scan_id, "Search stopped during the scan")
            return

        result = self.tracker.apply(chat_id, search.id, output, search_url=url)
        if scan_id is not None:
            self.scan_log.finished(
                scan_id,
                results=len(output.listings),
                new_ads=len(result.new),
                price_drops=len(result.drops),
                removed=len(result.removed),
                details_fetched=output.details_fetched,
            )
        self.on_scan_complete()
        if result.new:
            await self._send_safe(context, chat_id, f"🚗 {len(result.new)} new car(s) found{label}")
            await self._send_listings(context, chat_id, [format_listing(item) for item in result.new])
        if result.drops:
            await self._send_safe(context, chat_id, f"📉 {len(result.drops)} price drop(s) found{label}")
            await self._send_listings(
                context,
                chat_id,
                [format_listing(item, old_price) for item, old_price in result.drops],
            )
        if search.interval_minutes == 0 and not result.new and not result.drops:
            await self._send_safe(context, chat_id, f"No new cars or price drops found{label}.")

    def _label(self, search: Search) -> str:
        """' in <search>' when the chat has several searches, so alerts say which one."""
        searches = self.state.chat_searches(search.chat_id)
        if len(searches) < 2:
            return ""
        return f" in {describe_search(search.url)}"

    def is_scanning(self) -> bool:
        return bool(self._running)

    def _log_cancelled(self, scan_id: int | None, reason: str = "Search stopped") -> None:
        if scan_id is not None:
            self.scan_log.cancelled(scan_id, reason)

    def _log_failed(self, scan_id: int | None, error: str) -> None:
        if scan_id is not None:
            self.scan_log.failed(scan_id, error)

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
