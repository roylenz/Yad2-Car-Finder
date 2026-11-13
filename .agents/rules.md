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
- Ads are stored once in a global catalog (`ads`, history in `ad_snapshots`, `photos`); `chat_ads` holds per-chat alert state. First scan of a search sends all, later scans only new ads and price drops
- A chat has several searches (`searches.id`, `storage.searches.SearchStore`); scan jobs are named `scan_<search id>`. `chat_ads` is what a chat was told (one alert per ad across its searches); `search_ads` is what each search's results held. Stopping a search keeps the ad history; a chat left without searches forgets `chat_ads`
- Attributes that drift on their own (Yad2's `final_score` ranking, update/bump dates, ...) go in `_VOLATILE_ATTRIBUTES` in `storage/ads.py` so they are kept but not logged as changes
- The dashboard adds searches, changes their interval and stops them by writing `searches`; the bot's `sync_searches` job (every 30s) schedules, reschedules (interval differs from the job's trigger) or drops them and tells the chat about dashboard-added searches and interval changes
- One scan at a time by default (`MAX_CONCURRENT_SCANS=1`), `SCAN_GAP_SECONDS` apart, and a chat's searches never scan in parallel
- Schema changes: add an Alembic migration in `src/storage/migrations/versions/`; keep SQL portable between SQLite and Postgres (use `storage.db.upsert`)
- Store times as naive UTC (`storage.db.utcnow()`)
- Models/analysis go in `src/processing/processors/` as `Processor` subclasses; never call them from the scan path
- Paid processors (Claude) must be `active_only`, record `usage.cost_usd` in their result and raise `Deferred` when `CLAUDE_MONTHLY_BUDGET_USD` is spent
- A plate is saved on an ad only once the registry confirms it is the same make/model (OCR errors read as real plates of other cars)
- Dealers (סוחר) are not hands: compare the ad's ownership and hand with the last non-dealer owner in the registry's ownership history (`plates.ownership_summary`); a first registration to a company for under 3 months is the importer, not a hand; time with dealers is reported separately
- Reposts of one car share `ads.car_id` (token of the group's earliest ad, set only through `storage.cars.link`, reasons in `ad_links`). Dashboard lists show the newest ad per car; days on market, price drops and history span the whole group
- The dashboard's `search` query parameter (header picker, carried in nav links and filter forms) is remembered in the `search` cookie (`chosen_search` in `dashboard/app.py`; empty value clears it, a stopped search falls back to all) and scopes overview, ads and market queries through `queries._in_search` (cars with any ad in that search's `search_ads`); the scans page uses it for history
- One-time scans auto-deactivate after completion
- Bot token loaded from `.env` via `python-dotenv`

## Project Structure
```
src/
  main.py                    # entry point — starts Telegram bot
  bot/
    bot.py                   # Application setup, handler registration, background pipeline start
    handlers.py              # /start, /search, /stop (pick a search), /status + ConversationHandler
    keyboards.py             # inline keyboard builders (manufacturers, models, years, intervals)
    scanner.py               # scan queue, alert formatting, Telegram notifications
    state.py                 # BotState: searches, access control, per-chat view of the catalog
  car/
    car_finder.py            # browser automation (nodriver): feed pages, item pages, removal checks
    yad2_json.py             # parse Yad2's __NEXT_DATA__ (feed + item) into CarListing
    listing.py               # CarListing, ScanPlan, ScanOutput
    car_search_filter.py     # search filter params & enums
  storage/
    schema.py                # SQLAlchemy tables (ads, ad_snapshots, photos, chat_ads, scans, analysis_results, ...)
    db.py                    # engine from DATABASE_URL / DATA_DIR, Alembic migrate(), upsert helper
    ads.py                   # AdRepository: catalog upserts, change history, photo rows, chat state
    scans.py                 # ScanLog: one row per scan (status, progress, counts) + next scan time
    background_status.py     # StatusBoard: live background pipeline state for the dashboard
    photos.py                # PhotoStore protocol + LocalPhotoStore (content-addressed by sha256)
    migrations/              # Alembic migrations (run automatically at startup)
  tracking/
    tracker.py               # AdTracker: scan plan, apply scan → new ads / price drops; detect_changes
    photo_archiver.py        # async httpx download of original photos
    background.py            # BackgroundPipeline: photos, processors, then sale follow-up; woken after each scan
    sale_watch.py            # removed ads with a plate: registry ownership checked ~12h for a week → sold / not_seen
  processing/
    base.py                  # Processor interface (target "photo" or "ad")
    registry.py              # available processors, PROCESSORS env var
    runner.py                # runs processors once per target and version
    plates.py                # plate voting/format, data.gov.il registry lookup, ad vs registry comparison
    processors/photo_info.py # sample processor
    processors/plate_reader.py   # local plate detection + OCR (open-image-models, fast-plate-ocr)
    processors/plate_registry.py # per-ad plate vote + registry check + ownership history; writes ads.plate, links same-plate ads
    processors/photo_hash.py     # perceptual dHash per photo
    processors/repost_matcher.py # links reposts by shared photos / similar description, vetoed by plate/year/hand/colour/km
    processors/claude_condition.py # Claude Sonnet damage/price-factor check, monthly budget
  dashboard/
    app.py                   # FastAPI app factory (create_app), pages, photo/thumbnail serving
    auth.py                  # Cloudflare Access JWT check (DASHBOARD_AUTH=off for local dev)
    fields.py                # labels/grouping of ad attributes and equipment for the ad page
    queries.py               # read-only dashboard queries
    templates/               # Jinja2 pages, Chart.js from jsdelivr
  haggle/                    # negotiation agent, tried from a PC for now (not run by the bot)
    facts.py                 # read-only facts for one car: price history, market, registry, condition
    brief.py                 # Claude negotiation brief (offers capped at the buyer's max)
    call.py                  # CallAgent (text phone call, end_call tool) and SimulatedSeller
    __main__.py              # CLI: list / brief / rehearse; output in DATA_DIR/haggle/<token>/
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
- Results feed requires scrolling to trigger lazy loading (DOM fallback only)
- Filter out `spot=look_alike` items (similar cars, not exact matches)
- Pagination navbar: `nav[data-nagish='pagination-navbar']`
- Ad popup may appear in iframe with title "Modal Message" and close button class `bz-close-btn`
- Captcha may appear; detect timeout and wait for manual solve
- Keep a small delay between page loads (~1s) to avoid rate limiting
- Prefer the page JSON (`<script id="__NEXT_DATA__">`, react-query `dehydratedState.queries`) over DOM text:
  - Search page: query `feed-mix` (groups `ads`, `platinum`, `boost`, `solo`) or, in an A/B variant, `feed` (`private`, `commercial`, `platinum`, `boost`, `solo`); promoted groups repeat regular ads, dedupe by `token`; `pagination.pages`
  - Item page: query `["vehicles", "item", token]` has `km`, colour, gearbox, test date, city, description, `dates.createdAt`; missing ad = page JSON without that query (HTTP 404)
  - Item page also has `kmInLastTest`, `lastTestDate`, `vehicleDates` (production month/year = "עלייה לכביש", `testDate` = test valid until), `isVerifiedByMt`, `specification` (equipment booleans, `airBags`, `safetyPoints` of 8, `pollutionLevel` of 15, `ignition` = drive), `powertrainArchitecture`, `carFamilyType`, `carTag`
  - `yad2_json.listing_from_json` maps known fields to snake_case `attributes`; unknown top-level fields are kept automatically; never store `customer` (seller identity)
  - `metaData.images` are originals on `img.yad2.co.il` (`/o/` path), downloadable with plain HTTP; pages themselves need the browser (bot protection)
  - Times are Israel local time; store naive UTC
- Results pages load through `Yad2CarFinder._open`: the page's real `location.href` tells a bot check (another host, given 30s to return to Yad2 by itself) from a network error (`chrome-error://`) or a timeout; a failed load is retried after 10s, 3 attempts in all, and each failure's screenshot and page state are kept in `DATA_DIR/scan-failures/`
- Don't page through results: Yad2 A/B-tests two feed layouts (`feed`: commercial+private; `feed-mix`: `ads`) that order ads differently, and serves page 2 in either, so `page=2` repeats some ads and skips others. A search with more than one page is split into price slices that each fit on one page (`Yad2CarFinder._collect_by_price`); a scan logs a warning when it collects fewer results than the feed's `pagination.total`
- Price-drop cards have two `[data-testid=price]` elements; the first is the "ירד ב ..." badge (DOM fallback skips it)
- Background work (photo downloads, processors) pauses while a scan runs (`BackgroundPipeline.busy = Scanner.is_scanning`): a CPU-starved browser or mid-scan image traffic also trips the bot check. Plate models use `PLATE_THREADS` (default 2) cores
- Back-to-back scans from the same IP trip Yad2's bot check: on restart, restored searches keep their rhythm (first scan when the next regular one is due, `bot.scanner.first_delay`), never immediately
- Asking prices under 10% of the search's max price (`price=min-max`; else of the highest price in the scan) are placeholders (₪1) or monthly lease payments: archived, but not alerted and left out of market charts (`tracking.tracker.price_floor`)

## Deployment
- See `docs/DEPLOYMENT.md` (Raspberry Pi via SSH host `pi`, Docker Compose)
