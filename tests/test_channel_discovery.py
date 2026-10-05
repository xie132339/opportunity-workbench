"""Channel catalog tests use synthetic routes and never call GitHub or a shop."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import channel_discovery as discovery
import app as workbench


ROUTE = """
export const route = {
 path: '/cn/offers', categories: ['shopping'], example: '/shop/cn/offers',
 features: { requireConfig: false, requirePuppeteer: false, antiCrawler: false },
 name: '中国 - 会员特惠'
};
"""


class ChannelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "isolated.sqlite3")
        self.db_patch.start()
        db.initialize()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def insert_candidate(self, state="discovered"):
        with db.connect() as conn:
            return conn.execute("""INSERT INTO source_candidates
                (platform,name,category,url,method,discovery_provider,discovery_repo,
                 discovery_path,discovery_url,repo_license,route_example,state,reason)
                VALUES('SHOP','会员特惠','零售优惠','http://127.0.0.1:1200/shop/cn/offers',
                 'rsshub','rsshub','DIYgod/RSSHub','lib/routes/shop/offers.ts',
                 'https://github.com/DIYgod/RSSHub/blob/master/lib/routes/shop/offers.ts',
                 'AGPL-3.0','/shop/cn/offers',?,'test')""", (state,)).lastrowid

    def test_route_parser_requires_public_relevant_feed(self):
        row = discovery.parse_rsshub_route("lib/routes/shop/offers.ts", ROUTE)
        self.assertEqual(row["url"], "http://127.0.0.1:1200/shop/cn/offers")
        self.assertEqual(row["state"], "discovered")
        self.assertIsNone(discovery.parse_rsshub_route(
            "lib/routes/news/latest.ts", ROUTE.replace("['shopping']", "['new-media']")))
        self.assertIsNone(discovery.parse_rsshub_route(
            "lib/routes/shop/item.ts", ROUTE.replace("/shop/cn/offers", "/shop/:id")))
        blocked = discovery.parse_rsshub_route(
            "lib/routes/shop/offers.ts", ROUTE.replace("requireConfig: false", "requireConfig: true"))
        self.assertEqual(blocked["state"], "rejected")

    def test_github_refresh_is_dynamic_and_keeps_provenance(self):
        def fake_gh(*args, **kwargs):
            if args[:2] == ("api", "repos/DIYgod/RSSHub"):
                return json.dumps({"default_branch": "master", "pushed_at": "2026-10-04T00:00:00Z",
                                   "license": {"spdx_id": "AGPL-3.0"}})
            if args[0] == "api" and "/git/trees/" in args[1]:
                return json.dumps({"tree": [{"path": "lib/routes/shop/offers.ts"}]})
            if args[0] == "api" and "contents" in args[1]:
                return ROUTE
            raise AssertionError(args)
        with patch.object(discovery, "_gh", side_effect=fake_gh):
            result = discovery.refresh(queries=("优惠",))
        self.assertEqual(result["discovered"], 1)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM source_candidates").fetchone()
            self.assertEqual(row["discovery_path"], "lib/routes/shop/offers.ts")
            self.assertEqual(row["repo_license"], "AGPL-3.0")
            self.assertEqual(row["state"], "discovered")

    def test_real_extraction_gate_and_promotion(self):
        candidate_id = self.insert_candidate()
        records = [("one", "当前公开优惠", "https://shop.example/item/1", "说明", None)]
        with patch.object(discovery, "_rss_rows", return_value=records):
            self.assertEqual(discovery.validate(candidate_id)["state"], "validated")
        source_id = discovery.promote(candidate_id)
        with db.connect() as conn:
            candidate = conn.execute("SELECT * FROM source_candidates WHERE id=?", (candidate_id,)).fetchone()
            source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            self.assertEqual(candidate["state"], "promoted")
            self.assertEqual(candidate["observed_items"], 1)
            self.assertEqual(source["enabled"], 1)
            self.assertEqual(source["status"], "pending")

    def test_failed_or_unchecked_candidate_cannot_be_promoted(self):
        candidate_id = self.insert_candidate()
        with patch.object(discovery, "_rss_rows", side_effect=RuntimeError("route down")):
            self.assertEqual(discovery.validate(candidate_id)["state"], "failed")
        with self.assertRaises(ValueError):
            discovery.promote(candidate_id)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM source_candidates WHERE id=?", (candidate_id,)).fetchone()
            self.assertIn("route down", row["last_error"])
            self.assertEqual(conn.execute("SELECT count(*) FROM sources WHERE platform='SHOP'").fetchone()[0], 0)

    def test_candidates_are_visible_on_existing_sources_page(self):
        self.insert_candidate()
        client = workbench.app.test_client()
        page = client.get("/sources")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("GitHub 候选渠道", text)
        self.assertIn("lib/routes/shop/offers.ts", text)


if __name__ == "__main__":
    unittest.main()
