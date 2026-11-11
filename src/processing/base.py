"""Processor interface. A processor analyses an archived ad or photo and returns JSON.

To add a model (e.g. licence-plate recognition):
1. Subclass `Processor`, set `name`, `version` and `target` ("photo" or "ad"), implement `run`.
   Run heavy, blocking work with `asyncio.to_thread` so the bot stays responsive.
2. Register it in `processing/registry.py` and list its name in the `PROCESSORS` env var.
The runner processes every archived target once per processor version. Bump `version` to
re-run over the whole archive. Results appear on the dashboard's ad page.

Optional hooks:
- `requires`: photo processors whose results an ad processor needs. The ad waits until each of
  its photos has them, and gets them in `AdTarget.photo_results`.
- `active_only`: skip ads removed from Yad2 (for processors that cost money).
- `setup(engine)`: called once with the database engine.
- `apply(db, target_id, result)`: write a successful result into other tables, in the same
  transaction that saves it.
- Raise `Deferred` to leave the target pending (no attempt counted) and stop this pass.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from sqlalchemy import Connection, Engine

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
    # {photo sha256: {processor name: result}} for the processors in `requires`.
    photo_results: dict[str, dict[str, dict]] = field(default_factory=dict)


class Deferred(Exception):
    """The target cannot be processed now (e.g. budget spent); try again in a later pass."""


class Processor(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    target: ClassVar[Literal["photo", "ad"]]
    requires: ClassVar[tuple[str, ...]] = ()
    active_only: ClassVar[bool] = False

    def setup(self, engine: Engine) -> None:
        """Called once by the runner."""

    def apply(self, db: Connection, target_id: str, result: dict) -> None:
        """Write a successful result elsewhere (e.g. onto the ad row)."""

    @abstractmethod
    async def run(self, target: PhotoTarget | AdTarget) -> dict:
        """Return a JSON-serialisable result. Raise to record an error (retried a few times)."""
