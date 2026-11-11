import datetime
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CarListing:
    url: str
    title: str
    price: int | None
    km: int | None = None
    hand: int | None = None
    year: int | None = None
    images: tuple[str, ...] = ()
    listed_at: datetime.datetime | None = None  # Yad2 creation time, naive UTC
    manufacturer: str | None = None
    model: str | None = None
    sub_model: str | None = None
    attributes: dict = field(default_factory=dict, compare=False)
    raw: dict | None = field(default=None, compare=False, repr=False)
    detailed: bool = False  # True when read from the ad's own page

    @property
    def token(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class KnownAd:
    """What the database already knows about an ad, used to plan a scan."""

    price: int | None
    details_fetched_at: datetime.datetime | None


@dataclass(frozen=True)
class ScanPlan:
    known: dict[str, KnownAd] = field(default_factory=dict)  # by URL, across all chats
    previous_urls: frozenset[str] = frozenset()  # in this search's results last time
    refresh_after: datetime.timedelta = datetime.timedelta(hours=24)
    max_refresh: int = 20


@dataclass(frozen=True)
class ScanOutput:
    listings: list[CarListing]  # ads in the search results
    removed: list[str] = field(default_factory=list)  # URLs Yad2 no longer serves
    left_search: list[CarListing] = field(default_factory=list)  # still live, out of results
