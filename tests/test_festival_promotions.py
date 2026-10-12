import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import scanner
from app import app
from services.festival_promotions import (create_festival, festival_products,
                                          create_festival_material_feed,
                                          list_festivals)
from services.taobao_alliance import parse_item, search_config


CHINA = timezone(timedelta(hours=8))


class FestivalPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "festival.sqlite3"
        db.initialize()
        self.env = {
            "TAOBAO_ALLIANCE_APP_KEY": "35432822",
            "TAOBAO_ALLIANCE_APP_SECRET": "test-secret-never-persisted",
            "TAOBAO_ALLIANCE_ADZONE_ID": "123456789",
        }
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self.previous_path
        self.temp.cleanup()

    @staticmethod
    def _item(now, item_id, *, reserve, promotion, coupon="", threshold=""):
        return {
            "num_iid": item_id, "title": f"节日测试商品 {item_id}",
            "item_url": f"https://item.taobao.com/item.htm?id={item_id}",
            "reserve_price": reserve, "zk_final_price": promotion,
            "coupon_amount": coupon, "coupon_start_fee": threshold,
            "coupon_start_time": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "coupon_end_time": (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "coupon_remain_count": "20", "coupon_total_count": "100",
            "shop_title": "测试官方店", "user_type": 0,
        }

    def _festival(self, minimum_discount="0"):
        with db.connect() as conn:
            festival_id = create_festival(
                conn, festival_code="double_11", festival_year="2026", festival_name="",
                campaign_name="家清主会场", activity_material_id="987654321",
                material_id="27939", interval_minutes=60, minimum_discount=minimum_discount,
                env=self.env,
            )
            festival = dict(conn.execute("""SELECT f.*,c.source_id FROM festival_promotions f
                JOIN taobao_campaigns c ON c.id=f.campaign_id WHERE f.id=?""", (festival_id,)).fetchone())
        return festival

    def test_festival_is_separate_from_normal_campaign_and_keeps_ids_distinct(self):
        festival = self._festival()
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM taobao_campaigns WHERE id=?", (festival["campaign_id"],)).fetchone()
        self.assertEqual(festival["festival_code"], "double_11")
        self.assertEqual(festival["festival_name"], "双11")
        self.assertEqual(row["activity_material_id"], "987654321")
        self.assertEqual(row["material_id"], "27939")

    def test_direct_participation_is_ranked_and_bundle_coupon_is_separate(self):
        now = datetime.now(CHINA)
        festival = self._festival()
        rows = [
            parse_item(self._item(now, "101", reserve="30", promotion="20"), now=now),
            parse_item(self._item(now, "102", reserve="10", promotion="10", coupon="2", threshold="8"), now=now),
            parse_item(self._item(now, "103", reserve="10", promotion="10", coupon="2", threshold="12"), now=now),
        ]
        with patch("scanner.taobao_alliance_rows", return_value=rows):
            scanner.scan_source(festival["source_id"])
        with db.connect() as conn:
            products = festival_products(conn, festival, now=now)
        self.assertEqual([row["title"] for row in products["direct"]], ["节日测试商品 101", "节日测试商品 102"])
        self.assertEqual(products["direct"][0]["participation"]["label"], "可直接参与")
        self.assertEqual(products["direct"][1]["participation"]["label"], "领取单品券后参与")
        self.assertEqual(products["bundle"][0]["title"], "节日测试商品 103")
        self.assertEqual(products["bundle"][0]["participation"]["gap_cents"], 200)

    def test_threshold_excludes_small_direct_discount_without_hiding_bundle_observation(self):
        now = datetime.now(CHINA)
        festival = self._festival(minimum_discount="5")
        rows = [
            parse_item(self._item(now, "101", reserve="10", promotion="8"), now=now),
            parse_item(self._item(now, "103", reserve="10", promotion="10", coupon="2", threshold="12"), now=now),
        ]
        with patch("scanner.taobao_alliance_rows", return_value=rows):
            scanner.scan_source(festival["source_id"])
        with db.connect() as conn:
            products = festival_products(conn, festival, now=now)
        self.assertEqual(products["direct"], [])
        self.assertEqual(len(products["bundle"]), 1)

    def test_entry_page_explains_the_rule_without_creating_fake_festival_data(self):
        page = app.test_client().get("/festival-promotions")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("节日大促", html)
        self.assertIn("双11", html)
        self.assertIn("最优参与", html)
        self.assertIn("需凑单后再核验", html)

    def test_list_festivals_keeps_current_products_under_their_own_source(self):
        festival = self._festival()
        with db.connect() as conn:
            festivals = list_festivals(conn)
        self.assertEqual(len(festivals), 1)
        self.assertEqual(festivals[0]["id"], festival["id"])
        self.assertEqual(festivals[0]["products"]["direct"], [])
        self.assertEqual(festivals[0]["products"]["bundle"], [])

    def test_discovered_product_material_creates_separate_scheduled_festival_feed(self):
        material = {"material_id": "111338", "material_name": "快消第二波淘礼金补贴货盘",
                    "start_time": "2026-10-10 00:00:00", "end_time": "2026-10-31 00:00:00"}
        with db.connect() as conn:
            feed_id = create_festival_material_feed(
                conn, festival_code="double_11", festival_year="2026", material=material,
                minimum_discount="5", interval_minutes=60, env=self.env,
            )
            feed = conn.execute("SELECT * FROM festival_material_feeds WHERE id=?", (feed_id,)).fetchone()
            source = conn.execute("SELECT * FROM sources WHERE id=?", (feed["source_id"],)).fetchone()
            festivals = list_festivals(conn)
        self.assertEqual(feed["material_id"], "111338")
        self.assertEqual(feed["festival_name"], "双11")
        self.assertEqual(feed["material_name"], "快消第二波淘礼金补贴货盘")
        self.assertEqual(search_config(source["url"])["material_id"], "111338")
        self.assertEqual(search_config(source["url"])["q"], "双11")
        self.assertNotIn("activity_material_id=", source["url"])
        self.assertEqual([item["feed_type"] for item in festivals], ["official_material"])
        self.assertEqual(festivals[0]["products"]["direct"], [])

    def test_current_material_id_is_revalidated_before_it_can_be_monitored(self):
        client = app.test_client()
        page = client.get("/festival-promotions")
        self.assertEqual(page.status_code, 200)
        self.assertIn("查询当前官方促销物料", page.get_data(as_text=True))
        with client.session_transaction() as session:
            csrf = session["csrf"]
        with patch.dict(os.environ, self.env, clear=False), patch(
                "routes.festival_promotions.fetch_promoter_material_ids", return_value=[]):
            response = client.post("/festival-promotions/materials", data={
                "csrf": csrf, "material_id": "999999", "festival_code": "double_11",
                "festival_year": "2026", "minimum_discount": "5", "interval_minutes": "60",
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("该物料已不在淘宝联盟当前可发现列表中", response.get_data(as_text=True))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM festival_material_feeds").fetchone()[0], 0)

    def test_discovery_route_calls_official_api_and_renders_current_materials(self):
        client = app.test_client()
        client.get("/festival-promotions")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        material = {"material_id": "111338", "material_name": "官方快消活动物料",
                    "start_time": "2026-10-10 00:00:00", "end_time": "2026-10-31 00:00:00"}
        with patch.dict(os.environ, self.env, clear=False), patch(
                "routes.festival_promotions.fetch_promoter_material_ids", return_value=[material]) as fetch:
            response = client.post("/festival-promotions/discover", data={"csrf": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIn("111338", response.get_data(as_text=True))
        self.assertIn("官方快消活动物料", response.get_data(as_text=True))
        fetch.assert_called_once()

    def test_add_material_route_revalidates_and_creates_a_scheduled_feed(self):
        client = app.test_client()
        client.get("/festival-promotions")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        material = {"material_id": "111338", "material_name": "官方快消活动物料",
                    "start_time": "2026-10-10 00:00:00", "end_time": "2026-10-31 00:00:00"}
        with patch.dict(os.environ, self.env, clear=False), patch(
                "routes.festival_promotions.fetch_promoter_material_ids", return_value=[material]):
            response = client.post("/festival-promotions/materials", data={
                "csrf": csrf, "material_id": "111338", "festival_code": "double_11",
                "festival_year": "2026", "minimum_discount": "5", "interval_minutes": "60",
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("官方物料已加入节日监控", response.get_data(as_text=True))
        with db.connect() as conn:
            feed = conn.execute("SELECT * FROM festival_material_feeds").fetchone()
            source = conn.execute("SELECT * FROM sources WHERE id=?", (feed["source_id"],)).fetchone()
        self.assertEqual(feed["material_id"], "111338")
        self.assertEqual(feed["festival_code"], "double_11")
        self.assertEqual(search_config(source["url"])["material_id"], "111338")
