"""Background work after scans: download new photos, then run processors on them."""

import asyncio
import datetime
from collections.abc import Callable

from common.logger import logger
from processing.registry import enabled_processors
from processing.runner import ProcessingRunner
from storage.ads import AdRepository
from storage.background_status import StatusBoard
from storage.db import utcnow
from storage.photos import LocalPhotoStore, PhotoStore
from tracking.photo_archiver import PhotoArchiver


class BackgroundPipeline:
    def __init__(
        self, ads: AdRepository, store: PhotoStore | None = None, idle_seconds: int = 600
    ) -> None:
        store = store or LocalPhotoStore()
        self.archiver = PhotoArchiver(ads, store)
        self.runner = ProcessingRunner(ads.engine, store, enabled_processors())
        self.idle_seconds = idle_seconds
        self.ads = ads
        self.status = StatusBoard(ads.engine)
        # Returns True while a scan runs: Yad2's bot check fails when the browser is starved
        # of CPU (plate models) or Yad2 sees photo downloads mid-scan, so work waits.
        self.busy: Callable[[], bool] = lambda: False
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="background-pipeline")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def run_once(self) -> None:
        downloaded = 0
        while not self.busy():
            waiting = self.ads.pending_photo_count()
            if not waiting:
                break
            self._report("downloading", "Downloading photos", downloaded, downloaded + waiting)
            batch = await self.archiver.run_pending()
            if not batch:
                break
            downloaded += batch
        while not self.busy() and await self.runner.run_pending(paused=self.busy, progress=self._progress):
            pass
        if self.busy():
            self._report("paused", "Waiting for the running scan to finish")

    def _progress(self, processor: str, done: int, waiting: int) -> None:
        self._report("processing", processor, done, waiting)

    def _report(self, state: str, detail: str | None = None, done: int | None = None,
                total: int | None = None, next_run_at: datetime.datetime | None = None) -> None:
        try:
            self.status.set(state, detail, done, total, next_run_at)
        except Exception as error:  # status is informational only
            logger.warning("Could not record background status", error=str(error))

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
                if not self.busy():
                    self._report(
                        "idle",
                        next_run_at=utcnow() + datetime.timedelta(seconds=self.idle_seconds),
                    )
            except Exception as error:
                logger.error("Background pipeline failed", error=str(error))
                self._report("error", str(error))
            try:
                await asyncio.wait_for(self._wake.wait(), self.idle_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
