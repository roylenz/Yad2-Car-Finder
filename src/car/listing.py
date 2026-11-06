from dataclasses import dataclass


@dataclass(frozen=True)
class CarListing:
    url: str
    title: str
    price: int | None
    km: int | None = None
    hand: int | None = None
