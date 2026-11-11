"""Yad2 manufacturer and model IDs used by the Telegram search flow."""

import json
from pathlib import Path

_data = json.loads(Path(__file__).with_name("manufacturers.json").read_text(encoding="utf-8"))
MANUFACTURERS = _data["manufacturers"]
MANUFACTURER_BY_ID = {item["id"]: item for item in MANUFACTURERS}


def models_for(manufacturer_ids: set[int]) -> list[dict]:
    models = {}
    for manufacturer_id in manufacturer_ids:
        manufacturer = MANUFACTURER_BY_ID.get(manufacturer_id)
        if manufacturer:
            for model in manufacturer["models"]:
                models[model["id"]] = model
    return sorted(models.values(), key=lambda model: model["name"].casefold())


def model_name(model_id: int, manufacturer_ids: set[int]) -> str:
    return next(
        (model["name"] for model in models_for(manufacturer_ids) if model["id"] == model_id),
        str(model_id),
    )


def _normal(text: str) -> str:
    return " ".join(text.replace("׳", "'").split()).casefold()


_MODEL_NAMES = {_normal(model["name"]) for item in MANUFACTURERS for model in item["models"]}


def split_title(title: str, known: dict[str, tuple[str, str]] | None = None) -> tuple[str, str] | None:
    """Split an old-style "<make> <model>" title (Hebrew make) into (manufacturer, model).

    `known` maps normalised "make model" titles to the spelling other ads already use. Otherwise
    the shortest leading words whose remainder is a catalog model name are the make.
    """
    if known and _normal(title) in known:
        return known[_normal(title)]
    words = title.split()
    for split in range(1, len(words)):
        if _normal(" ".join(words[split:])) in _MODEL_NAMES:
            return " ".join(words[:split]), " ".join(words[split:])
    return None
