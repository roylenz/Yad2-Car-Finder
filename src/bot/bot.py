from common.config import Configuration
from common.exceptions import CustomException, ErrorCode
from common.logger import logger
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

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
    start_command,
    status_command,
    stop_command,
    url_search_message,
    year_max_callback,
    year_min_callback,
    scanner,
)
from bot.scanner import schedule_scan


async def restore_searches(app: Application) -> None:
    for chat_id, url, interval in scanner.state.active_searches():
        schedule_scan(app.job_queue, scanner, chat_id, url, interval, first=5)
    logger.info("Restored saved searches", count=len(scanner.state.active_searches()))


def create_bot() -> Application:
    try:
        Configuration.Bot.validate()
    except CustomException:
        raise

    try:
        app = Application.builder().token(Configuration.Bot.TOKEN).post_init(restore_searches).build()
    except Exception as e:
        raise CustomException(
            f"Failed to create bot application: {e}", ErrorCode.BOT_START_FAILED
        ) from e

    url_handler = MessageHandler(filters.TEXT & filters.Regex(r"^https?://"), url_search_message)

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

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(search_conversation)
    app.add_handler(url_handler)
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_error_handler(error_handler)

    logger.info("Bot handlers registered successfully")
    return app
