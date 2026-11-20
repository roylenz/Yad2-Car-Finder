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
- Start Chrome through `Yad2CarFinder._start_browser`: nodriver waits under 3 s for Chrome to answer and the Pi is sometimes slower, so it tries 3 times and stops the unanswered Chrome nodriver leaves running (`stop_unconnected_browsers`)
- Stop Chrome with `await stop_browser(browser)`, never `browser.stop()` alone: nodriver deletes its temporary profile (`/tmp/uc_*`, ~180 MB) only at interpreter exit, so a long-running bot leaked one per scan and filled the Pi's disk. Leftovers are swept at startup and by the disk check (`remove_stale_profiles`)
- `tracking/disk_watch.py` checks the root and `DATA_DIR` filesystems every 30 min and messages the admins on Telegram at `DISK_ALERT_PERCENT` (default 85): again every 12 h or +5 points while high, all-clear 5 points below

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
- A seller replacing an ad's photos is a history change: `ad_snapshots.changes["photos"] = [removed, added]` (only when the ad already had photos), shown by `_change_summary` on the overview and ad page
- A chat has several searches (`searches.id`, `storage.searches.SearchStore`); scan jobs are named `scan_<search id>`. `chat_ads` is what a chat was told (one alert per ad across its searches); `search_ads` is what each search's results held. Stopping a search keeps the ad history; a chat left without searches forgets `chat_ads`
- A search can have extra Yad2 links and a name (both set on the dashboard's Scans page). A link is a `searches` row with `parent_id` set: scanned on its own with its own `search_ads` (so leaving one link's results is tracked per link), it copies and follows its search's interval, is stopped with it, and is left out of `chat_searches`, the picker and the Telegram search count (`chat_urls` covers links, for duplicate checks). The dashboard's search filter covers the search and its links (`queries._search_group`); `?search=<link id>` means its search. `Search.label` is the name, else `describe_search(url)`; alerts from a link say the search's label, and a named search's alerts always carry it
- Attributes that drift on their own (Yad2's `final_score` ranking, update/bump dates, ...) go in `_VOLATILE_ATTRIBUTES` in `storage/ads.py` so they are kept but not logged as changes
- The dashboard adds searches, changes their interval and stops them by writing `searches`; the bot's `sync_searches` job (every 30s) schedules, reschedules (interval differs from the job's trigger) or drops them and tells the chat about dashboard-added searches and interval changes
- The Scans page's "Edit filters" form changes the price, year, km and hand limits of a search's (or extra link's) Yad2 link (`search_url.RANGE_FILTERS`, `range_filters`/`with_ranges`, `SearchStore.set_url`); the rest of the link is kept. Scans read the link from `searches` each time, so the next scan uses it; `search_ads` is kept, so cars that no longer match are checked as ads that left the results and stay in the catalog. The bot's sync job tells the chat (`bot.edited_searches`)
- One scan at a time by default (`MAX_CONCURRENT_SCANS=1`), `SCAN_GAP_SECONDS` apart, and a chat's searches never scan in parallel
- Schema changes: add an Alembic migration in `src/storage/migrations/versions/`; keep SQL portable between SQLite and Postgres (use `storage.db.upsert`)
- Store times as naive UTC (`storage.db.utcnow()`)
- Models/analysis go in `src/processing/processors/` as `Processor` subclasses; never call them from the scan path
- Paid processors (Claude) must be `active_only`, record `usage.cost_usd` in their result and raise `Deferred` when `CLAUDE_MONTHLY_BUDGET_USD` is spent
- Claude prompts are cached (5 minutes) up to their stable part: `claude_condition` its instructions and answer format (photos differ per ad), the haggle brief its instructions and then the car's facts and research (the budget line comes last, so another budget for the same car reads the rest from the cache), the research its searches so far when a paused search resumes. Keep what changes per request after the last `cache_control` block. Cost is worked out in one place (`claude_condition.cost_usd`: cache writes 1.25x the input price, reads from `CACHE_READ_PRICES`)
- A plate is saved on an ad only once the registry confirms it is the same make/model (OCR errors read as real plates of other cars). Rejected plates keep their registry record and `plates.side_by_side` rows (shown on the ad page with differences highlighted). When Yad2 and the registry name a make differently, add it to `plates.MAKE_ALIASES` rather than loosening the match; changing what `plate_registry` stores means bumping its version (all ads are re-checked)
- Registry warnings (`plates.registry_warnings`): for a confirmed plate, `plate_registry` also reads data.gov.il's change history (`HISTORY_RESOURCE`: structural change, gas conversion, colour and tyre changes, origin, mileage at the last test) and open recalls (`RECALL_RESOURCE`), stored as `vehicle_history`, `recalls` and `warnings` in its result. Yad2 already gives the mileage at the last test; the registry's figure is only used to flag an ad claiming more than `KM_ROUNDING` (1,000 km) less, since sellers round down
- `plate_registry` keeps the registry's `tozeret_cd`/`degem_cd`/`sug_degem` (not displayed) to look up the importer's new-car list price (`plates.new_price`, `PRICE_RESOURCE`): the model year's price, else the nearest year's, marked `exact_year`. It is a list price, not what the first owner paid
- Ad processors (plate registry, repost matcher, Claude inspection) re-run on an ad that gained photos after their result was saved (`runner._finished` compares `photos.first_seen_at` with `analysis_results.created_at`), and `AdTarget.photos` holds only the ad's current photos when it has any (replaced ones, often Yad2 stock pictures, are left out)
- The haggle brief treats the registry contradicting the ad's history (more hands; lease, rental or company where the ad says private, including the ad's "original owner") as the strongest leverage, argued first
- Dealers (סוחר) are not hands: compare the ad's ownership and hand with the last non-dealer owner in the registry's ownership history (`plates.ownership_summary`); a first registration to a company for under 3 months is the importer, not a hand; time with dealers is reported separately
- Reposts of one car share `ads.car_id` (token of the group's earliest ad, set only through `storage.cars.link`, reasons in `ad_links`). Dashboard lists show the newest ad per car; days on market, price drops and history span the whole group
- The dashboard's `search` query parameter (header picker, carried in nav links and filter forms) is remembered in the `search` cookie (`chosen_search` in `dashboard/app.py`; empty value clears it, a stopped search falls back to all) and scopes overview, ads and market queries through `queries._in_search` (cars with any ad in that search's `search_ads`); the scans page uses it for history
- An ad page's "Market for <model>" section ignores the search picker: it covers every ad of the make and model (`queries.model_market`; peers fall back from same year and subtype to same year to ±1 year until there are 3). Its link opens `/market?ad=<token>`, which also ignores the picker (without touching the cookie) and marks that car
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
    seller_phone.py          # seller's number and name: ad in a fresh desktop-size tab (button answers only on a tab's first ad), real click on "הצגת מספר טלפון" (dealers: "יצירת קשר", first contact + dealership, form untouched), read the contact box's name and number
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
    background.py            # BackgroundPipeline: photos, processors, sale follow-up, then seller numbers; woken after each scan
    sale_watch.py            # removed ads with a plate: registry ownership checked ~12h for a week → sold / not_seen
    phone_collector.py       # ads missing the seller's number or name: opened between scans (15/round, 3 tries, 1 for ads with no contact button), saved to ads.seller_phone/seller_name; scans read it for new ads
    disk_watch.py            # disk usage alert to admins (DISK_ALERT_PERCENT) + leftover Chrome profile sweep
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
    brief.py                 # Claude negotiation brief built around the buyer's budget (offers never above it)
    call.py                  # CallAgent (text phone call, end_call tool) and SimulatedSeller
    phone.py                 # seller's number: the bot's saved one (ads.seller_phone), else read from the ad in Chrome
    research.py              # online research per model+year (saved, reused 30 days): new/0 km prices, used prices, known faults
    voice.py                 # real calls: ElevenLabs agent (Hebrew, Claude Sonnet, short turns, one question each) + imported Twilio number; brief as dynamic variables
    __main__.py              # CLI: list / brief / rehearse / call; output in DATA_DIR/haggle/<token>/
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
- Background work (photo downloads, processors) pauses while a scan runs (`BackgroundPipeline.busy = Scanner.is_scanning`): a CPU-starved browser or mid-scan image traffic also trips the bot check. The scanner wakes the pipeline only after `_running` is cleared, and a paused pipeline polls `busy()` every 5 s and resumes when the scan ends (never sleep the idle wait while paused). Plate models use `PLATE_THREADS` (default 2) cores
- Back-to-back scans from the same IP trip Yad2's bot check: on restart, restored searches keep their rhythm (first scan when the next regular one is due, `bot.scanner.first_delay`), never immediately
- Asking prices under 10% of the search's max price (`price=min-max`; else of the highest price in the scan) are placeholders (₪1) or monthly lease payments: archived, but not alerted and left out of market charts (`tracking.tracker.price_floor`)

## Deployment
- See `docs/DEPLOYMENT.md` (Raspberry Pi via SSH host `pi`, Docker Compose)
