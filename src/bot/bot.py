from common.config import Configuration
from common.exceptions import CustomException, ErrorCode
from common.logger import logger
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram import Update

from bot.access import AccessControl
from bot.menu import configure_menu
from bot.handlers import (
    CONFIRMING,
    SELECTING_INTERVAL,
    SELECTING_MANUFACTURER,
    SELECTING_MODEL,
    SELECTING_YEAR_MAX,
    SELECTING_YEAR_MIN,
    confirm_callback,
    error_handler,
    interval_callback,
    manufacturer_callback,
    model_callback,
    search_entry,
    search_link_prompt,
    start_command,
    status_command,
    stop_callback,
    stop_command,
    url_search_message,
    year_max_callback,
    year_min_callback,
    scanner,
)
from bot.scanner import first_delay, schedule_scan
from car.search_url import describe_search
from tracking.background import BackgroundPipeline


async def restore_searches(app: Application) -> None:
    await configure_menu(app.bot, app.bot_data["access"].admin_ids())
    interrupted = scanner.scan_log.interrupt_unfinished()
    if interrupted:
        logger.info("Marked scans interrupted by the restart", count=interrupted)
    scanner.start_workers()
    pipeline = BackgroundPipeline(scanner.state.ads)
    scanner.on_scan_complete = pipeline.wake
    pipeline.busy = scanner.is_scanning
    pipeline.start()
    app.bot_data["pipeline"] = pipeline
    restored, _ = sync_searches(app.job_queue)
    logger.info("Restored saved searches", count=restored)
    # Searches added or stopped on the dashboard are picked up within a minute.
    app.job_queue.run_repeating(_sync_job, interval=30, first=30, name="sync_searches")


def sync_searches(job_queue) -> tuple[int, list]:
    """Schedule saved searches that have no scan job yet, reschedule ones whose interval changed
    (on the dashboard) and drop jobs of deleted searches. Returns (searches, rescheduled ones)."""
    saved = {search.id: search for search in scanner.state.all_searches() if scanner.access_check(search.chat_id)}
    scheduled: dict[int, float | None] = {}
    for job in job_queue.jobs():
        if job.name and job.name.startswith("scan_") and isinstance(job.data, int):
            if job.data in saved:
                scheduled[job.data] = _interval_seconds(job)
            else:
                job.schedule_removal()
    changed = []
    for search in saved.values():
        if search.interval_minutes <= 0:
            continue
        if search.id in scheduled:
            if scheduled[search.id] in (None, search.interval_minutes * 60):
                continue
            changed.append(search)
            logger.info("Search interval changed", search_id=search.id, minutes=search.interval_minutes)
        first = first_delay(scanner.scan_log.last_started(search.id), search.interval_minutes)
        schedule_scan(job_queue, scanner, search, first=first)
    return len(saved), changed


def _interval_seconds(job) -> float | None:
    """Seconds between a repeating job's runs (None for a one-time job)."""
    interval = getattr(getattr(job.job, "trigger", None), "interval", None)
    return interval.total_seconds() if interval is not None else None


async def _sync_job(context) -> None:
    for search in scanner.state.searches_added_on_dashboard():
        await context.bot.send_message(
            search.chat_id,
            f"✅ Search added on the dashboard: {describe_search(search.url)}. "
            f"I'll check it every {search.interval_minutes} minutes.",
        )
    _, changed = sync_searches(context.job_queue)
    for search in changed:
        await context.bot.send_message(
            search.chat_id,
            f"⏱ {describe_search(search.url)}: now checked every {search.interval_minutes} minutes.",
        )


async def stop_scan_workers(app: Application) -> None:
    await scanner.stop_workers()
    if "pipeline" in app.bot_data:
        await app.bot_data["pipeline"].stop()


def create_bot() -> Application:
    try:
        Configuration.Bot.validate()
    except CustomException:
        raise

    try:
        app = (
            Application.builder()
            .token(Configuration.Bot.TOKEN)
            .post_init(restore_searches)
            .post_shutdown(stop_scan_workers)
            .build()
        )
    except Exception as e:
        raise CustomException(
            f"Failed to create bot application: {e}", ErrorCode.BOT_START_FAILED
        ) from e

    url_handler = MessageHandler(filters.TEXT & filters.Regex(r"^https?://"), url_search_message)
    access = AccessControl(scanner)
    scanner.access_check = access.is_allowed
    app.bot_data["access"] = access

    search_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("search", search_entry),
            CallbackQueryHandler(search_entry, pattern="^new_search$"),
        ],
        states={
            SELECTING_MANUFACTURER: [
                CallbackQueryHandler(manufacturer_callback, pattern=r"^mfr_"),
                url_handler,
            ],
            SELECTING_MODEL: [
                CallbackQueryHandler(model_callback, pattern=r"^mdl_"),
                url_handler,
            ],
            SELECTING_YEAR_MIN: [
                CallbackQueryHandler(year_min_callback, pattern=r"^yr_min:|^yr_skip$"),
                url_handler,
            ],
            SELECTING_YEAR_MAX: [
                CallbackQueryHandler(year_max_callback, pattern=r"^yr_max:|^yr_skip$"),
                url_handler,
            ],
            SELECTING_INTERVAL: [
                CallbackQueryHandler(interval_callback, pattern=r"^interval:"),
                url_handler,
            ],
            CONFIRMING: [
                CallbackQueryHandler(confirm_callback, pattern=r"^confirm_"),
                url_handler,
            ],
        },
        fallbacks=[CommandHandler("stop", stop_command)],
        per_message=False,
    )

    app.add_handler(TypeHandler(Update, access.guard), group=-1)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", access.help_command))
    app.add_handler(search_conversation)
    app.add_handler(CallbackQueryHandler(access.help_command, pattern="^show_commands$"))
    app.add_handler(CallbackQueryHandler(search_link_prompt, pattern="^use_search_link$"))
    app.add_handler(CallbackQueryHandler(access.request_callback, pattern=r"^access_(approve|deny):[0-9]+$"))
    app.add_handler(url_handler)
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern=r"^stop_search:(all|[0-9]+)$"))
    app.add_handler(CommandHandler("status", status_command))
    for command in ("allow", "deny", "whitelist", "requests", "promote", "demote"):
        app.add_handler(CommandHandler(command, access.admin_command))
    app.add_error_handler(error_handler)

    logger.info("Bot handlers registered successfully")
    return app
