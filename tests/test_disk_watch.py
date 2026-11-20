import datetime
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from car import car_finder
from tracking.disk_watch import DiskWatch, Usage

NOW = datetime.datetime(2026, 11, 21, 12, tzinfo=datetime.UTC)


def reading(percent: float) -> Usage:
    return Usage("System disk", "/", percent, 59 * (100 - percent) / 100, 59)


class DiskWatchTest(unittest.TestCase):
    def test_below_threshold_is_quiet(self):
        self.assertEqual(DiskWatch(threshold=85).messages([reading(60)], NOW), [])

    def test_alerts_once_then_repeats_later_or_when_higher(self):
        watch = DiskWatch(threshold=85)
        self.assertEqual(len(watch.messages([reading(88)], NOW)), 1)
        self.assertEqual(watch.messages([reading(89)], NOW + datetime.timedelta(hours=1)), [])
        self.assertEqual(len(watch.messages([reading(93)], NOW + datetime.timedelta(hours=2))), 1)
        self.assertEqual(len(watch.messages([reading(93)], NOW + datetime.timedelta(hours=15))), 1)

    def test_all_clear_after_dropping_below_margin(self):
        watch = DiskWatch(threshold=85)
        watch.messages([reading(90)], NOW)
        self.assertEqual(watch.messages([reading(82)], NOW), [])  # within the margin
        [text] = watch.messages([reading(40)], NOW)
        self.assertIn("back to 40%", text)
        self.assertEqual(watch.messages([reading(40)], NOW), [])


class StaleProfilesTest(unittest.TestCase):
    def test_removes_old_unused_profiles_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("tempfile.gettempdir", return_value=tmp):
            old, fresh, other = Path(tmp, "uc_old"), Path(tmp, "uc_new"), Path(tmp, "keep")
            for path in (old, fresh, other):
                path.mkdir()
                (path / "file").write_text("x")
            hour_ago = time.time() - 7200
            os.utime(old, (hour_ago, hour_ago))
            self.assertEqual(car_finder.remove_stale_profiles(), 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists() and other.exists())
            self.assertEqual(car_finder.remove_stale_profiles(min_age=0), 1)
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
