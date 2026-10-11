import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import scanner
import autoreview
from app import app
from services.taobao_alliance import search_config
from services.taobao_living_topics import (
    CORE_LIVING_TOPICS,
    add_custom_topic,
    configurations,
    ensure_core_topics,
    source_url,
)


VALID_ENV = {
    "TAOBAO_ALLIANCE_APP_KEY": "123456",
    "TAOBAO_ALLIANCE_APP_SECRET": "test-secret",
    "TAOBAO_ALLIANCE_ADZONE_ID": "7890",
}


class TaobaoLivingTopicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "topics.sqlite3")
        self.db_patch.start()
        db.initialize()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def _insert_paper_source(self):
        with db.connect() as conn:
            return conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES('淘宝联盟','淘宝店抽纸优惠搜索','零售优惠',?,'taobao_alliance',
                       'taobao_alliance','healthy',1,60)""", (source_url("抽纸"),)).lastrowid

    def test_core_topics_adopt_existing_paper_source_and_are_idempotent(self):
        paper_id = self._insert_paper_source()
        with db.connect() as conn:
            first = ensure_core_topics(conn, env=VALID_ENV)
            second = ensure_core_topics(conn, env=VALID_ENV)
            sources = conn.execute("SELECT * FROM sources WHERE method='taobao_alliance' ORDER BY id").fetchall()

        self.assertEqual([item["source_id"] for item in first["adopted"]], [paper_id])
        self.assertEqual(len(first["created"]), len(CORE_LIVING_TOPICS) - 1)
        self.assertEqual(second["created"], [])
        self.assertEqual(len(second["adopted"]), len(CORE_LIVING_TOPICS))
        self.assertEqual(len(sources), len(CORE_LIVING_TOPICS))
        configs = configurations(sources)
        paper = next(item for item in configs if item["key"] == "paper")
        self.assertEqual(paper["source_id"], paper_id)
        self.assertFalse(paper["duplicate_source_ids"])
        for row in sources:
            config = search_config(row["url"])
            self.assertEqual((config["page_no"], config["page_size"], config["max_pages"]), ("1", "100", "all"))

    def test_custom_topic_creates_an_independent_immutable_query(self):
        with db.connect() as conn:
            source_id = add_custom_topic(conn, name="垃圾袋", keyword="  家用   垃圾袋 ",
                                         interval_minutes=120, env=VALID_ENV)
            source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            with self.assertRaisesRegex(ValueError, "关键词“家用 垃圾袋”"):
                add_custom_topic(conn, name="另一名称", keyword="家用 垃圾袋",
                                 interval_minutes=60, env=VALID_ENV)

        self.assertEqual(source["name"], "淘宝生活品·垃圾袋优惠搜索")
        self.assertEqual(source["status"], "pending")
        self.assertEqual(search_config(source["url"])["q"], "家用 垃圾袋")
        self.assertEqual(source_url("大米"),
                         "https://eco.taobao.com/router/rest?q=%E5%A4%A7%E7%B1%B3&page_no=1&page_size=100&max_pages=all")

    def test_due_scheduler_scans_each_enabled_topic_source(self):
        self._insert_paper_source()
        with db.connect() as conn:
            ensure_core_topics(conn, env=VALID_ENV)
            conn.execute("UPDATE sources SET enabled=0 WHERE method='taobao_alliance'")
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM sources WHERE method='taobao_alliance' ORDER BY id LIMIT 2").fetchall()]
            conn.execute("UPDATE sources SET enabled=1,status='pending',last_checked=NULL WHERE id IN (?,?)", ids)
        with patch("scanner.scan_source", side_effect=lambda source_id, review=False: {"source_id": source_id}), \
                patch("autoreview.review_all"):
            rows = scanner.scan_all(due_only=True, method="taobao_alliance")
        self.assertEqual({row["source_id"] for row in rows}, set(ids))

    def test_taobao_reclassification_does_not_touch_other_collectors(self):
        with db.connect() as conn:
            conn.execute("UPDATE sources SET enabled=0")
            taobao_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled)
                VALUES('淘宝联盟','淘宝主题','零售优惠',?,'taobao_alliance','taobao_alliance','healthy',1)""",
                (source_url("大米"),)).lastrowid
            other_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,status,enabled)
                VALUES('其他平台','不应复审','零售优惠','https://example.test/feed','rss','healthy',1)""").lastrowid
            for source_id, key in ((taobao_id, "taobao"), (other_id, "other")):
                event_id = conn.execute("""INSERT INTO events
                    (source_id,external_key,title,url,snippet,fingerprint,last_seen_at)
                    VALUES(?,?,?,?,'',?,CURRENT_TIMESTAMP)""",
                    (source_id, key, key + " 商品", "https://item.taobao.com/item.htm?id=" + key,
                     key + "-fingerprint")).lastrowid
                conn.execute("""INSERT INTO opportunities(event_id,source_id,title,category,url)
                              VALUES(?,?,?,'零售优惠',?)""",
                             (event_id, source_id, key + " 商品", "https://item.taobao.com/item.htm?id=" + key))
            before = conn.execute("SELECT count(*) FROM auto_reviews").fetchone()[0]
        autoreview.review_all(source_method="taobao_alliance")
        with db.connect() as conn:
            reviewed = conn.execute("SELECT opportunity_id FROM auto_reviews").fetchall()
        self.assertEqual(before, 0)
        self.assertEqual(len(reviewed), 1)

    def test_sources_page_bootstraps_topics_without_duplicate_paper_source(self):
        paper_id = self._insert_paper_source()
        client = app.test_client()
        client.get("/sources")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        with patch.dict(os.environ, VALID_ENV, clear=False):
            response = client.post("/sources/taobao-topics/bootstrap", data={"csrf": csrf},
                                   follow_redirects=True)
            response_again = client.post("/sources/taobao-topics/bootstrap", data={"csrf": csrf},
                                         follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("淘宝生活品主题配置", response.get_data(as_text=True))
        self.assertIn("饮用水", response.get_data(as_text=True))
        self.assertIn("沿用 纸品", response_again.get_data(as_text=True))
        with db.connect() as conn:
            sources = conn.execute("SELECT id FROM sources WHERE method='taobao_alliance'").fetchall()
        self.assertEqual(len(sources), len(CORE_LIVING_TOPICS))
        self.assertIn(paper_id, [row["id"] for row in sources])

    def test_sources_page_creates_custom_topic_with_generated_official_url(self):
        client = app.test_client()
        client.get("/sources")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        with patch.dict(os.environ, VALID_ENV, clear=False):
            response = client.post("/sources/taobao-topics", data={
                "csrf": csrf,
                "topic_name": "厨房垃圾袋",
                "keyword": "厨房垃圾袋",
                "interval_minutes": "90",
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("生活品主题来源", response.get_data(as_text=True))
        with db.connect() as conn:
            source = conn.execute("SELECT * FROM sources WHERE name='淘宝生活品·厨房垃圾袋优惠搜索'").fetchone()
        self.assertIsNotNone(source)
        self.assertEqual(search_config(source["url"])["q"], "厨房垃圾袋")
        self.assertEqual(source["interval_minutes"], 90)


if __name__ == "__main__":
    unittest.main()
