"""Download ad photos (original files from Yad2's image CDN) into the photo store."""

import asyncio
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from common.logger import logger
from storage.ads import AdRepository
from storage.photos import PhotoStore

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"


class PhotoArchiver:
    def __init__(
        self, ads: AdRepository, store: PhotoStore, concurrency: int = 3, delay: float = 0.3
    ) -> None:
        self.ads = ads
        self.store = store
        self.concurrency = concurrency
        self.delay = delay

    async def run_pending(self, limit: int = 200, client: httpx.AsyncClient | None = None) -> int:
        """Download up to `limit` photos not stored yet; return how many were stored."""
        pending = self.ads.pending_photos(limit)
        if not pending:
            return 0
        semaphore = asyncio.Semaphore(self.concurrency)
        owns_client = client is None
        client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        )
        try:
            results = await asyncio.gather(
                *(self._download(client, semaphore, photo_id, url) for photo_id, url in pending)
            )
        finally:
            if owns_client:
                await client.aclose()
        stored = sum(results)
        logger.info("Photos archived", stored=stored, attempted=len(pending))
        return stored

    async def _download(
        self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, photo_id: int, url: str
    ) -> bool:
        async with semaphore:
            try:
                response = await client.get(url)
                content_type = response.headers.get("content-type", "").split(";")[0]
                if response.status_code != 200 or not content_type.startswith("image/"):
                    raise ValueError(f"HTTP {response.status_code} {content_type}")
                extension = _EXTENSIONS.get(content_type) or (
                    PurePosixPath(urlsplit(url).path).suffix.lower() or ".jpg"
                )
                sha256, key = await self.store.save(response.content, extension)
                self.ads.photo_stored(photo_id, sha256, key, len(response.content))
                return True
            except Exception as error:
                logger.warning("Photo download failed", url=url, error=str(error))
                self.ads.photo_failed(photo_id)
                return False
            finally:
                await asyncio.sleep(self.delay)
