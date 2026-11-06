# Antigravity Agent Rules — Yad2 Car Finder

> These rules mirror `.cursorrules` and serve as the agent-readable project conventions.
> **When making changes, always update both `.cursorrules`, `.agents/rules.md`, and `README.md`
> to reflect the current state of the project.**

---

## Code Style
- Use single underscore `_` for private members and methods (not `__`)
- Format with **ruff**: double quotes, space indentation, 100 char line length
- Imports sorted by **isort** (stdlib → third-party → first-party)

## Error Handling
- Use custom exceptions with error codes (see `src/common/exceptions.py`)
- Every error code is an `IntEnum` in `ErrorCode`
- All custom exceptions extend `CustomException(message, error_code)`
- Raise specific exceptions instead of bare `except` or boolean error returns
- Log the exception before raising when appropriate

## Async Patterns
- **All** I/O operations must be async (browser automation, file writes, Telegram API)
- Use `nodriver` for browser automation (not Selenium)

- Use `python-telegram-bot` v22+ for Telegram bot (natively async)
- Use `asyncio.sleep()` instead of `time.sleep()`

## Logging
- Use `from common.logger import logger` — a `StructuredLogger` instance
- Log with kwargs: `logger.info("Scan started", chat_id=123, filters=[...])`
- Console output is colored via `colorlog`; file handler stays plain
- Log level via `Configuration.Logger.DEFAULT_LOG_LEVEL` (reads `LOG_LEVEL` env var, defaults `DEBUG`)
- Mute noisy third-party loggers in `LoggerSetup`
- Use `TelegramUserInfo.from_update(update)` for structured user context in bot handler logs

## Telegram Bot
- Entry point: `main.py` starts the bot via `Application.run_polling()`
- Interactive config via `ConversationHandler` with inline keyboards
- Periodic scanning via `JobQueue.run_repeating()`, or one-time scan (interval=0)
- State stored in `context.chat_data` (per-chat filter, scan status)
- Scanner tracks seen URLs per chat; first scan sends all, subsequent only new
- One-time scans auto-deactivate after completion
- Bot token loaded from `.env` via `python-dotenv`

## Project Structure
```
src/
  main.py                    # entry point — starts Telegram bot
  bot/
    bot.py                   # Application setup, handler registration
    handlers.py              # /start, /search, /stop, /status + ConversationHandler
    keyboards.py             # inline keyboard builders (manufacturers, models, years, intervals)
    scanner.py               # periodic scan logic, URL diffing, Telegram notifications
  car/
    car_finder.py            # browser automation (nodriver)
    car_search_filter.py     # search filter params & enums
    cars_link_retriever.py   # orchestrates finder
  common/
    config.py                # bot config (from .env), logger config, paths
    exceptions.py            # custom exceptions & error codes
    logger.py                # logging setup
    range.py                 # URL range helper
```

## Yad2 Domain Context
- Target site: `https://www.yad2.co.il/vehicles/cars`
- Feed items use `data-nagish` attributes (stable selectors)
- Results feed items: `a[data-nagish='private-item-link']`, `a[data-nagish='agent-item-link']`, `a[data-nagish='agent-item-no-footer-link']`
- Page load indicator: `a[data-nagish='feed-item-card-link']` (carousel/recommendations load first)
- Results feed requires scrolling to trigger lazy loading
- Filter out `spot=look_alike` items (similar cars, not exact matches)
- Pagination navbar: `nav[data-nagish='pagination-navbar']`
- Ad popup may appear in iframe with title "Modal Message" and close button class `bz-close-btn`
- Captcha may appear; detect timeout and wait for manual solve
- Keep a small delay between page loads (~1s) to avoid rate limiting

## Deployment
- See `docs/DEPLOYMENT.md` (Raspberry Pi via SSH host `pi`, Docker Compose)
