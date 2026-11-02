# Yad2 Car Finder 🚗 
Telegram bot that scrapes [Yad2](https://www.yad2.co.il/vehicles/cars) for car listings based on your filters and sends you notifications when new cars appear

## Features
- **Interactive configuration** - configure manufacturer, model, year range via inline keyboard buttons in Telegram
- **Periodic scanning** - automatically checks for new listings every X minutes (configurable: 5/10/15/30/60) or run a one-time scan
- **Search URL alerts** - send a filtered Yad2 car search URL directly to the bot; it checks every 10 minutes
- **New car and price drop alerts** - messages include the listing price, mileage, and link
- **Persistent tracking** - searches and last seen prices survive container restarts
- **Bounded concurrent scans** - a shared queue runs at most `MAX_CONCURRENT_SCANS` browser scans at once (default 2)
- **Private access** - users request access with their name; admins approve requests and can promote other admins
- **Structured logging** - colored console output with structured JSON kwargs, configurable log level via `LOG_LEVEL` env var
- **Bot commands** - `/start`, `/search`, `/stop`, `/status`
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
stores active searches and listing prices in the `bot_data` volume.
Each private chat has its own saved search and listing history. Alerts are sent
only to the chat that created the search. When several scans become due together,
they wait in a FIFO queue; repeated triggers for the same search do not pile up.
Set `MAX_CONCURRENT_SCANS` in `.env` to adjust the number of browser scans allowed
at once. `/status` shows whether your search is queued, running, or waiting.

To update, pull the latest changes and run `docker compose up -d --build` again.

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
minutes. `/status` shows the active URL; `/stop` stops its alerts. A new URL
replaces the previous search for that chat. If Yad2 does not list a price or
mileage for a car, the alert says "Not listed" for that field.

## Usage

1. Tap **Menu** next to the message box to see the available commands, or tap **Commands** after `/start`
2. `/start` - welcome message with search link and filter buttons (or access instructions for new users)
3. `/search` - configure a new search (manufacturer, model, year range, scan interval)
4. `/stop` - stop the active scan
5. `/status` - show current scan configuration and how many cars have been found
