from datetime import datetime

from car.catalog import MANUFACTURERS, models_for
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CURRENT_YEAR = datetime.now().year
MIN_YEAR = 2000
PAGE_SIZE = 20


def build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Use a Yad2 search link", callback_data="use_search_link")],
            [InlineKeyboardButton("🔍 Choose filters here", callback_data="new_search")],
        ]
    )


def build_manufacturer_keyboard(selected: set[int], page: int = 0) -> InlineKeyboardMarkup:
    buttons = []
    page_count = (len(MANUFACTURERS) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, page_count - 1))
    for mfr in MANUFACTURERS[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        prefix = "✅ " if mfr["id"] in selected else ""
        name = mfr["name"]
        buttons.append(
            InlineKeyboardButton(f"{prefix}{name}", callback_data=f"mfr_toggle:{mfr['id']}:{page}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton("⬅️", callback_data=f"mfr_page:{page - 1}"),
            InlineKeyboardButton(
                f"{page + 1}/{page_count} · {len(selected)} selected", callback_data="mfr_count"
            ),
            InlineKeyboardButton("➡️", callback_data=f"mfr_page:{page + 1}"),
        ]
    )
    rows.append([InlineKeyboardButton("Next ➡️", callback_data="mfr_done")])
    return InlineKeyboardMarkup(rows)


def build_model_keyboard(
    selected: set[int], manufacturer_ids: set[int], page: int = 0
) -> InlineKeyboardMarkup:
    buttons = []
    models = models_for(manufacturer_ids)
    page_count = max(1, (len(models) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    for model in models[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        prefix = "✅ " if model["id"] in selected else ""
        name = model["name"]
        buttons.append(
            InlineKeyboardButton(f"{prefix}{name}", callback_data=f"mdl_toggle:{model['id']}:{page}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if page_count > 1:
        rows.append(
            [
                InlineKeyboardButton("⬅️", callback_data=f"mdl_page:{page - 1}"),
                InlineKeyboardButton(
                    f"{page + 1}/{page_count} · {len(selected)} selected", callback_data="mdl_count"
                ),
                InlineKeyboardButton("➡️", callback_data=f"mdl_page:{page + 1}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("Skip ⏭️", callback_data="mdl_skip"),
            InlineKeyboardButton("Next ➡️", callback_data="mdl_done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_year_min_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(str(year), callback_data=f"yr_min:{year}")
        for year in range(CURRENT_YEAR, MIN_YEAR - 1, -1)
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("Skip ⏭️", callback_data="yr_skip")])
    return InlineKeyboardMarkup(rows)


def build_year_max_keyboard(min_year: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(str(year), callback_data=f"yr_max:{year}")
        for year in range(CURRENT_YEAR, min_year - 1, -1)
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("Skip ⏭️", callback_data="yr_skip")])
    return InlineKeyboardMarkup(rows)


def build_interval_keyboard() -> InlineKeyboardMarkup:
    intervals = [5, 10, 15, 30, 60]
    buttons = [InlineKeyboardButton(f"{m} min", callback_data=f"interval:{m}") for m in intervals]
    once_button = InlineKeyboardButton("Once 🔂", callback_data="interval:once")
    return InlineKeyboardMarkup([buttons[:3], buttons[3:], [once_button]])


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 Start Scanning", callback_data="confirm_start"),
                InlineKeyboardButton("✏️ Edit", callback_data="confirm_edit"),
            ]
        ]
    )
