from typing import NamedTuple

from car.catalog import MANUFACTURERS, MANUFACTURER_BY_ID, model_name, models_for
from car.car_search_filter import CarSearchFilter
from common.logger import logger
from common.range import Range
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from bot.keyboards import (
    PAGE_SIZE,
    build_confirm_keyboard,
    build_interval_keyboard,
    build_manufacturer_keyboard,
    build_model_keyboard,
    build_start_keyboard,
    build_year_max_keyboard,
    build_year_min_keyboard,
)
from bot.scanner import Scanner

(
    SELECTING_MANUFACTURER,
    SELECTING_MODEL,
    SELECTING_YEAR_MIN,
    SELECTING_YEAR_MAX,
    SELECTING_INTERVAL,
    CONFIRMING,
) = range(6)

JOB_NAME_PREFIX = "scan_"

scanner = Scanner()


class TelegramUserInfo(NamedTuple):
    chat_id: int
    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language: str | None = None

    def to_log_dict(self) -> dict:
        return {k: v for k, v in self._asdict().items() if v is not None}

    @classmethod
    def from_update(cls, update: Update) -> "TelegramUserInfo":
        user = update.effective_user
        if not user:
            return cls(chat_id=update.effective_chat.id)
        return cls(
            chat_id=update.effective_chat.id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language=user.language_code,
        )


def _user_context(update: Update) -> dict:
    return TelegramUserInfo.from_update(update).to_log_dict()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.debug("/start command received", **_user_context(update))
    await update.message.reply_text(
        "🚗 *Yad2 Car Finder Bot*\n\n"
        "I'll help you find cars on Yad2 and notify you when new listings appear.\n\n"
        "Tap the button below to configure your search:",
        parse_mode="Markdown",
        reply_markup=build_start_keyboard(),
    )


async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for search configuration (from /search command or inline button)."""
    chat_id = update.effective_chat.id
    was_active = context.chat_data.get("scan_active", False)
    logger.debug("/search entry", **_user_context(update), previous_scan_active=was_active)

    if was_active:
        _remove_scan_jobs(context, chat_id)
        scanner.clear_seen_urls(chat_id)
        context.chat_data["scan_active"] = False
        logger.debug("Previous scan stopped for new search", **_user_context(update))
    else:
        _remove_scan_jobs(context, chat_id)
        context.chat_data["scan_active"] = False

    context.chat_data["selected_manufacturers"] = set()
    context.chat_data["selected_models"] = set()
    context.chat_data["year_min"] = None
    context.chat_data["year_max"] = None
    context.chat_data["scan_interval"] = 10

    text = "Select up to 4 manufacturers, then tap *Next*:"
    keyboard = build_manufacturer_keyboard(set())

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=keyboard
        )
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    return SELECTING_MANUFACTURER


async def manufacturer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    selected: set[int] = context.chat_data.get("selected_manufacturers", set())

    if data.startswith("mfr_page:"):
        await query.answer()
        page = int(data.split(":")[1])
        if 0 <= page < (len(MANUFACTURERS) + PAGE_SIZE - 1) // PAGE_SIZE:
            await query.edit_message_reply_markup(reply_markup=build_manufacturer_keyboard(selected, page))
        return SELECTING_MANUFACTURER

    if data == "mfr_count":
        await query.answer(f"{len(selected)} of 4 manufacturers selected")
        return SELECTING_MANUFACTURER

    if data.startswith("mfr_toggle:"):
        _, raw_value, raw_page = data.split(":")
        value, page = int(raw_value), int(raw_page)
        if value not in MANUFACTURER_BY_ID:
            await query.answer("Unknown manufacturer", show_alert=True)
            return SELECTING_MANUFACTURER
        if value not in selected and len(selected) >= 4:
            await query.answer("Yad2 allows up to 4 manufacturers", show_alert=True)
            return SELECTING_MANUFACTURER
        await query.answer()
        selected.symmetric_difference_update({value})
        context.chat_data["selected_manufacturers"] = selected
        await query.edit_message_reply_markup(reply_markup=build_manufacturer_keyboard(selected, page))
        return SELECTING_MANUFACTURER

    if data == "mfr_done":
        if not selected:
            await query.answer("Please select at least one manufacturer!", show_alert=True)
            return SELECTING_MANUFACTURER

        await query.answer()
        await query.edit_message_text(
            "Select up to 10 models, or tap *Skip* / *Next*:",
            parse_mode="Markdown",
            reply_markup=build_model_keyboard(set(), selected),
        )
        return SELECTING_MODEL

    await query.answer()
    return SELECTING_MANUFACTURER


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    selected: set[int] = context.chat_data.get("selected_models", set())
    manufacturer_ids = context.chat_data.get("selected_manufacturers", set())

    if data.startswith("mdl_page:"):
        await query.answer()
        page = int(data.split(":")[1])
        if 0 <= page < max(1, (len(models_for(manufacturer_ids)) + PAGE_SIZE - 1) // PAGE_SIZE):
            await query.edit_message_reply_markup(
                reply_markup=build_model_keyboard(selected, manufacturer_ids, page)
            )
        return SELECTING_MODEL

    if data == "mdl_count":
        await query.answer(f"{len(selected)} of 10 models selected")
        return SELECTING_MODEL

    if data.startswith("mdl_toggle:"):
        _, raw_value, raw_page = data.split(":")
        value, page = int(raw_value), int(raw_page)
        if value not in {model["id"] for model in models_for(manufacturer_ids)}:
            await query.answer("Unknown model", show_alert=True)
            return SELECTING_MODEL
        if value not in selected and len(selected) >= 10:
            await query.answer("Yad2 allows up to 10 models", show_alert=True)
            return SELECTING_MODEL
        await query.answer()
        selected.symmetric_difference_update({value})
        context.chat_data["selected_models"] = selected
        await query.edit_message_reply_markup(
            reply_markup=build_model_keyboard(selected, manufacturer_ids, page)
        )
        return SELECTING_MODEL

    if data in ("mdl_done", "mdl_skip"):
        await query.answer()
        if data == "mdl_skip":
            context.chat_data["selected_models"] = set()
        await query.edit_message_text(
            "Select minimum year, or tap *Skip*:",
            parse_mode="Markdown",
            reply_markup=build_year_min_keyboard(),
        )
        return SELECTING_YEAR_MIN

    await query.answer()
    return SELECTING_MODEL


async def year_min_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data

    if data == "yr_skip":
        await query.answer()
        await query.edit_message_text(
            "Select scan interval:",
            parse_mode="Markdown",
            reply_markup=build_interval_keyboard(),
        )
        return SELECTING_INTERVAL

    if data.startswith("yr_min:"):
        await query.answer()
        year = int(data.split(":")[1])
        context.chat_data["year_min"] = year
        await query.edit_message_text(
            f"Min year: *{year}*\nSelect maximum year, or tap *Skip*:",
            parse_mode="Markdown",
            reply_markup=build_year_max_keyboard(year),
        )
        return SELECTING_YEAR_MAX

    await query.answer()
    return SELECTING_YEAR_MIN


async def year_max_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("yr_max:"):
        context.chat_data["year_max"] = int(data.split(":")[1])

    await query.edit_message_text(
        "Select scan interval:",
        parse_mode="Markdown",
        reply_markup=build_interval_keyboard(),
    )
    return SELECTING_INTERVAL


async def interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data

    if data.startswith("interval:"):
        await query.answer()
        raw_value = data.split(":")[1]
        if raw_value == "once":
            context.chat_data["scan_interval"] = 0
        else:
            context.chat_data["scan_interval"] = int(raw_value)
        summary = _build_filter_summary(context.chat_data)
        await query.edit_message_text(
            f"*Search Configuration:*\n\n{summary}\n\nReady to start?",
            parse_mode="Markdown",
            reply_markup=build_confirm_keyboard(),
        )
        return CONFIRMING

    await query.answer()
    return SELECTING_INTERVAL


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data

    if data == "confirm_edit":
        return await search_entry(update, context)

    if data == "confirm_start":
        chat_id = update.effective_chat.id
        if not context.chat_data.get("selected_manufacturers"):
            await query.answer("No manufacturers selected! Please start over.", show_alert=True)
            logger.warning("Confirm without manufacturers", **_user_context(update))
            return CONFIRMING

        await query.answer()
        car_filter = _build_car_filter(context.chat_data)
        interval = context.chat_data.get("scan_interval", 10)
        logger.debug(
            "Scan confirmed",
            **_user_context(update),
            interval_minutes=interval,
            filters=car_filter.get_url_parameters(),
        )

        _remove_scan_jobs(context, chat_id)

        context.job_queue.run_once(
            scanner.scan_job,
            when=2,
            chat_id=chat_id,
            name=f"{JOB_NAME_PREFIX}{chat_id}_once",
            data=car_filter,
        )

        if interval > 0:
            context.job_queue.run_repeating(
                scanner.scan_job,
                interval=interval * 60,
                first=interval * 60,
                chat_id=chat_id,
                name=f"{JOB_NAME_PREFIX}{chat_id}",
                data=car_filter,
            )

        context.chat_data["active_filter"] = car_filter
        context.chat_data["scan_active"] = True

        if interval > 0:
            msg = (
                f"✅ Scanning started! I'll check every {interval} minutes.\n"
                f"Use /stop to stop scanning.\n"
                f"Use /status to check current configuration."
            )
        else:
            msg = (
                "✅ Running a one-time scan now...\n"
                "Use /status to check results."
            )

        await query.edit_message_text(msg)
        return ConversationHandler.END

    await query.answer()
    return CONFIRMING


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    is_active = context.chat_data.get("scan_active", False)
    logger.debug("/stop command received", **_user_context(update), was_active=is_active)

    if not is_active:
        await update.message.reply_text(
            "ℹ️ No active scan to stop. Use /search to configure one.",
            reply_markup=build_start_keyboard(),
        )
        return ConversationHandler.END

    _remove_scan_jobs(context, chat_id)
    context.chat_data["scan_active"] = False
    scanner.clear_seen_urls(chat_id)
    logger.debug("Scan stopped and seen URLs cleared", **_user_context(update))

    await update.message.reply_text(
        "⏹️ Scanning stopped. Use /search to configure a new search.",
        reply_markup=build_start_keyboard(),
    )
    return ConversationHandler.END


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_active = context.chat_data.get("scan_active", False)
    logger.debug("/status command received", **_user_context(update), scan_active=is_active)

    if not is_active:
        await update.message.reply_text(
            "No active scan. Use /search to configure one.",
            reply_markup=build_start_keyboard(),
        )
        return

    summary = _build_filter_summary(context.chat_data)
    seen_count = scanner.get_seen_count(chat_id)
    logger.debug("Status check", **_user_context(update), seen_count=seen_count)

    await update.message.reply_text(
        f"*Current Scan Status:*\n\n"
        f"🟢 Active\n"
        f"📊 {seen_count} unique cars found so far\n\n"
        f"{summary}",
        parse_mode="Markdown",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update", error=str(context.error))
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ An error occurred. Please try again with /search.",
            )
        except Exception:
            pass


def _remove_scan_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for suffix in ("", "_once"):
        for job in context.job_queue.get_jobs_by_name(f"{JOB_NAME_PREFIX}{chat_id}{suffix}"):
            job.schedule_removal()


def _build_car_filter(chat_data: dict) -> CarSearchFilter:
    manufacturers = sorted(chat_data.get("selected_manufacturers", set()))
    models = sorted(chat_data.get("selected_models", set()))

    year_min = chat_data.get("year_min")
    year_max = chat_data.get("year_max")
    year_range = None
    if year_min is not None or year_max is not None:
        year_range = Range(
            min=year_min if year_min is not None else Range.INFINITE,
            max=year_max if year_max is not None else Range.INFINITE,
        )

    return CarSearchFilter(
        manufacturers=manufacturers,
        models=models,
        year_range=year_range,
    )


def _build_filter_summary(chat_data: dict) -> str:
    lines = []

    mfr_values = chat_data.get("selected_manufacturers", set())
    if mfr_values:
        names = [MANUFACTURER_BY_ID[value]["name"] for value in sorted(mfr_values)]
        lines.append(f"🏭 Manufacturers: {', '.join(names)}")

    mdl_values = chat_data.get("selected_models", set())
    if mdl_values:
        names = [model_name(value, mfr_values) for value in sorted(mdl_values)]
        lines.append(f"🚙 Models: {', '.join(names)}")

    year_min = chat_data.get("year_min")
    year_max = chat_data.get("year_max")
    if year_min or year_max:
        year_str = f"{year_min or 'Any'} - {year_max or 'Any'}"
        lines.append(f"📅 Year: {year_str}")

    interval = chat_data.get("scan_interval", 10)
    if interval > 0:
        lines.append(f"⏱️ Scan every: {interval} minutes")
    else:
        lines.append("⏱️ Scan: Once")

    return "\n".join(lines)
