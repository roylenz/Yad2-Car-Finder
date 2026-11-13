"""The seller's phone number and name for a tracked ad.

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


def saved_contact(token: str, engine=None) -> seller_phone.Contact:
    """What the bot saved for the ad, else what an earlier lookup here found; no browser."""
    if engine is not None:
        try:
            with engine.connect() as db:
                row = db.execute(
                    select(ads.c.seller_phone, ads.c.seller_name).where(ads.c.token == token)
                ).first()
        except OperationalError:
            row = None  # a database copy from before these columns
        if row and row.seller_phone:
            return seller_phone.Contact(row.seller_phone, row.seller_name)
    path = _path(token)
    if path.exists():
        saved = json.loads(path.read_text())
        return seller_phone.Contact(saved.get("phone"), saved.get("name"))
    return seller_phone.NONE


def seller_contact(token: str, engine=None, refresh: bool = False) -> seller_phone.Contact:
    """The saved contact, else read from the ad now (and kept in seller.json)."""
    if not refresh:
        saved = saved_contact(token, engine)
        if saved.phone:
            return saved
    contact = asyncio.run(_reveal(token))
    path = _path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phone": contact.phone, "name": contact.name,
                                "found": time.strftime("%Y-%m-%d")}, ensure_ascii=False))
    return contact


def profile_dir() -> Path:
    """The tool's own Chrome profile, kept between lookups."""
    return data_dir() / "haggle" / "chrome-profile"


async def _reveal(token: str) -> seller_phone.Contact:
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
        contact = await seller_phone.reveal(tab, button_wait=PAGE_TIMEOUT)
        if contact.phone:
            return contact
        kind, state = await Yad2CarFinder._page_state(tab)
        if kind == "bot_check":
            raise PhoneError("Yad2 asked for browser validation; open the ad yourself")
        if "לא נמצא" in (state.get("text") or ""):
            raise PhoneError("The ad is no longer on Yad2")
        raise PhoneError("No number on the ad page")
    finally:
        browser.stop()
        await asyncio.sleep(0.5)  # let Chrome's pipes close before asyncio.run closes the loop
