"""Known processors. `PROCESSORS` (comma-separated names) picks which run; default photo_info."""

import os

from common.logger import logger
from processing.base import Processor
from processing.processors.photo_info import PhotoInfoProcessor

AVAILABLE: dict[str, type[Processor]] = {
    PhotoInfoProcessor.name: PhotoInfoProcessor,
}


def enabled_processors(names: str | None = None) -> list[Processor]:
    names = os.getenv("PROCESSORS", "photo_info") if names is None else names
    processors = []
    for name in (part.strip() for part in names.split(",")):
        if not name:
            continue
        if name not in AVAILABLE:
            logger.warning("Unknown processor in PROCESSORS, skipping", processor=name)
            continue
        processors.append(AVAILABLE[name]())
    return processors
