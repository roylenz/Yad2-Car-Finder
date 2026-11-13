"""The seller's phone number and name on a Yad2 ad page, read the way a buyer gets them: press
"הצגת מספר טלפון" (private ads) or "יצירת קשר" (dealer ads) with real mouse events (a scripted
click is ignored) and read the contact box that appears: the name as the seller wrote it, then
the number. A dealer's box lists its people (the first is taken, with the dealership's name)
above a lead form, which is left untouched.

Yad2 shows a forwarding number that relays the call to the seller, so keeping it is fine.
"""

import json
import re
import time
from dataclasses import dataclass

import nodriver as uc
from nodriver.core.connection import ProtocolException

SHOW_PHONE = "הצגת מספר טלפון"
DEALER_CONTACT = "יצירת קשר"
BUTTONS = (SHOW_PHONE, DEALER_CONTACT)
PHONE = re.compile(r"\b0(5\d|[2-489]|7\d)-?\d{7}\b")
DESKTOP = (1600, 1000)
CLICKS = 3

# Marks the visible button so it can be clicked like a person would, and returns the page text
# from before the click; "" while there is no such button.
_BUTTON_EXPRESSION = f"""(() => {{
    const labels = {list(BUTTONS)!r};
    const button = [...document.querySelectorAll("button")].find(b =>
        labels.some(label => b.innerText.includes(label))
        && b.getClientRects().length && b.offsetParent !== null);
    if (!button) return "";
    button.setAttribute("data-seller-phone", "1");
    button.scrollIntoView({{block: "center"}});
    return document.body.innerText || " ";
}})()"""


@dataclass(frozen=True)
class Contact:
    phone: str | None = None  # +972 form
    name: str | None = None


NONE = Contact()

# The contact box shown after the click: a private seller's, or the first of a dealer's people.
# Its first line is the name; its number is read from it too.
_CONTACT_EXPRESSION = """JSON.stringify((() => {
    const box = document.querySelector("[data-testid=private-contact-info]")
        || document.querySelector("[data-testid=agent-contact-info]")
        || document.querySelector("[data-testid=phone-number-link]")?.closest("li");
    if (!box) return {};
    const skip = /\\d{7}|sms|הודעה|צ׳אט|צ'אט|חיוג/i;
    const leaf = [...box.querySelectorAll("*")].find(e =>
        !e.children.length && e.textContent.trim() && !skip.test(e.textContent));
    const agency = box.matches("[data-testid=agent-contact-info]")
        ? document.querySelector("a[href*='yad2_source=showcontacts']")?.textContent.trim() : "";
    return {name: leaf ? leaf.textContent.trim() : "", text: box.innerText, agency: agency || ""};
})())"""


def _contact_from_box(raw: str, page_number: str | None) -> Contact:
    try:
        box = json.loads(raw or "{}")
    except ValueError:
        box = {}
    name = clean_name(box.get("name"))
    if name and box.get("agency"):
        name = clean_name(f"{name} ({box['agency']})")
    return Contact(find_number(box.get("text", "")) or page_number, name)


def clean_name(text: str | None) -> str | None:
    name = " ".join((text or "").split())
    return name[:60] or None


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


async def desktop_layout(tab: uc.Tab) -> None:
    """Lay the page out like a full desktop window, whatever size the window manager gives it:
    in a small (tiled) window Yad2 serves its mobile page, where the button doesn't respond."""
    await tab.send(uc.cdp.emulation.set_device_metrics_override(
        width=DESKTOP[0], height=DESKTOP[1], device_scale_factor=1, mobile=False))


async def reveal_in_new_tab(browser: uc.Browser, url: str, button_wait: float = 15,
                            reveal_wait: float = 12) -> tuple[Contact, uc.Tab]:
    """Open the ad in a new desktop-size tab and read the contact. The button only answers on the
    first ad page a tab shows, so every ad gets its own tab; the caller closes it."""
    tab = await browser.get("about:blank", new_tab=True)
    await desktop_layout(tab)
    await tab.get(url)
    return await reveal(tab, button_wait, reveal_wait), tab


async def has_button(tab: uc.Tab) -> bool:
    """Whether the loaded ad page offers "show number" (dealer and leasing ads may not)."""
    try:
        text = await tab.evaluate("document.body.innerText")
        return any(label in (text or "") for label in BUTTONS)
    except ProtocolException:
        return True  # unknown: try again later


async def reveal(tab: uc.Tab, button_wait: float = 10, reveal_wait: float = 12) -> Contact:
    """On a loaded ad page: press the button and return the number (+972 form) and the name.

    An empty Contact when the page has no button (within `button_wait` seconds) or no number
    appears; the name may be missing when the number isn't.
    """
    deadline = time.monotonic() + button_wait
    while True:
        try:
            before = await tab.evaluate(_BUTTON_EXPRESSION)
        except ProtocolException:
            before = ""
        if before:
            break
        if time.monotonic() > deadline:
            return NONE
        await tab.sleep(0.5)
    # A click before the page is interactive does nothing, so click again if no number shows.
    for click in range(CLICKS):
        await tab.sleep(1)
        try:
            button = await tab.select("button[data-seller-phone]", timeout=2)
            if click % 2 == 0:
                await button.mouse_click()
            else:
                await button.click()  # the element's own click, in case the mouse missed
        except Exception:
            pass  # the button is gone: the number may be showing already
        deadline = time.monotonic() + reveal_wait / CLICKS
        while time.monotonic() < deadline:
            await tab.sleep(0.5)
            number = find_number(await tab.evaluate("document.body.innerText"), before)
            if number:
                try:
                    return _contact_from_box(await tab.evaluate(_CONTACT_EXPRESSION), number)
                except ProtocolException:
                    return Contact(number)
    return NONE
