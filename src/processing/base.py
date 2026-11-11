"""Processor interface. A processor analyses an archived ad or photo and returns JSON.

To add a model (e.g. licence-plate recognition):
1. Subclass `Processor`, set `name`, `version` and `target` ("photo" or "ad"), implement `run`.
   Run heavy, blocking work with `asyncio.to_thread` so the bot stays responsive.
2. Register it in `processing/registry.py` and list its name in the `PROCESSORS` env var.
The runner processes every archived target once per processor version. Bump `version` to
re-run over the whole archive. Results appear on the dashboard's ad page.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from car.listing import CarListing


@dataclass(frozen=True)
class PhotoTarget:
    sha256: str
    path: Path  # local file with the original image


@dataclass(frozen=True)
class AdTarget:
    token: str
    listing: CarListing
    photos: list[PhotoTarget]


class Processor(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    target: ClassVar[Literal["photo", "ad"]]

    @abstractmethod
    async def run(self, target: PhotoTarget | AdTarget) -> dict:
        """Return a JSON-serialisable result. Raise to record an error (retried a few times)."""
