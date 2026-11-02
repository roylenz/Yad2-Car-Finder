# Yad2 Car Finder 🚗 
Telegram bot that scrapes [Yad2](https://www.yad2.co.il/vehicles/cars) for car listings based on your filters and sends you notifications when new cars appear

## Features
- **Interactive configuration** - configure manufacturer, model, year range via inline keyboard buttons in Telegram
- **Periodic scanning** - automatically checks for new listings every X minutes (configurable: 5/10/15/30/60) or run a one-time scan
- **Smart notifications** - first scan sends all found listings, subsequent scans only notify about new ones
- **Structured logging** - colored console output with structured JSON kwargs, configurable log level via `LOG_LEVEL` env var
- **Bot commands** - `/start`, `/search`, `/stop`, `/status`

The manufacturer picker includes all 127 makes shown in Yad2's car search filter on 2 November 2026. Model IDs come from a [Yad2 API catalog snapshot](https://github.com/SRaz-Sw/Local-Whisper-Diarization/blob/main/nextjs-v1/public/data/yad2-mappings.json) dated 20 August 2025, with current models for the five newer makes when Yad2 offered them. The model picker shows models for the selected makes. Yad2 may add makes or models after these snapshots.

## Setup

### Docker Compose (recommended for Raspberry Pi)

Install Docker and the Compose plugin, then create a private `.env` file from
`.env.example` and set `TELEGRAM_BOT_TOKEN` to your BotFather token. Keep the
token out of Git.

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
ports need to be published. Only run one instance for a given token. The current
bot keeps search configuration and seen listings in memory; restarting it resets
active scans and their history.

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
```

### Running

```bash
python3 src/main.py
```

Then open your bot in Telegram and send `/start`

## Usage

1. `/start` - welcome message with a "New Search" button
2. `/search` - configure a new search (manufacturer, model, year range, scan interval)
3. `/stop` - stop the active scan
4. `/status` - show current scan configuration and how many cars have been found
