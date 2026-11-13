"""The seller's phone number for a tracked ad.

The bot saves it in the database (ads.seller_phone) when it reads an ad and in the background;
this uses that, and otherwise opens the ad in its own Chrome and reads it the way a buyer does
(car/seller_phone.py). A number found here is kept in DATA_DIR/haggle/<token>/seller.json: the
haggle tools never write to the database.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import nodriver as uc
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from car import seller_phone
from car.car_finder import Yad2CarFinder
from storage.db import data_dir
from storage.schema import ads

PAGE_TIMEOUT = 30


class PhoneError(RuntimeError):
    pass


def _path(token: str) -> Path:
    return data_dir() / "haggle" / token / "seller.json"


def seller_phone_for(token: str, engine=None, refresh: bool = False) -> str:
    if engine is not None and not refresh:
        try:
            with engine.connect() as db:
                saved = db.execute(select(ads.c.seller_phone).where(ads.c.token == token)).scalar()
        except OperationalError:
            saved = None  # a database copy from before the seller_phone column
        if saved:
            return saved
    path = _path(token)
    if path.exists() and not refresh:
        return json.loads(path.read_text())["phone"]
    number = asyncio.run(_reveal(token))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phone": number, "found": time.strftime("%Y-%m-%d")}))
    return number


def profile_dir() -> Path:
    """The tool's own Chrome profile, kept between lookups."""
    return data_dir() / "haggle" / "chrome-profile"


async def _reveal(token: str) -> str:
    profile_dir().mkdir(parents=True, exist_ok=True)
    browser = await uc.start(
        user_data_dir=str(profile_dir()),
        browser_args=Yad2CarFinder._CHROME_ARGUMENTS,
        browser_executable_path=os.getenv("CHROME_PATH") or None,
        sandbox=os.getenv("CHROME_NO_SANDBOX", "false").lower() not in ("1", "true", "yes"),
    )
    try:
        tab = await browser.get("about:blank")
        await seller_phone.desktop_layout(tab)
        await tab.get(f"https://www.yad2.co.il/vehicles/item/{token}")
        number = await seller_phone.reveal(tab, button_wait=PAGE_TIMEOUT)
        if number:
            return number
        kind, state = await Yad2CarFinder._page_state(tab)
        if kind == "bot_check":
            raise PhoneError("Yad2 asked for browser validation; open the ad yourself")
        if "לא נמצא" in (state.get("text") or ""):
            raise PhoneError("The ad is no longer on Yad2")
        raise PhoneError("No number on the ad page")
    finally:
        browser.stop()
        await asyncio.sleep(0.5)  # let Chrome's pipes close before asyncio.run closes the loop
