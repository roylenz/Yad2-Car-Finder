from __future__ import annotations

from enum import IntEnum, StrEnum

from common.range import Range


class CarSearchFilter:
    class GearBox(IntEnum):
        MANUAL = 101
        AUTO = 102

    class Area(IntEnum):
        CENTER = 2
        SHARON = 19
        NORTH = 25
        SHFELA = 41
        SOUTH = 43
        JEHUDA = 75
        JERUSALEM = 100
        HADERA = 101

    class UrlParameterName(StrEnum):
        MANUFACTURER = "manufacturer"
        MODEL = "model"
        GEAR_BOX = "gearBox"
        YEAR = "year"
        HAND = "hand"
        KILOMETRAGE = "km"
        PRICE = "price"
        AREA = "topArea"
        PRICE_ONLY = "priceOnly"
        IMAGE_ONLY = "imgOnly"

    @staticmethod
    def get_selections_as_string(selections: list[int | IntEnum]) -> str:
        return ",".join(str(int(selection)) for selection in selections)

    def __init__(
        self,
        manufacturers: list[int] | None = None,
        models: list[int] | None = None,
        gear_boxes: list[GearBox] | None = None,
        year_range: Range | None = None,
        hand_range: Range | None = None,
        kilometrage_range: Range | None = None,
        price_range: Range | None = None,
        areas: list[Area] | None = None,
        only_with_price: bool = False,
        only_with_image: bool = False,
    ) -> None:
        self.manufacturers = manufacturers or []
        self.models = models or []
        self.gear_boxes = gear_boxes or []
        self.year_range = year_range or Range()
        self.hand_range = hand_range or Range()
        self.kilometrage_range = kilometrage_range or Range()
        self.price_range = price_range or Range()
        self.areas = areas or []
        self.only_with_price = only_with_price
        self.only_with_image = only_with_image

    def get_url_parameters(self) -> list[str]:
        url_parameters = []

        if len(self.manufacturers) > 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.MANUFACTURER}="
                f"{CarSearchFilter.get_selections_as_string(self.manufacturers)}"
            )

        if len(self.models) > 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.MODEL}="
                f"{CarSearchFilter.get_selections_as_string(self.models)}"
            )

        if len(self.gear_boxes) > 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.GEAR_BOX}="
                f"{CarSearchFilter.get_selections_as_string(self.gear_boxes)}"
            )

        if self.year_range.get_min() >= 0 or self.year_range.get_max() >= 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.YEAR}="
                f"{self.year_range.get_min()}-{self.year_range.get_max()}"
            )

        if self.hand_range.get_min() >= 0 or self.hand_range.get_max() >= 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.HAND}="
                f"{self.hand_range.get_min()}-{self.hand_range.get_max()}"
            )

        if self.kilometrage_range.get_min() >= 0 or self.kilometrage_range.get_max() >= 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.KILOMETRAGE}="
                f"{self.kilometrage_range.get_min()}-{self.kilometrage_range.get_max()}"
            )

        if self.price_range.get_min() >= 0 or self.price_range.get_max() >= 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.PRICE}="
                f"{self.price_range.get_min()}-{self.price_range.get_max()}"
            )

        if len(self.areas) > 0:
            url_parameters.append(
                f"{CarSearchFilter.UrlParameterName.AREA}="
                f"{CarSearchFilter.get_selections_as_string(self.areas)}"
            )

        if self.only_with_price:
            url_parameters.append(f"{CarSearchFilter.UrlParameterName.PRICE_ONLY}=1")

        if self.only_with_image:
            url_parameters.append(f"{CarSearchFilter.UrlParameterName.IMAGE_ONLY}=1")

        return url_parameters
