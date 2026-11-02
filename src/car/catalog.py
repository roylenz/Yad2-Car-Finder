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
