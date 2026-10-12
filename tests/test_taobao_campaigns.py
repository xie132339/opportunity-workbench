import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import scanner
from app import app
from services.taobao_alliance import (ACTIVITY_INFO_METHOD, MATERIAL_IDS_METHOD,
                                      fetch_activity_info, fetch_promoter_material_ids,
                                      parse_item, search_config)
from services.taobao_campaigns import (campaign_copy, campaign_window, create_campaign,
                                       material_source_url, refresh_campaign)

CHINA = timezone(timedelta(hours=8))


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._body = json.dumps(payload).encode("utf-8")

    def iter_content(self, _size):
        yield self._body

    def close(self):
        pass


class TaobaoCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "campaign.sqlite3"
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

    def test_campaign_keeps_activity_and_product_material_ids_separate(self):
        with db.connect() as conn:
            campaign_id = create_campaign(
                conn, name="十一官方会场", activity_material_id="987654321",
                material_id="27939", interval_minutes=60, env=self.env,
            )
            campaign = conn.execute("SELECT * FROM taobao_campaigns WHERE id=?", (campaign_id,)).fetchone()
            source = conn.execute("SELECT * FROM sources WHERE id=?", (campaign["source_id"],)).fetchone()
        self.assertEqual(campaign["activity_material_id"], "987654321")
        self.assertEqual(campaign["material_id"], "27939")
        self.assertEqual(search_config(source["url"])["material_id"], "27939")
        self.assertNotIn("987654321", source["url"])
        self.assertEqual(search_config(material_source_url("27939"))["q"], "双11")

    def test_activity_api_uses_exact_official_method_and_does_not_report_secret(self):
        received = {}
        payload = {"tbk_activity_info_get_response": {"data": {
            "page_name": "十一官方会场", "page_start_time": "2026-10-20 20:00:00",
            "page_end_time": "2026-11-11 23:59:59",
            "click_url": "https://s.click.taobao.com/example",
            "short_click_url": "https://s.click.taobao.com/short",
        }}}

        def http_get(_url, **kwargs):
            received.update(kwargs["params"])
            return _Response(payload)

        report = {}
        result = fetch_activity_info("987654321", http_get=http_get, env=self.env,
                                     now=datetime(2026, 10, 12, 9, tzinfo=CHINA), report=report)
        self.assertEqual(received["method"], ACTIVITY_INFO_METHOD)
        self.assertEqual(received["activity_material_id"], "987654321")
        self.assertEqual(received["adzone_id"], "123456789")
        self.assertEqual(result["page_name"], "十一官方会场")
        self.assertEqual(result["short_promotion_url"], "https://s.click.taobao.com/short")
        self.assertNotIn("test-secret-never-persisted", json.dumps(report, ensure_ascii=False))
        self.assertNotIn("test-secret-never-persisted", json.dumps(result["raw_payload"], ensure_ascii=False))

    def test_promoter_material_discovery_uses_official_promotion_products_and_validates_ids(self):
        received = {}
        now = datetime(2026, 10, 12, 9, tzinfo=CHINA)
        payload = {"tbk_optimus_tou_material_ids_get_response": {
            "total_count": 3, "data": {"tou_materials": [
                {"material_id": 111338, "material_name": "快消第二波淘礼金补贴货盘",
                 "material_type": 1, "subject": 1, "start_time": 1791561600,
                 "end_time": 1793376000},
                {"material_id": "111338", "material_name": "重复项",
                 "material_type": 1, "subject": 1},
                {"material_id": 222, "material_name": "不接受的权益物料",
                 "material_type": 2, "subject": 1},
            ]},
        }}

        def request(method, params, **_kwargs):
            received.update(method=method, params=params)
            return payload

        with patch("services.taobao_alliance._request_top_payload", side_effect=request):
            report = {}
            materials = fetch_promoter_material_ids(env=self.env, now=now, report=report)
        self.assertEqual(received["method"], MATERIAL_IDS_METHOD)
        query = json.loads(received["params"]["material_query"])
        self.assertEqual(query, {"page_no": 1, "subject": 1, "material_type": 1, "page_size": 100})
        self.assertEqual([item["material_id"] for item in materials], ["111338"])
        self.assertEqual(materials[0]["material_name"], "快消第二波淘礼金补贴货盘")
        self.assertEqual(materials[0]["start_time"], "2026-10-10 00:00:00")
        self.assertEqual(report["returned_count"], 1)

    def test_official_rights_material_zero_response_is_empty(self):
        payload = {"tbk_optimus_tou_material_ids_get_response": {"total_count": 0}}
        captured = {}
        def request(method, params, **_kwargs):
            captured.update(json.loads(params["material_query"]))
            return payload
        with patch("services.taobao_alliance._request_top_payload", side_effect=request):
            rows = fetch_promoter_material_ids(material_type=2, env=self.env)
        self.assertEqual(rows, [])
        self.assertEqual(captured["material_type"], 2)

    def test_refresh_and_date_only_window_do_not_claim_an_unproven_active_time(self):
        with db.connect() as conn:
            campaign_id = create_campaign(conn, name="十一官方会场", activity_material_id="987654321", env=self.env)
        info = {
            "activity_material_id": "987654321", "page_name": "十一官方会场",
            "page_start_time": "2026-10-12", "page_end_time": "2026-10-12",
            "promotion_url": "https://s.click.taobao.com/example", "short_promotion_url": "",
            "raw_payload": {"tbk_activity_info_get_response": {"data": {}}},
        }
        with patch("services.taobao_campaigns.fetch_activity_info", return_value=info):
            result = refresh_campaign(campaign_id, env=self.env,
                                      now=datetime(2026, 10, 12, 10, tzinfo=CHINA))
        self.assertEqual(result["status"], "verified")
        with db.connect() as conn:
            campaign = dict(conn.execute("SELECT * FROM taobao_campaigns WHERE id=?", (campaign_id,)).fetchone())
        self.assertEqual(campaign_window(campaign, now=datetime(2026, 10, 12, 12))["state"], "date_range")
        copy = campaign_copy(campaign)
        self.assertIn("最终支付金额以淘宝页面或结算页为准", copy)
        self.assertNotIn("全网最低", copy)

    def test_missing_credentials_marks_campaign_failed_without_leaking_or_crashing(self):
        with db.connect() as conn:
            campaign_id = create_campaign(conn, name="十一官方会场", activity_material_id="987654321", env=self.env)
        result = refresh_campaign(campaign_id, env={})
        self.assertEqual(result["status"], "failed")
        self.assertIn("缺少服务端配置", result["error"])

    def test_campaign_route_renders_without_creating_fake_activity_data(self):
        client = app.test_client()
        page = client.get("/taobao-campaigns")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("官方活动 ID", html)
        self.assertIn("商品物料 ID", html)
        self.assertIn("只填活动 ID 时", html)

    @staticmethod
    def _item(now, item_id, title, reserve, promotion):
        return {
            "num_iid": item_id, "title": title,
            "item_url": f"https://item.taobao.com/item.htm?id={item_id}",
            "reserve_price": reserve, "zk_final_price": promotion,
            "coupon_amount": "", "shop_title": "测试官方店", "user_type": 0,
            "coupon_start_time": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "coupon_end_time": (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def test_default_list_ranks_by_one_official_discount_path_then_rate(self):
        now = datetime.now(CHINA)
        items = [
            self._item(now, "101", "优惠额同额低比例纸巾", "30", "20"),
            self._item(now, "102", "优惠额同额高比例纸巾", "20", "10"),
            self._item(now, "103", "优惠额较低纸巾", "15", "10"),
        ]
        rows = [parse_item(item, now=now) for item in items]
        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES('淘宝联盟','排序测试','零售优惠','https://eco.taobao.com/router/rest?q=纸巾',
                       'taobao_alliance','taobao_alliance','healthy',1,60)""").lastrowid
        with patch("scanner.taobao_alliance_rows", return_value=rows):
            scanner.scan_source(source_id)
        client = app.test_client()
        page = client.get("/?q=纸巾&buy_platform=taobao&layout=list").get_data(as_text=True)
        self.assertLess(page.index("优惠额同额高比例纸巾"), page.index("优惠额同额低比例纸巾"))
        self.assertLess(page.index("优惠额同额低比例纸巾"), page.index("优惠额较低纸巾"))
        self.assertIn("sort=discount_rate_high", page)
        self.assertIn("约 50.0%", page)
