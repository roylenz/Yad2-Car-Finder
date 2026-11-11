"""Turn a scan of one chat's search into catalog updates and the alerts that chat should get."""

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from urllib.parse import parse_qs, urlsplit

from car.listing import CarListing, ScanOutput, ScanPlan
from common.logger import logger
from storage.ads import AdRepository, is_badge_price

# A "drop" below these is almost certainly a parsing error, not a real price.
MIN_PLAUSIBLE_PRICE = 5000
MAX_PLAUSIBLE_DROP_RATIO = 0.5
# Prices under this share of the search's maximum are placeholders (₪1) or monthly lease
# payments, not real asking prices: they are archived but not alerted or charted.
IGNORED_PRICE_RATIO = 0.10


def search_max_price(search_url: str) -> int | None:
    """The upper bound of a Yad2 search's `price=min-max` filter, if it has one."""
    value = parse_qs(urlsplit(search_url).query).get("price", [""])[0]
    _, _, upper = value.partition("-")
    try:
        upper_price = int(upper)
    except ValueError:
        return None
    return upper_price if upper_price > 0 else None


def price_floor(max_price: int | None, prices: Iterable[int | None] = ()) -> int:
    """Prices below this are ignored: 10% of the search's maximum, else of the highest price."""
    base = max_price or max((price for price in prices if price), default=0)
    return int(base * IGNORED_PRICE_RATIO)


@dataclass
class ScanResult:
    new: list[CarListing] = field(default_factory=list)
    drops: list[tuple[CarListing, int]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def is_plausible_drop(old_price: int, new_price: int) -> bool:
    return new_price >= MIN_PLAUSIBLE_PRICE and new_price >= old_price * MAX_PLAUSIBLE_DROP_RATIO


def detect_changes(
    previous: dict[str, CarListing], current: list[CarListing]
) -> tuple[list[CarListing], list[tuple[CarListing, int]]]:
    new = []
    drops = []
    for listing in current:
        old = previous.get(listing.url)
        if old is None:
            new.append(listing)
        elif old.price is not None and listing.price is not None and listing.price < old.price:
            if is_badge_price(old.price, listing):
                continue  # the stored "price" was the old scraper's badge amount
            if not is_plausible_drop(old.price, listing.price):
                logger.warning(
                    "Ignoring implausible price drop",
                    url=listing.url,
                    old_price=old.price,
                    new_price=listing.price,
                )
                continue
            listing = replace(
                listing,
                km=listing.km if listing.km is not None else old.km,
                hand=listing.hand if listing.hand is not None else old.hand,
                year=listing.year if listing.year is not None else old.year,
                listed_at=listing.listed_at or old.listed_at,
            )
            drops.append((listing, old.price))
    return new, drops


class AdTracker:
    def __init__(self, ads: AdRepository) -> None:
        self.ads = ads

    def plan(self, chat_id: int) -> ScanPlan:
        return ScanPlan(known=self.ads.known_ads(), previous_urls=self.ads.chat_search_urls(chat_id))

    def apply(
        self,
        chat_id: int,
        output: ScanOutput,
        now: datetime.datetime | None = None,
        search_url: str | None = None,
    ) -> ScanResult:
        previous = self.ads.chat_listings(chat_id)
        self.ads.record(output.listings + output.left_search, now)
        self.ads.mark_removed(output.removed, now)
        new, drops = detect_changes(previous, output.listings)
        floor = price_floor(
            search_max_price(search_url) if search_url else None,
            (listing.price for listing in output.listings),
        )
        new = [listing for listing in new if listing.price is None or listing.price >= floor]
        drops = [(listing, old_price) for listing, old_price in drops if listing.price >= floor]
        # Feed results lack mileage; fill alert fields from the catalog (other searches may know).
        new = [self._complete(listing) for listing in new]
        drops = [(self._complete(listing), old_price) for listing, old_price in drops]
        self.ads.save_chat(
            chat_id,
            output.listings,
            [*output.removed, *(listing.url for listing in output.left_search)],
            now,
        )
        return ScanResult(new, drops, list(output.removed))

    def _complete(self, listing: CarListing) -> CarListing:
        stored = self.ads.get(listing.token)
        if stored is None:
            return listing
        return replace(
            listing,
            km=listing.km if listing.km is not None else stored.km,
            hand=listing.hand if listing.hand is not None else stored.hand,
            year=listing.year if listing.year is not None else stored.year,
            listed_at=listing.listed_at or stored.listed_at,
        )
