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
    stop_command,
    url_search_message,
    year_max_callback,
    year_min_callback,
    scanner,
)
from bot.scanner import first_delay, schedule_scan
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
    for chat_id, url, interval in scanner.state.active_searches():
        if scanner.access_check(chat_id):
            first = first_delay(scanner.scan_log.last_started(chat_id), interval)
            schedule_scan(app.job_queue, scanner, chat_id, url, interval, first=first)
    logger.info("Restored saved searches", count=len(scanner.state.active_searches()))


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
    app.add_handler(CommandHandler("status", status_command))
    for command in ("allow", "deny", "whitelist", "requests", "promote", "demote"):
        app.add_handler(CommandHandler(command, access.admin_command))
    app.add_error_handler(error_handler)

    logger.info("Bot handlers registered successfully")
    return app
