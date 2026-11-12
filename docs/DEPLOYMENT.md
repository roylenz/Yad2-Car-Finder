# Deploying to the Raspberry Pi

The bot runs on a Raspberry Pi with Docker Compose. The Pi deploys from the
`master` branch on GitHub (`origin`, https://github.com/roylenz/Yad2-Car-Finder).

| What | Value |
| --- | --- |
| SSH host | `pi` (alias in `~/.ssh/config`, user `roylenz`) |
| Checkout on the Pi | `~/Yad2-Car-Finder` |
| Compose services | `bot` (container `yad2-car-finder-bot-1`); profile `dashboard` adds `dashboard`, profile `tunnel` adds `cloudflared` |
| Secrets | `~/Yad2-Car-Finder/.env` on the Pi (mode 600, not in Git) |
| Data | `/app/data` in the containers: SQLite database, `photos/`, `thumbs/`. On the host it is `HOST_DATA_DIR` (the SSD, see below) or, if unset, the Docker volume `bot_data` |

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

   For a dashboard-only change, rebuild the shared image and restart just the dashboard, so a
   running scan isn't interrupted (`up --build --no-deps dashboard` alone does not rebuild it,
   because the image is built by the `bot` service):

   ```bash
   ssh pi 'cd ~/Yad2-Car-Finder && git pull --ff-only && sudo docker compose build bot && sudo docker compose up -d --no-deps dashboard'
   ```

4. Check that the bot started:

   ```bash
   ssh pi 'cd ~/Yad2-Car-Finder && sudo docker compose ps && sudo docker compose logs --tail 30 bot'
   ```

## Notes

- Run only one instance per bot token. Telegram polling conflicts if two bots run.
- Rebuilding keeps the `bot_data` volume, so saved searches and listing history stay.
  Don't run `docker compose down -v` unless you mean to wipe that data.
- Schema changes are Alembic migrations in `src/storage/migrations/versions/`. The bot runs
  `alembic upgrade head` on startup (`storage.db.migrate`), so deploying applies them. Add a new
  numbered file for each change; never edit a migration that has already run on the Pi.
- To change settings, edit `.env` on the Pi and run `sudo docker compose up -d`.

## Claude condition check (API key)

`claude_condition` runs only when `ANTHROPIC_API_KEY` is set in `.env` on the Pi. Create the key
inside a Console **workspace** (an organization-wide key is rejected without a workspace
header). Never paste the key into a chat or commit it; add it from your own terminal:

```bash
ssh -t pi 'cd ~/Yad2-Car-Finder && read -rsp "Workspace API key: " key && echo \
  && sed -i "/^ANTHROPIC_API_KEY=/d" .env && echo "ANTHROPIC_API_KEY=$key" >> .env && echo saved'
ssh pi 'cd ~/Yad2-Car-Finder && sudo docker compose up -d'
```

`CLAUDE_MONTHLY_BUDGET_USD` (default 20) caps what the check spends per calendar month; set a
spend limit on the workspace in the Console as well. The first run after enabling it checks
every active ad once (about 3 cents each).

## First deploy of the ad archive (one time)

This version moves the per-chat `listings` table into the global ad catalog (migration `0002`)
and drops `listings`. Back up the database first:

```bash
ssh pi 'cd ~/Yad2-Car-Finder && sudo docker compose cp bot:/app/data/bot.sqlite3 ~/bot-before-archive.sqlite3'
```

Saved searches and alert state carry over, so nobody gets re-alerted about cars they already saw.

## Moving the archive to an SSD

Photos are kept forever at Yad2's original size (about 50 KB each, roughly 250 KB per ad), so
put the data on an SSD rather than the SD card. Do this once:

1. Plug in the SSD and find it with `lsblk`. **Formatting erases the disk**; skip `mkfs` if it
   already has an ext4 file system you want to keep:

   ```bash
   sudo mkfs.ext4 -L yad2data /dev/sda1          # check the device name first!
   sudo mkdir -p /mnt/ssd
   echo "UUID=$(sudo blkid -s UUID -o value /dev/sda1) /mnt/ssd ext4 defaults,noatime,nofail 0 2" | sudo tee -a /etc/fstab
   sudo mount -a
   sudo mkdir -p /mnt/ssd/yad2 && sudo chown 10001:10001 /mnt/ssd/yad2   # the container's user
   ```

2. Stop the bot and copy the current data from the Docker volume:

   ```bash
   cd ~/Yad2-Car-Finder
   sudo docker compose stop bot
   sudo docker run --rm -v yad2-car-finder_bot_data:/from -v /mnt/ssd/yad2:/to alpine cp -a /from/. /to/
   ```

3. Set `HOST_DATA_DIR=/mnt/ssd/yad2` in `.env` and start again with `sudo docker compose up -d`.
   Keep the old volume until you have checked that `/status` still shows your search.

`DATABASE_URL` can point the bot at Postgres (for example Supabase) instead of SQLite. Photos
stay on the SSD either way.

## Dashboard on the home network (no login)

Until Cloudflare Access is set up, the dashboard runs without login and is reachable only on the
home network and over Tailscale. In `.env` on the Pi:

```bash
COMPOSE_PROFILES=dashboard
DASHBOARD_BIND=0.0.0.0
DASHBOARD_AUTH=off
DASHBOARD_PORT=8080
DASHBOARD_URL=http://<pi address>:8080   # alerts link to each ad's history
```

Then `sudo docker compose up -d --build`. Do not forward this port on the router: anyone who can
reach it can see the archive.

## Dashboard in public (Cloudflare Tunnel + Cloudflare Access)

With the `tunnel` profile the dashboard is reached through Cloudflare instead; remove
`DASHBOARD_BIND`/`DASHBOARD_AUTH` so the port is only on the Pi itself and login is enforced. `cloudflared` opens an outbound tunnel to
Cloudflare, and Cloudflare Access asks for login (email code or Google) before any request
reaches it. The app also verifies Access's signed token, so requests that skip Access get 403.
This needs a domain on Cloudflare (free plan is enough).

1. In the Cloudflare Zero Trust dashboard, **Networks → Tunnels → Create a tunnel**
   (type cloudflared). Copy the tunnel token. Add a **public hostname**, e.g.
   `cars.example.com`, with service `HTTP` and URL `dashboard:8000`.
2. **Access → Applications → Add → Self-hosted** for the same hostname. Add a policy that
   allows only your email address(es). Copy the application's **Audience (AUD) tag**.
3. In `.env` on the Pi set:

   ```bash
   COMPOSE_PROFILES=dashboard,tunnel
   CLOUDFLARE_TUNNEL_TOKEN=<tunnel token>
   CF_ACCESS_TEAM_DOMAIN=<your-team>.cloudflareaccess.com
   CF_ACCESS_AUD=<audience tag>
   DASHBOARD_URL=https://cars.example.com   # alerts then link to each ad's history
   ```

4. `sudo docker compose up -d --build`, then open the hostname. Check
   `sudo docker compose logs --tail 30 dashboard cloudflared` if it does not load.

If `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD` are missing, every page returns 403 (fails closed).
To try the dashboard on your own machine, see the README.
