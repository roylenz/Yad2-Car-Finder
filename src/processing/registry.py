"""Known processors. `PROCESSORS` (comma-separated names) picks which run.

By default all of them run. A processor whose requirements are missing (libraries, API key) is
skipped with a log line, as is any processor that needs its results.
"""

import os

from common.logger import logger
from processing.base import Processor
from processing.processors.claude_condition import ClaudeConditionProcessor
from processing.processors.photo_info import PhotoInfoProcessor
from processing.processors.plate_reader import PlateReaderProcessor
from processing.processors.plate_registry import PlateRegistryProcessor

AVAILABLE: dict[str, type[Processor]] = {
    processor.name: processor
    for processor in (
        PhotoInfoProcessor,
        PlateReaderProcessor,
        PlateRegistryProcessor,
        ClaudeConditionProcessor,
    )
}
DEFAULT = ",".join(AVAILABLE)


def enabled_processors(names: str | None = None) -> list[Processor]:
    names = (os.getenv("PROCESSORS") or DEFAULT) if names is None else names
    processors: list[Processor] = []
    for name in (part.strip() for part in names.split(",")):
        if not name:
            continue
        if name not in AVAILABLE:
            logger.warning("Unknown processor in PROCESSORS, skipping", processor=name)
            continue
        cls = AVAILABLE[name]
        missing = getattr(cls, "available", lambda: None)()
        if missing:
            logger.info("Processor disabled", processor=name, reason=missing)
            continue
        processors.append(cls())
    # Drop processors whose required photo processors are not running (they would wait forever),
    # and run photo processors first so ads find their results in the same pass.
    enabled = {processor.name for processor in processors}
    kept = []
    for processor in processors:
        absent = [name for name in processor.requires if name not in enabled]
        if absent:
            logger.info("Processor disabled", processor=processor.name, reason=f"needs {', '.join(absent)}")
            continue
        kept.append(processor)
    return sorted(kept, key=lambda processor: processor.target != "photo")
