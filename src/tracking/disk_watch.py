"""Telegram alert to the bot admins when the Pi's disk fills up.

Checks the filesystems holding the container's root (on the Pi that is the SD card, where Docker
keeps /tmp and the image) and DATA_DIR (the SSD when HOST_DATA_DIR is set) every CHECK_SECONDS.
A filesystem at or above DISK_ALERT_PERCENT (default 85) alerts once, again every REPEAT_HOURS
while it stays high or when it rises another STEP points, and sends an all-clear once it drops
RECOVER_MARGIN points below the threshold. Each check also deletes leftover Chrome profiles.
"""

import datetime
import os
import shutil
from dataclasses import dataclass, field

from car.car_finder import remove_stale_profiles
from common.logger import logger

CHECK_SECONDS = 30 * 60
REPEAT_HOURS = 12
STEP = 5
RECOVER_MARGIN = 5


def alert_percent() -> float:
    try:
        return float(os.getenv("DISK_ALERT_PERCENT", "85"))
    except ValueError:
        return 85.0


def watched_paths() -> dict[str, str]:
    """Name → path, one per filesystem."""
    paths = {"System disk": "/", "Data": os.getenv("DATA_DIR", "/app/data")}
    seen, result = set(), {}
    for name, path in paths.items():
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue
        if device not in seen:
            seen.add(device)
            result[name] = path
    return result


@dataclass
class Usage:
    name: str
    path: str
    percent: float
    free_gb: float
    total_gb: float


def usage(name: str, path: str) -> Usage:
    total, used, free = shutil.disk_usage(path)
    # Same figure as `df`: space usable by non-root users.
    percent = 100 * used / (used + free) if used + free else 0.0
    return Usage(name, path, percent, free / 1e9, total / 1e9)


@dataclass
class DiskWatch:
    threshold: float = field(default_factory=alert_percent)
    # path -> (percent alerted at, when)
    alerted: dict[str, tuple[float, datetime.datetime]] = field(default_factory=dict)

    def messages(self, readings: list[Usage], now: datetime.datetime | None = None) -> list[str]:
        """Alerts and all-clears due for these readings (and remember them)."""
        now = now or datetime.datetime.now(datetime.UTC)
        out = []
        for reading in readings:
            last = self.alerted.get(reading.path)
            if reading.percent >= self.threshold:
                due = (
                    last is None
                    or reading.percent >= last[0] + STEP
                    or now - last[1] >= datetime.timedelta(hours=REPEAT_HOURS)
                )
                if due:
                    self.alerted[reading.path] = (reading.percent, now)
                    out.append(
                        f"⚠️ {reading.name} is {reading.percent:.0f}% full on the Pi: "
                        f"{reading.free_gb:.1f} GB free of {reading.total_gb:.0f} GB."
                    )
            elif last is not None and reading.percent < self.threshold - RECOVER_MARGIN:
                del self.alerted[reading.path]
                out.append(
                    f"✅ {reading.name} is back to {reading.percent:.0f}% full "
                    f"({reading.free_gb:.1f} GB free)."
                )
        return out

    async def check(self, context) -> None:
        """JobQueue callback: clean leftover Chrome profiles, then alert the admins if needed."""
        remove_stale_profiles()
        readings = [usage(name, path) for name, path in watched_paths().items()]
        texts = self.messages(readings)
        if not texts:
            return
        admins = context.application.bot_data["access"].admin_ids()
        logger.warning("Disk usage alert", texts=texts, admins=len(admins))
        for admin_id in admins:
            for text in texts:
                try:
                    await context.bot.send_message(admin_id, text)
                except Exception as e:
                    logger.warning("Could not send disk alert", admin_id=admin_id, error=str(e))
