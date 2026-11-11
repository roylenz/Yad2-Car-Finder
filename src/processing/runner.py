"""Runs enabled processors over archived ads and photos that have no result for their version."""

from sqlalchemy import Engine, and_, exists, func, or_, select

from common.logger import logger
from processing.base import AdTarget, PhotoTarget, Processor
from storage.ads import AdRepository
from storage.db import upsert, utcnow
from storage.photos import PhotoStore
from storage.schema import ads, analysis_results, photos

MAX_ATTEMPTS = 3
MAX_DOWNLOAD_ATTEMPTS = 5


class ProcessingRunner:
    def __init__(self, engine: Engine, store: PhotoStore, processors: list[Processor]) -> None:
        self.engine = engine
        self.store = store
        self.processors = processors
        self.ads = AdRepository(engine)

    async def run_pending(self, limit: int = 50) -> int:
        """Process up to `limit` targets per processor; return how many results were saved."""
        done = 0
        for processor in self.processors:
            for target_id, target in self._pending(processor, limit):
                try:
                    result, error = await processor.run(target), None
                except Exception as exception:
                    result, error = None, f"{type(exception).__name__}: {exception}"
                    logger.warning(
                        "Processor failed", processor=processor.name, target=target_id, error=error
                    )
                self._save(processor, target_id, result, error)
                done += 1
        if done:
            logger.info("Processing pass finished", results=done)
        return done

    def _finished(self, processor: Processor, target_id_column):
        return exists().where(
            and_(
                analysis_results.c.target_type == processor.target,
                analysis_results.c.target_id == target_id_column,
                analysis_results.c.processor == processor.name,
                analysis_results.c.version == processor.version,
                or_(
                    analysis_results.c.status == "ok",
                    analysis_results.c.attempts >= MAX_ATTEMPTS,
                ),
            )
        )

    def _pending(self, processor: Processor, limit: int):
        with self.engine.connect() as db:
            if processor.target == "photo":
                rows = db.execute(
                    select(photos.c.sha256, func.min(photos.c.path).label("path"))
                    .where(photos.c.sha256.is_not(None), ~self._finished(processor, photos.c.sha256))
                    .group_by(photos.c.sha256)
                    .limit(limit)
                ).all()
                return [
                    (row.sha256, PhotoTarget(row.sha256, self.store.path(row.path))) for row in rows
                ]
            tokens = db.execute(
                select(ads.c.token)
                .where(
                    ~self._finished(processor, ads.c.token),
                    # Wait until the ad's photos are downloaded (or given up on).
                    ~exists().where(
                        photos.c.token == ads.c.token,
                        photos.c.sha256.is_(None),
                        photos.c.download_attempts < MAX_DOWNLOAD_ATTEMPTS,
                    ),
                )
                .order_by(ads.c.first_seen_at.desc())
                .limit(limit)
            ).scalars().all()
            targets = []
            for token in tokens:
                ad_photos = db.execute(
                    select(photos.c.sha256, photos.c.path)
                    .where(photos.c.token == token, photos.c.sha256.is_not(None))
                    .order_by(photos.c.position)
                ).all()
                targets.append(
                    (
                        token,
                        AdTarget(
                            token,
                            self.ads.get(token),
                            [PhotoTarget(row.sha256, self.store.path(row.path)) for row in ad_photos],
                        ),
                    )
                )
            return targets

    def _save(self, processor: Processor, target_id: str, result: dict | None, error: str | None):
        with self.engine.begin() as db:
            upsert(
                db,
                analysis_results,
                {
                    "target_type": processor.target,
                    "target_id": target_id,
                    "processor": processor.name,
                    "version": processor.version,
                    "status": "error" if error else "ok",
                    "result": result,
                    "error": error,
                    "attempts": 1,
                    "created_at": utcnow(),
                },
                ["target_type", "target_id", "processor", "version"],
                lambda excluded: {
                    "status": excluded.status,
                    "result": excluded.result,
                    "error": excluded.error,
                    "created_at": excluded.created_at,
                    "attempts": analysis_results.c.attempts + 1,
                },
            )
