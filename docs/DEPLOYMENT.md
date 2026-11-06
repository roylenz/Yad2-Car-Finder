# Deploying to the Raspberry Pi

The bot runs on a Raspberry Pi with Docker Compose. The Pi deploys from the
`master` branch on GitHub (`origin`, https://github.com/roylenz/Yad2-Car-Finder).

| What | Value |
| --- | --- |
| SSH host | `pi` (alias in `~/.ssh/config`, user `roylenz`) |
| Checkout on the Pi | `~/Yad2-Car-Finder` |
| Compose service | `bot` (container `yad2-car-finder-bot-1`) |
| Secrets | `~/Yad2-Car-Finder/.env` on the Pi (mode 600, not in Git) |
| Data | Docker volume `bot_data`, mounted at `/app/data` (SQLite database) |

Your user on the Pi can't access the Docker socket, so run `docker` with `sudo`.

## Steps

1. Run the tests locally. They need Python 3.12; on Python 3.14 a dependency fails to import:

   ```bash
   PYTHONPATH=src uv run --no-project --python 3.12 --with-requirements requirements.txt \
     python -m unittest discover -s tests
   ```

2. Commit to `master` and push to GitHub:

   ```bash
   git push origin master
   ```

3. Pull the changes and rebuild on the Pi:

   ```bash
   ssh pi 'cd ~/Yad2-Car-Finder && git pull --ff-only && sudo docker compose up -d --build'
   ```

4. Check that the bot started:

   ```bash
   ssh pi 'cd ~/Yad2-Car-Finder && sudo docker compose ps && sudo docker compose logs --tail 30 bot'
   ```

## Notes

- Run only one instance per bot token. Telegram polling conflicts if two bots run.
- Rebuilding keeps the `bot_data` volume, so saved searches and listing history stay.
  Don't run `docker compose down -v` unless you mean to wipe that data.
- Schema changes have to migrate the existing SQLite database themselves on startup,
  for example with `ALTER TABLE ... ADD COLUMN` (see `src/bot/state.py`).
- To change settings, edit `.env` on the Pi and run `sudo docker compose up -d`.
