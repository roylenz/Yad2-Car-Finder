"""The seller's phone number for a tracked ad, the way a buyer gets it: open the ad, press
"הצגת מספר טלפון" and read the number shown.

It uses its own Chrome profile (DATA_DIR/haggle/chrome-profile), so Yad2's cookies carry over
between lookups like a returning visitor's. No login is needed.

Yad2 shows a forwarding number (it relays the call to the seller), so keeping it is fine. It is
saved next to the brief (DATA_DIR/haggle/<token>/seller.json) and not looked up again.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import nodriver as uc

from car.car_finder import Yad2CarFinder
from storage.db import data_dir

SHOW_PHONE = "הצגת מספר טלפון"
PHONE = re.compile(r"\b0(5\d|[2-489]|7\d)-?\d{7}\b")
PAGE_TIMEOUT = 30
REVEAL_TIMEOUT = 10
DESKTOP = (1600, 1000)


class PhoneError(RuntimeError):
    pass


def international(number: str) -> str:
    """"055-4393473" -> "+972554393473"."""
    digits = re.sub(r"\D", "", number)
    return "+972" + digits[1:] if digits.startswith("0") else "+" + digits


def find_number(text: str, before: str = "") -> str | None:
    """The first number in `text` that wasn't on the page before the button was pressed."""
    seen = {international(match.group(0)) for match in PHONE.finditer(before or "")}
    for match in PHONE.finditer(text or ""):
        number = international(match.group(0))
        if number not in seen:
            return number
    return None


def _path(token: str):
    return data_dir() / "haggle" / token / "seller.json"


def seller_phone(token: str, refresh: bool = False) -> str:
    path = _path(token)
    if path.exists() and not refresh:
        return json.loads(path.read_text())["phone"]
    number = asyncio.run(_reveal(token))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phone": number, "found": time.strftime("%Y-%m-%d")}))
    return number


# Marks the visible button so it can be clicked like a person would, and returns the page text
# from before the click; "" while there is no such button yet.
_BUTTON_EXPRESSION = f"""(() => {{
    const button = [...document.querySelectorAll("button")].find(b =>
        b.innerText.includes("{SHOW_PHONE}") && b.getClientRects().length && b.offsetParent !== null);
    if (!button) return "";
    button.setAttribute("data-haggle-phone", "1");
    button.scrollIntoView({{block: "center"}});
    return document.body.innerText || " ";
}})()"""


def profile_dir() -> Path:
    """The tool's own Chrome profile, kept between lookups."""
    return data_dir() / "haggle" / "chrome-profile"


async def _browser():
    profile_dir().mkdir(parents=True, exist_ok=True)
    return await uc.start(
        user_data_dir=str(profile_dir()),
        browser_args=Yad2CarFinder._CHROME_ARGUMENTS,
        browser_executable_path=os.getenv("CHROME_PATH") or None,
        sandbox=os.getenv("CHROME_NO_SANDBOX", "false").lower() not in ("1", "true", "yes"),
    )


async def _desktop_size(tab) -> None:
    """Lay the page out like a full desktop window, whatever size the window manager gives it:
    a tiled window got Yad2's mobile page, where the button sits under the cookie notice."""
    await tab.send(uc.cdp.emulation.set_device_metrics_override(
        width=DESKTOP[0], height=DESKTOP[1], device_scale_factor=1, mobile=False))
    try:
        await tab.set_window_size(0, 0, *DESKTOP)
    except Exception:
        pass  # a tiling window manager may refuse; the layout above is what matters


async def _reveal(token: str) -> str:
    browser = await _browser()
    try:
        tab = await browser.get("about:blank")
        await _desktop_size(tab)
        await tab.get(f"https://www.yad2.co.il/vehicles/item/{token}")
        deadline = time.monotonic() + PAGE_TIMEOUT
        while not (before := await tab.evaluate(_BUTTON_EXPRESSION)):
            kind, state = await Yad2CarFinder._page_state(tab)
            if time.monotonic() > deadline:
                if kind == "bot_check":
                    raise PhoneError("Yad2 asked for browser validation; open the ad yourself")
                if "לא נמצא" in (state.get("text") or ""):
                    raise PhoneError("The ad is no longer on Yad2")
                raise PhoneError(f"No '{SHOW_PHONE}' button on the ad page")
            await tab.sleep(1)
        await tab.sleep(1)
        button = await tab.select("button[data-haggle-phone]")
        await button.mouse_click()  # real mouse events: the page ignores a scripted click
        deadline = time.monotonic() + REVEAL_TIMEOUT
        while time.monotonic() < deadline:
            await tab.sleep(1)
            number = find_number(await tab.evaluate("document.body.innerText"), before)
            if number:
                return number
        shot = _path(token).with_name("phone-failure.png")
        shot.parent.mkdir(parents=True, exist_ok=True)
        await tab.save_screenshot(str(shot))
        shot.with_suffix(".txt").write_text(await tab.evaluate(
            "location.href + '\\n' + innerWidth + 'x' + innerHeight + '\\n' + document.body.innerText"))
        raise PhoneError(f"Pressed the button but no number appeared (screenshot: {shot})")
    finally:
        browser.stop()
        await asyncio.sleep(0.5)  # let Chrome's pipes close before asyncio.run closes the loop
