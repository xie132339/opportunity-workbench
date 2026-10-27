"""Large histories must not change migration or review results at batch edges."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autoreview
import db


class MemoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = patch.object(db, "DB_PATH", Path(self.temp.name) / "test.sqlite3")
        self.database.start()
        db.initialize()

    def tearDown(self):
        self.database.stop()
        self.temp.cleanup()

    def test_startup_migration_reaches_rows_after_first_batch(self):
        with db.connect() as connection:
            connection.executemany(
                "INSERT INTO opportunities(title,category) VALUES(?,'零售优惠')",
                [("普通商品",)] * 519 + [("新人专享商品",)],
            )
        db.initialize()
        with db.connect() as connection:
            self.assertEqual(connection.execute("""SELECT COUNT(*) FROM opportunities
                WHERE offer_type='suspected_new_user'""").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 520)

    def test_review_reaches_later_batches_and_preserves_duplicate_rule(self):
        with db.connect() as connection:
            source_id = connection.execute("""INSERT INTO sources
                (platform,name,category,url,method,status,enabled,last_success)
                VALUES('测试','批量源','零售优惠','https://example.test/feed',
                       'html','healthy',1,CURRENT_TIMESTAMP)""").lastrowid
            for number in range(450):
                url = f"https://example.test/item/{199 if number == 200 else number}"
                event_id = connection.execute("""INSERT INTO events
                    (source_id,external_key,title,url,fingerprint,published_at)
                    VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (source_id, str(number), f"商品 {number} 10元", url, str(number))).lastrowid
                connection.execute("""INSERT INTO opportunities
                    (event_id,source_id,title,category,url) VALUES(?,?,?,?,?)""",
                    (event_id, source_id, f"商品 {number} 10元", "零售优惠", url))

        def classified(_row, _now, duplicate=False):
            return dict(state="excluded" if duplicate else "observed", reason="batch check",
                        advertised_cents=None, specification="", conditions="", evidence="")

        with patch.object(autoreview, "classify", side_effect=classified):
            counts = autoreview.review_all(source_method="html")
        self.assertEqual(sum(counts.values()), 450)
        self.assertEqual(counts["excluded"], 1)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auto_reviews").fetchone()[0], 450)
