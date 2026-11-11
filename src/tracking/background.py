"""Background work after scans: download new photos, then run processors on them."""

import asyncio
from collections.abc import Callable

from common.logger import logger
from processing.registry import enabled_processors
from processing.runner import ProcessingRunner
from storage.ads import AdRepository
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
        while not self.busy() and await self.archiver.run_pending():
            pass
        while not self.busy() and await self.runner.run_pending(paused=self.busy):
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as error:
                logger.error("Background pipeline failed", error=str(error))
            try:
                await asyncio.wait_for(self._wake.wait(), self.idle_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
