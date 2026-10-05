"""Source yield metrics count evidence stages, not presumed bargains."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app as workbench
import db
from services.source_metrics import current_source_yield


class SourceMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "isolated.sqlite3")
        self.db_patch.start()
        db.initialize()
        now = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,status,enabled,interval_minutes,last_success)
                VALUES('test','current feed','shopping','https://example.test/feed','rss','healthy',1,10,?)""",
                (now,)).lastrowid
            states = ("observed", "conditional", "missing_price", "conflict", "missing_time",
                      "stale", "activity", "excluded", "queued", "retry", "source_unavailable", None)
            for index, state in enumerate(states):
                published = None if state == "missing_time" else now
                event_id = conn.execute("""INSERT INTO events
                    (source_id,external_key,title,url,fingerprint,published_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (source_id, str(index), f"item {index}", f"https://example.test/{index}",
                     f"fp-{index}", published, now)).lastrowid
                opportunity_id = conn.execute("""INSERT INTO opportunities
                    (event_id,source_id,title,category,url) VALUES(?,?,?,?,?)""",
                    (event_id, source_id, f"item {index}", "shopping", f"https://example.test/{index}")).lastrowid
                if state:
                    checked_at = "2020-01-01 00:00:00" if state == "observed" else now
                    conn.execute("INSERT INTO auto_reviews(opportunity_id,state,reason) VALUES(?,?,?)",
                                 (opportunity_id, state, "synthetic"))
                    if state == "observed":
                        conn.execute("UPDATE auto_reviews SET checked_at=? WHERE opportunity_id=?",
                                     (checked_at, opportunity_id))
        self.source_id = source_id

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_current_yield_counts_each_review_stage_without_calling_it_verified(self):
        with db.connect() as conn:
            before = (conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                      conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
            metrics = current_source_yield(conn)[self.source_id]
            after = (conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                     conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
        self.assertEqual(before, after)
        self.assertEqual(metrics["reviewed_count"], 12)
        self.assertEqual(metrics["quote_extracted_count"], 2)
        self.assertEqual(metrics["conditional_count"], 1)
        self.assertEqual(metrics["missing_price_count"], 1)
        self.assertEqual(metrics["conflict_count"], 1)
        self.assertEqual(metrics["missing_time_count"], 1)
        self.assertEqual(metrics["stale_count"], 1)
        self.assertEqual(metrics["activity_count"], 1)
        self.assertEqual(metrics["excluded_count"], 1)
        self.assertEqual(metrics["queued_count"], 1)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(metrics["source_unavailable_count"], 1)
        self.assertEqual(metrics["review_behind_count"], 1)
        self.assertEqual(metrics["not_reviewed_count"], 1)

    def test_sources_page_explains_collection_status_and_shows_evidence_yield(self):
        response = workbench.app.test_client().get("/sources")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("采集有返回", page)
        self.assertIn("公开金额已提取 2", page)
        self.assertIn("缺明确金额 1", page)
        self.assertIn("这些阶段数不能直接相加", page)
        self.assertIn("此状态不代表报价有效", page)


if __name__ == "__main__":
    unittest.main()
