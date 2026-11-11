import asyncio
import datetime
import random
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, update

from dashboard import auth, queries
from dashboard.app import create_app
from processing.processors.photo_hash import PhotoHashProcessor, distance, dhash
from processing.processors.repost_matcher import RepostMatcherProcessor
from processing.runner import ProcessingRunner
from storage import cars
from storage.schema import ad_links, ads, photos
from test_plates import FIRST, SECOND, StoreTestCase, limo, registry_transport
from tracking.sale_watch import SaleWatch, new_owners

THIRD = "https://www.yad2.co.il/vehicles/item/limo0003"
DESCRIPTION = "רכב שמור מאוד, טופל רק במוסך מורשה, צמיגים חדשים, ללא תאונות, מחיר גמיש לרציניים בלבד"


def picture(seed: int, quality: int = 90) -> bytes:
    """A blocky random picture; the same seed at another JPEG quality is the 'same' photo."""
    generator = random.Random(seed)
    image = Image.new("RGB", (8, 6))
    image.putdata([tuple(generator.randrange(256) for _ in range(3)) for _ in range(48)])
    buffer = BytesIO()
    image.resize((320, 240), Image.NEAREST).save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


class RepostTests(StoreTestCase):
    def save_pictures(self, token: str, seeds: list[int], quality: int) -> None:
        with self.state.engine.connect() as db:
            ids = db.execute(
                select(photos.c.id).where(photos.c.token == token).order_by(photos.c.position)
            ).scalars().all()
        for photo_id, seed in zip(ids, seeds, strict=True):
            sha, key = asyncio.run(self.store.save(picture(seed, quality), ".jpg"))
            self.state.ads.photo_stored(photo_id, sha, key, 10)

    def test_dhash_survives_reencoding(self):
        same = distance(dhash(Image.open(BytesIO(picture(1, 95)))), dhash(Image.open(BytesIO(picture(1, 60)))))
        other = distance(dhash(Image.open(BytesIO(picture(1)))), dhash(Image.open(BytesIO(picture(2)))))
        self.assertLessEqual(same, 6)
        self.assertGreater(other, 12)

    def test_reposts_are_merged_by_photos_and_description(self):
        three = tuple(f"{FIRST}/{name}.jpg" for name in "abc")
        early = datetime.datetime(2026, 10, 1)
        self.state.ads.record([limo(FIRST, images=three, km=40000, price=100000)], now=early)
        self.state.ads.mark_removed([FIRST], now=early + datetime.timedelta(days=5))
        self.state.ads.record(
            [limo(SECOND, images=tuple(f"{SECOND}/{n}.jpg" for n in "abc"), km=40500, price=95000)],
            now=early + datetime.timedelta(days=6),
        )
        # Same photos but a different year: a dealer's stock photos of another car.
        self.state.ads.record(
            [limo(THIRD, images=tuple(f"{THIRD}/{n}.jpg" for n in "abc"), km=41000, year=2023)],
            now=early + datetime.timedelta(days=7),
        )
        Image.init()
        for token, quality in (("limo0001", 92), ("limo0002", 70), ("limo0003", 80)):
            self.save_pictures(token, [1, 2, 3], quality)

        runner = ProcessingRunner(
            self.state.engine, self.store, [PhotoHashProcessor(), RepostMatcherProcessor()]
        )
        asyncio.run(runner.run_pending())

        with self.state.engine.connect() as db:
            self.assertEqual(cars.group_tokens(db, "limo0002"), ["limo0001", "limo0002"])
            self.assertEqual(cars.group_tokens(db, "limo0003"), ["limo0003"])
            reasons = db.execute(select(ad_links.c.reason)).scalars().all()
        self.assertEqual(reasons, ["3 matching photos"])

        rows, total = queries.list_ads(self.state.engine, status="all", sort="drop")
        self.assertEqual(total, 2)
        first = rows[0]
        self.assertEqual((first.token, first.reposts, first.price_drop, first.drops), ("limo0002", 1, 5000, 1))
        # Days on the market count from the first ad.
        self.assertEqual(first.market_since, early)

        web = TestClient(create_app(self.state.engine, self.store, auth.NoAuth()))
        page = web.get("/ads/limo0002").text
        self.assertIn("Same car, other ads", page)
        self.assertIn("3 matching photos", page)
        self.assertIn("/ads/limo0001", page)
        self.assertIn("Reposted ×1", web.get("/ads", params={"status": "all", "sort": "drops"}).text)

    def test_description_with_a_shared_photo_links_ads(self):
        self.state.ads.record([
            limo(FIRST, images=(f"{FIRST}/a.jpg", f"{FIRST}/b.jpg"), attributes={"description": DESCRIPTION}),
            limo(SECOND, images=(f"{SECOND}/a.jpg", f"{SECOND}/b.jpg"),
                 attributes={"description": DESCRIPTION + " החלפה אפשרית"}),
        ])
        self.save_pictures("limo0001", [1, 5], 90)
        self.save_pictures("limo0002", [1, 9], 75)
        runner = ProcessingRunner(
            self.state.engine, self.store, [PhotoHashProcessor(), RepostMatcherProcessor()]
        )
        asyncio.run(runner.run_pending())
        with self.state.engine.connect() as db:
            self.assertEqual(cars.group_tokens(db, "limo0001"), ["limo0001", "limo0002"])
            reason = db.execute(select(ad_links.c.reason)).scalar_one()
        self.assertTrue(reason.startswith("1 matching photo, description"), reason)

    def test_dealer_template_description_is_ignored(self):
        template = "פריסבי מציעה מבחר גדול של מכוניות פרטיות מיד ראשונה, כמו מכונית חדשה, עד שנתיים אחריות"
        urls = [f"https://www.yad2.co.il/vehicles/item/limo000{n}" for n in range(1, 5)]
        self.state.ads.record([
            limo(url, images=(f"{url}/a.jpg",), km=30000 + n * 100, attributes={"description": template})
            for n, url in enumerate(urls)
        ])
        for n, url in enumerate(urls):
            self.save_pictures(url.rsplit("/", 1)[1], [n], 90)
        runner = ProcessingRunner(
            self.state.engine, self.store, [PhotoHashProcessor(), RepostMatcherProcessor()]
        )
        asyncio.run(runner.run_pending())
        with self.state.engine.connect() as db:
            self.assertEqual(db.execute(select(ad_links)).all(), [])


class DropSortTests(StoreTestCase):
    def test_sort_by_drop_amount_and_count(self):
        start = datetime.datetime(2026, 10, 1)
        self.state.ads.record([limo(FIRST, price=100000), limo(SECOND, price=100000)], now=start)
        # FIRST: two small drops (-4,000); SECOND: one big drop (-10,000).
        self.state.ads.record([limo(FIRST, price=98000), limo(SECOND, price=90000)], now=start + datetime.timedelta(days=1))
        self.state.ads.record([limo(FIRST, price=96000), limo(SECOND, price=90000)], now=start + datetime.timedelta(days=2))
        by_amount, _ = queries.list_ads(self.state.engine, sort="drop")
        by_count, _ = queries.list_ads(self.state.engine, sort="drops")
        self.assertEqual([(row.token, row.price_drop, row.drops) for row in by_amount],
                         [("limo0002", 10000, 1), ("limo0001", 4000, 2)])
        self.assertEqual([row.token for row in by_count], ["limo0001", "limo0002"])


class SaleWatchTests(StoreTestCase):
    def history(self, *entries):
        return [{"mispar_rechev": 13107604, "baalut_dt": month, "baalut": owner} for month, owner in entries]

    def test_new_owners_ignores_the_known_history(self):
        baseline = [{"month": "2024-01", "owner": "private"}, {"month": "2026-11", "owner": "dealer"}]
        removed = datetime.datetime(2026, 11, 10)
        self.assertEqual(new_owners(baseline, baseline, removed), [])
        later = [*baseline, {"month": "2026-11", "owner": "private"}]
        self.assertEqual(new_owners(later, baseline, removed), [{"month": "2026-11", "owner": "private"}])
        # Without a saved history, only entries from the removal month on count.
        self.assertEqual(new_owners(later, None, removed), later[1:])

    def test_removed_ad_is_watched_then_marked_sold_or_not_seen(self):
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        self.state.ads.record([limo(FIRST), limo(SECOND)], now=now - datetime.timedelta(days=20))
        baseline = [{"month": "2024-01", "owner": "private"}]
        with self.state.engine.begin() as db:
            db.execute(update(ads).values(plate="13107604", plate_status="confirmed", ownership=baseline))
            db.execute(update(ads).where(ads.c.token == "limo0002").values(plate="5555555"))
        self.state.ads.mark_removed([FIRST], now=now - datetime.timedelta(days=1))
        self.state.ads.mark_removed([SECOND], now=now - datetime.timedelta(days=9))

        histories = {13107604: self.history((202401, "פרטי")), 5555555: self.history((202401, "פרטי"))}
        client = httpx.AsyncClient(transport=registry_transport({}, histories=histories))
        watch = SaleWatch(self.state.engine, client)
        self.assertEqual(asyncio.run(watch.run()), 2)
        statuses = dict(self.state.engine.connect().execute(select(ads.c.token, ads.c.sale_status)).all())
        self.assertEqual(statuses, {"limo0001": "watching", "limo0002": "not_seen"})
        self.assertEqual(watch.due(), [])  # checked recently

        removal_month = int(f"{now - datetime.timedelta(days=1):%Y%m}")
        histories[13107604] = self.history((202401, "פרטי"), (removal_month, "פרטי"))
        later = now + datetime.timedelta(hours=13)
        self.assertEqual([row.token for row in watch.due(later)], ["limo0001"])
        watch._save(watch.due(later)[0], [{"month": "2024-01", "owner": "private"},
                                          {"month": f"{removal_month // 100}-{removal_month % 100:02d}", "owner": "private"}], later)
        with self.state.engine.connect() as db:
            sold = db.execute(select(ads).where(ads.c.token == "limo0001")).one()
        self.assertEqual((sold.sale_status, sold.sold_to), ("sold", "private"))

        web = TestClient(create_app(self.state.engine, self.store, auth.NoAuth()))
        self.assertIn("Sold", web.get("/ads/limo0001").text)
        self.assertIn("Sale follow-up", web.get("/background").text)

        # Back on Yad2: the conclusion is cleared.
        self.state.ads.record([limo(FIRST)])
        with self.state.engine.connect() as db:
            self.assertIsNone(db.execute(select(ads.c.sale_status).where(ads.c.token == "limo0001")).scalar_one())
