# Yad2 Car Finder 🚗 
Telegram bot that scrapes [Yad2](https://www.yad2.co.il/vehicles/cars) for car listings based on your filters and sends you notifications when new cars appear

## Features
- **Interactive configuration** - configure manufacturer, model, year range via inline keyboard buttons in Telegram
- **Periodic scanning** - automatically checks for new listings every X minutes (configurable: 5/10/15/30/60) or run a one-time scan
- **Search URL alerts** - send a filtered Yad2 car search URL directly to the bot; it checks every 10 minutes
- **New car and price drop alerts** - messages include the listing year, price, mileage, hand (number of previous owners), how long the ad has been listed, and links to Yad2 and the ad's dashboard page
- **Ad archive** - every ad any search finds is kept with every detail Yad2 has: mileage, mileage at the last test, last test date, test validity, when it went on the road, ownership history, which fields the Ministry of Transport verified, engine and powertrain, safety and pollution ratings, full equipment list, city, description and more (new Yad2 fields are kept automatically), its full price and change history, when Yad2 posted it, and when it was removed. Stopping a search keeps the history
- **Photo archive** - all photos of every ad are downloaded at Yad2's original size and kept after the ad is gone (stored once even if re-posted)
- **Accurate price drops** - prices come from the JSON Yad2 embeds in its pages, not from card text, so a "dropped by ₪3,000" badge is never mistaken for the price. Implausible drops (over 50% or under ₪5,000) are logged and not alerted. Prices under 10% of the search's maximum price (₪1 placeholders, monthly lease prices) are archived but not alerted or charted, and are marked "Ignored price" in the ad list
- **Dashboard** - web pages for an overview, an ad list you can filter by make, model and subtype and sort by model, each ad's photos, details, price chart and change log, and a market view that plots every subtype of a model (or every model) by price plus a price-vs-mileage/year chart; a scans page with each search's next scan time, the running scan's progress and every past scan's result; a background page with what the bot is processing right now (photo downloads, plate reading, registry checks, Claude checks) and its progress, each processor's backlog and failures, Claude spend against the monthly budget, and recent results and errors; public through Cloudflare Tunnel with Cloudflare Access login
- **Car number per ad** - licence plates are read from the photos on the Pi (local ONNX models, no network) and checked against the Ministry of Transport's vehicle registry on data.gov.il. A confirmed plate is saved on the ad, so other ads of the same car are linked and you can search by plate. The ad page shows the registry record (exact trim, on-road month, last test, ownership, colour, VIN) and where it disagrees with the ad (wrong year, wrong hand or ownership, "private" seller whose car a dealer now owns, ...). Dealers are not counted as hands: the ad is compared with the last owner before the dealers
- **Owners and dealer time** - the registry's ownership history (month and owner type) is shown per ad, with how long the car has been with dealers and how many dealers it passed through in a row, which is leverage for a lower offer
- **Reposts merged** - when a seller takes an ad down and uploads it again, the ads are linked as one car: same confirmed licence plate, 3+ matching photos (perceptual hash), or a matching photo plus a similar description (different plate, year, hand, colour or mileage vetoes a match). Lists show one row per car marked "Reposted ×N", and days on the market, price drops and the change log cover all of its ads
- **Sale follow-up** - for a week after an ad with a confirmed plate is removed, the registry's ownership history is checked about every 12 hours; a new owner from the month of removal on marks the car as sold (shown on the ad and the Background page)
- **Sorting** - newest on the market (by the car's first ad), longest listed, biggest price drop (₪ from the first asking price) and most price drops, besides price, mileage, year and model
- **Condition check with Claude** - when an Anthropic API key is set, Claude Sonnet looks at each active ad's photos and description once and lists visible damage and anything that could lower the price (repairs, wear, warning lights, odometer vs advertised km, photos that do not match the ad), with a monthly spending cap
- **Pluggable processors** - a small interface for running models over archived ads and photos; results show on the dashboard
- **Persistent tracking** - searches, alert state and the archive survive container restarts
- **Every result, not just the first page** - Yad2 serves result pages in two layouts that order ads differently, so paging skips some ads. A search with more results than fit on one page is split into price ranges that each fit on one page, and the scan warns if it still collects fewer ads than Yad2 reports
- **Retries and failure evidence** - a results page that fails to load is retried after 10 seconds (3 attempts). The bot tells Yad2's bot check apart from a network error or a timeout, waits up to 30 seconds for a bot check to clear by itself, and keeps a screenshot of each failure in `data/scan-failures/`
- **Several searches per chat** - each chat can track up to `MAX_SEARCHES_PER_CHAT` searches (default 5), each on its own schedule. An ad that matches two of a chat's searches alerts once, and alerts say which search found them when a chat has several
- **Searches from the dashboard** - the Scans page adds a search (paste a Yad2 link, pick the chat and interval), changes how often a search is checked, or stops one; the bot picks it up within a minute and tells the chat
- **Dashboard by search** - the search picker in the dashboard header limits the overview, ads, market and scans pages to one search's cars (a reposted car counts if any of its ads was found); the choice is remembered (a cookie) on every tab until you pick "All searches"
- **One scan at a time** - a shared queue runs at most `MAX_CONCURRENT_SCANS` browser scans at once (default 1), at least `SCAN_GAP_SECONDS` apart (default 45); back-to-back page loads trip Yad2's bot check
- **Private access** - users request access with their name; admins approve requests and can promote other admins
- **Structured logging** - colored console output with structured JSON kwargs, configurable log level via `LOG_LEVEL` env var
- **Bot commands** - `/start`, `/search`, `/stop` (pick which search), `/status` (all of the chat's searches)
- **Command menu** - tap Telegram's Menu button to see commands; admins also see access controls

The manufacturer picker includes all 127 makes shown in Yad2's car search filter on 2 November 2026. Model IDs come from a [Yad2 API catalog snapshot](https://github.com/SRaz-Sw/Local-Whisper-Diarization/blob/main/nextjs-v1/public/data/yad2-mappings.json) dated 20 August 2025, with current models for the five newer makes when Yad2 offered them. The model picker shows models for the selected makes. Yad2 may add makes or models after these snapshots.

## Setup

### Docker Compose (recommended for Raspberry Pi)

Install Docker and the Compose plugin, then create a private `.env` file from
`.env.example` and set `TELEGRAM_BOT_TOKEN` to your BotFather token. Optionally
set `TELEGRAM_ADMIN_IDS` to comma-separated Telegram user IDs. If it is empty on
a fresh installation, the first person to message the bot becomes an admin.
Keep the token out of Git.

```bash
cp .env.example .env
chmod 600 .env
docker compose up -d --build
docker compose logs -f bot
```

If your account cannot access the Docker socket, prefix the `docker compose`
commands with `sudo`.

The image includes Chromium and runs it on a virtual X display as a non-root user. It works
on ARM64 Raspberry Pi OS and x86-64 Linux. The bot uses Telegram polling, so no
ports need to be published. Only run one instance for a given token. Docker Compose
stores the database and photos in the `bot_data` volume, or in the host folder set by
`HOST_DATA_DIR` (use an SSD on a Raspberry Pi).
Each chat has its own saved searches and alert state; the ad archive is shared, so an
ad found by two searches is stored once. Alerts are sent only to the chat that owns the search, and
once per ad even when several of the chat's searches find it. When several scans become due together,
they wait in a FIFO queue; repeated triggers for the same search do not pile up. A chat's searches
scan one after another.
Set `MAX_CONCURRENT_SCANS` in `.env` to adjust the number of browser scans allowed
at once, and `SCAN_GAP_SECONDS` for the pause between scans. `/status` shows whether each search is
queued, running, or waiting.

To update, pull the latest changes and run `docker compose up -d --build` again.
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full Raspberry Pi deployment steps.

### Direct Python installation

### Prerequisites

- Python 3.12+
- Google Chrome installed
- A Telegram bot token (get one from [@BotFather](https://t.me/BotFather))

### Installation

```bash
pip install -r requirements.txt
```

### Configuration

Create `.env` file and set your Telegram bot token:

```
TELEGRAM_BOT_TOKEN=your-token-here
TELEGRAM_ADMIN_IDS=
TELEGRAM_WHITELIST_IDS=
```

### Running

```bash
python3 src/main.py
```

Then open your bot in Telegram and send `/start`

New users send `/request Their Name`. Each admin receives an Approve/Deny button
and can also use `/requests` to see pending requests and `/allow <user ID>` to
approve one. Admins can use `/deny <user ID>` to remove access, `/promote <user ID>`
and `/demote <user ID>` to manage admins, and `/whitelist` to list access. `/id`
shows a person's Telegram user ID. Users approved by an admin can use the bot.
Access decisions and promoted admins survive restarts. `TELEGRAM_ADMIN_IDS` and
`TELEGRAM_WHITELIST_IDS` in `.env` are optional initial lists. Users listed as
admins in `.env` remain admins until removed from that file.

You can also send a filtered Yad2 car search URL directly, for example:

```text
https://www.yad2.co.il/vehicles/cars?manufacturer=334&model=13190&year=2023--1&price=-1-90000&km=-1-50000
```

The bot uses the URL's filters as sent, starts a scan, and checks again every 10
minutes. `/status` lists the chat's searches; `/stop` stops one of them (or all). A new URL
is added next to the chat's other searches. If Yad2 does not list a year, price,
mileage, or hand for a car, the alert says "Not listed" for that field.

## Usage

1. Tap **Menu** next to the message box to see the available commands, or tap **Commands** after `/start`
2. `/start` - welcome message with search link and filter buttons (or access instructions for new users)
3. `/search` - add a search (manufacturer, model, year range, scan interval); your other searches keep running
4. `/stop` - stop a search (pick which one when you have several, or stop all)
5. `/status` - list your searches, how many cars each found and when each is checked next

## Ad archive and dashboard

Each scan reads the search results from the JSON in Yad2's page (price, photos, posting time,
model, year, hand). It opens an ad's own page for new ads, ads whose price changed, ads not
refreshed in 24 hours (up to 20 per scan), and ads that left the results. That page adds
mileage and the remaining parameters, and shows whether Yad2 removed the ad. A history row is
written only when something changes. After each scan, new photos are downloaded and the
enabled processors run in the background. Background work pauses while a scan runs, so the
browser has the Pi's CPU (Yad2's bot check fails on a slow browser).

Every scan is logged with its status (queued, running, ok, failed, cancelled, or interrupted
by a restart), live progress, duration, result counts and error. The dashboard's Scans page
shows this history and when each search scans next; a scan that is still running when the
next one is due makes that one skip. After a restart, each search scans when its next regular
scan is due rather than straight away, since back-to-back scans trip Yad2's bot check.

| Setting (`.env`) | Purpose |
| --- | --- |
| `HOST_DATA_DIR` | Host folder for the database and photos (Compose) |
| `DATA_DIR` | Data folder inside the app (default `data`; Compose sets `/app/data`) |
| `DATABASE_URL` | Optional SQLAlchemy URL, e.g. Postgres on Supabase, instead of SQLite |
| `PROCESSORS` | Comma-separated processors to run (default: all of `photo_info`, `photo_hash`, `plate_reader`, `plate_registry`, `repost_matcher`, `claude_condition`) |
| `ANTHROPIC_API_KEY` | Enables `claude_condition`. Use a key created in a Console workspace; Max/Ultra plans include monthly API credits |
| `CLAUDE_MONTHLY_BUDGET_USD` | Spending cap for `claude_condition` per calendar month (default 20); ads wait for next month once it is reached |
| `CLAUDE_CONDITION_MODEL` | Model for the condition check (default `claude-sonnet-5-5`) |
| `DASHBOARD_URL` | Public dashboard address, used for links in alerts |
| `COMPOSE_PROFILES` | `dashboard` starts the dashboard; add `tunnel` for Cloudflare Tunnel |
| `DASHBOARD_BIND`, `DASHBOARD_PORT` | Where the dashboard listens on the host (default `127.0.0.1:8080`) |
| `DASHBOARD_AUTH=off` | No login; only for a trusted network |
| `CLOUDFLARE_TUNNEL_TOKEN`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | Tunnel and login, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |

To run the dashboard on your own machine without login:

```bash
DASHBOARD_AUTH=off uvicorn --factory --app-dir src dashboard.app:create_app --port 8000
```

`DASHBOARD_AUTH=off` is for local use only; deployed, the dashboard requires Cloudflare Access.

### Adding a model

Processors live in `src/processing/processors/`. Subclass `processing.base.Processor`, set
`name`, `version` and `target` (`"photo"` gets the original image file, `"ad"` gets the ad and
all its photos), implement `async run(target) -> dict`, register the class in
`src/processing/registry.py`, and add its name to `PROCESSORS`. Every archived target is
processed once per version (failures are retried up to 3 times), so bumping `version` re-runs
it over the whole archive. `photo_info` is a minimal example.

Optional hooks: `requires` (photo processors an ad processor waits for; their results arrive
in `AdTarget.photo_results`), `active_only` (skip removed ads), `apply(db, target_id, result)`
(write a result into other tables) and raising `processing.base.Deferred` (leave the target for
a later pass). The bundled processors:

| Processor | Target | What it does |
| --- | --- | --- |
| `photo_info` | photo | Image size and format (example) |
| `plate_reader` | photo | Finds and reads licence plates with local ONNX models |
| `photo_hash` | photo | Perceptual hash (dHash) for spotting the same photo in other ads |
| `plate_registry` | ad | Votes on the plate across the ad's photos, confirms it in the data.gov.il registry, saves it and the ownership history on the ad, links other ads with the same plate |
| `repost_matcher` | ad | Links reposts of the same car by shared photos and similar descriptions |
| `claude_condition` | ad | Claude Sonnet inspects photos and description for damage and price factors (one request per active ad, ~3 cents) |

## Tests

```bash
PYTHONPATH=src uv run --no-project --python 3.12 --with-requirements requirements.txt \
  python -m unittest discover -s tests
```

