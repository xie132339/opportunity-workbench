import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import scanner
from app import app
from services.festival_promotions import (create_festival, festival_products,
                                          list_festivals)
from services.taobao_alliance import parse_item


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
