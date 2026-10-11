"""Tests for operator-maintained, fixed product price bands."""
from datetime import date, datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as workbench
import db
from services.maintained_price_baselines import (
    assess_maintained_baseline,
    load_baselines,
    validate_baseline,
)
from services.taobao_alliance import parse_item


CHINA = timezone(timedelta(hours=8))


def baseline(**changes):
    value = {
        "id": 1,
        "category_id": 1,
        "category_name": "纸品",
        "topic_key": "home",
        "category_match_terms": ["抽纸"],
        "name": "清风原木 3层抽纸",
        "match_terms": ["清风", "原木", "3层"],
        "unit_mode": "count",
        "count_unit": "抽",
        "base_quantity": 100,
        "exact_specification": "",
        "bargain_cents": 150,
        "low_cents": 180,
        "typical_cents": 220,
        "high_cents": 260,
        "effective_from": "2026-01-01",
        "enabled": 1,
        "version": 1,
    }
    value.update(changes)
    return value


def official_quote(amount_cents):
    return {"kind": "official_promotion_observation", "amount_cents": amount_cents}


class MaintainedPriceBaselineAssessmentTests(unittest.TestCase):
    def test_count_rule_normalizes_a_multi_pack_and_marks_bargain(self):
        result = assess_maintained_baseline(
            {"title": "清风 原木 3层抽纸 100抽×6包", "display_topic": "home"},
            {}, official_quote(900), [baseline()], today=date(2026, 10, 11),
        )
        self.assertEqual(result["state"], "maintained_bargain")
        self.assertEqual(result["current_unit_cents"], 150)
        self.assertEqual(result["typical_delta_cents"], 70)
        self.assertEqual(result["baseline_version"], 1)
        self.assertIn("按标题明确数量归一", result["matched_by"])

    def test_volume_rule_uses_maintained_base_quantity(self):
        water = baseline(
            id=2,
            category_id=3,
            category_name="乳品饮料",
            topic_key="food",
            category_match_terms=["矿泉水"],
            name="农夫山泉 550毫升",
            match_terms=["农夫山泉", "550ml"],
            unit_mode="volume",
            count_unit="",
            base_quantity=1000,
            bargain_cents=170,
            low_cents=190,
            typical_cents=230,
            high_cents=270,
        )
        result = assess_maintained_baseline(
            {"title": "农夫山泉 天然矿泉水 550ml×24瓶", "display_topic": "food"},
            {}, official_quote(2400), [water], today=date(2026, 10, 11),
        )
        self.assertEqual(result["state"], "maintained_low")
        self.assertEqual(result["current_unit_cents"], 182)
        self.assertEqual(result["unit_label"], "每 1000 毫升")

    def test_fixed_spec_ignores_spacing_and_multiplication_spelling(self):
        item = baseline(
            id=3,
            category_id=4,
            category_name="手机数码",
            topic_key="electronics",
            category_match_terms=["耳机"],
            name="苹果耳机固定型号",
            match_terms=["苹果", "耳机"],
            unit_mode="exact_spec",
            count_unit="",
            base_quantity=1,
            exact_specification="耳机 Pro 2代 USB-C",
            bargain_cents=150000,
            low_cents=160000,
            typical_cents=180000,
            high_cents=200000,
        )
        result = assess_maintained_baseline(
            {"title": "苹果耳机 Pro 2代 USB-C", "display_topic": "electronics"},
            {}, official_quote(150000), [item], today=date(2026, 10, 11),
        )
        self.assertEqual(result["state"], "maintained_bargain")

    def test_missing_measurement_or_multiple_matches_never_create_a_verdict(self):
        missing = assess_maintained_baseline(
            {"title": "清风 原木 3层抽纸 多规格可选", "display_topic": "home"},
            {}, official_quote(900), [baseline()], today=date(2026, 10, 11),
        )
        self.assertEqual(missing["state"], "unit_unavailable")
        duplicate = assess_maintained_baseline(
            {"title": "清风 原木 3层抽纸 100抽×6包", "display_topic": "home"},
            {}, official_quote(900), [baseline(), baseline(id=9, name="重复规则")], today=date(2026, 10, 11),
        )
        self.assertEqual(duplicate["state"], "ambiguous")
        non_official = assess_maintained_baseline(
            {"title": "清风 原木 3层抽纸 100抽×6包", "display_topic": "home"},
            {}, {"kind": "source_claim", "amount_cents": 900}, [baseline()], today=date(2026, 10, 11),
        )
        self.assertEqual(non_official["state"], "not_official_promotion")

    def test_validation_rejects_inverted_bands(self):
        raw = {
            "category_id": 1, "name": "测试商品", "match_terms": ["测试", "商品"],
            "unit_mode": "count", "count_unit": "包", "base_quantity": 1,
            "exact_specification": "", "bargain_cents": 200, "low_cents": 180,
            "typical_cents": 220, "high_cents": 260, "effective_from": "2026-10-11",
            "enabled": True, "notes": "测试维护价格带",
        }
        with self.assertRaisesRegex(ValueError, "价格带"):
            validate_baseline(raw)


class MaintainedPriceBaselineRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "isolated.sqlite3")
        self.db_patch.start()
        db.initialize()
        self.client = workbench.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = "test-csrf"
        with db.connect() as conn:
            self.paper_category_id = conn.execute(
                "SELECT id FROM benchmark_categories WHERE name='纸品'"
            ).fetchone()[0]

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def form_data(self, **changes):
        data = {
            "csrf": "test-csrf", "category_id": str(self.paper_category_id),
            "name": "清风原木 3层抽纸", "match_terms": "清风，原木，3层",
            "unit_mode": "count", "count_unit": "抽", "base_quantity": "100",
            "exact_specification": "", "bargain_amount": "1.50", "low_amount": "1.80",
            "typical_amount": "2.20", "high_amount": "2.60",
            "effective_from": "2026-01-01", "enabled": "1", "notes": "平台内维护的纸品基准",
        }
        data.update(changes)
        return data

    def test_create_update_disable_and_audit_are_versioned(self):
        response = self.client.post("/price-benchmarks/maintained", data=self.form_data())
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            saved = conn.execute("SELECT * FROM maintained_price_baselines").fetchone()
            self.assertEqual((saved["bargain_cents"], saved["version"]), (150, 1))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintained_price_baseline_audit").fetchone()[0], 1)
            baseline_id = saved["id"]
        response = self.client.post("/price-benchmarks/maintained", data=self.form_data(
            baseline_id=str(baseline_id), low_amount="1.90", notes="修订后的低位价格"
        ))
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            saved = conn.execute("SELECT * FROM maintained_price_baselines WHERE id=?", (baseline_id,)).fetchone()
            self.assertEqual((saved["low_cents"], saved["version"]), (190, 2))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintained_price_baseline_audit WHERE baseline_id=?", (baseline_id,)).fetchone()[0], 2)
            self.assertEqual(len(load_baselines(conn)), 1)
        self.client.post("/price-benchmarks/maintained", data=self.form_data(
            baseline_id=str(baseline_id), low_amount="1.90", notes="修订后的低位价格"
        ))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT version FROM maintained_price_baselines WHERE id=?", (baseline_id,)).fetchone()[0], 2)
        self.client.post("/price-benchmarks/maintained", data=self.form_data(
            baseline_id=str(baseline_id), low_amount="1.90", notes="修订后的低位价格", enabled=""
        ))
        with db.connect() as conn:
            self.assertEqual(load_baselines(conn), [])
            self.assertEqual(conn.execute("SELECT version FROM maintained_price_baselines WHERE id=?", (baseline_id,)).fetchone()[0], 3)

    def test_targeted_migration_is_idempotent_and_keeps_business_rows(self):
        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources(platform,name,category,url,method)
                VALUES('测试入口','历史不改写','零售优惠','https://example.test/feed','rss')""").lastrowid
            event_id = conn.execute("""INSERT INTO events(source_id,external_key,title,url,fingerprint)
                VALUES(?,?,?,?,?)""", (source_id, "historic", "保留的历史机会", "https://example.test/item", "historic"),
            ).lastrowid
            conn.execute("""INSERT INTO opportunities(event_id,source_id,title,category,url)
                VALUES(?,?,?,'零售优惠',?)""", (event_id, source_id, "保留的历史机会", "https://example.test/item"))
            before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("events", "opportunities", "auto_reviews", "quotes")}
            db.initialize_maintained_price_baselines(conn)
            db.initialize_maintained_price_baselines(conn)
            after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(before, after)
            self.assertTrue({"maintained_price_baselines", "maintained_price_baseline_audit"} <= tables)
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_invalid_band_order_does_not_write(self):
        response = self.client.post("/price-benchmarks/maintained", data=self.form_data(
            bargain_amount="3.00", low_amount="1.80"
        ), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("价格带必须满足", response.get_data(as_text=True))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintained_price_baselines").fetchone()[0], 0)

    def test_missing_category_is_rejected_without_choosing_the_first_category(self):
        response = self.client.post("/price-benchmarks/maintained", data=self.form_data(
            category_id=""
        ), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("请选择所属品类", response.get_data(as_text=True))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintained_price_baselines").fetchone()[0], 0)

    def test_list_renders_a_matched_official_taobao_promotion(self):
        now = datetime.now(CHINA)
        item = {
            "num_iid": "123456789", "title": "清风 原木 3层抽纸 100抽*6包",
            "item_url": "https://item.taobao.com/item.htm?id=123456789",
            "reserve_price": "10.00", "zk_final_price": "8.00", "coupon_amount": "2.00",
            "coupon_start_fee": "8.00",
            "coupon_start_time": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "coupon_end_time": (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "coupon_remain_count": "20", "coupon_total_count": "100", "shop_title": "测试店", "user_type": 0,
        }
        parsed = parse_item(item, now=now)
        self.assertIsNotNone(parsed)
        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes,last_success)
                VALUES('淘宝联盟','清风抽纸','零售优惠','https://eco.taobao.com/router/rest?q=抽纸',
                       'taobao_alliance','taobao','healthy',1,60,CURRENT_TIMESTAMP)""").lastrowid
            event_id = conn.execute("""INSERT INTO events
                (source_id,external_key,title,url,snippet,metadata_json,fingerprint,published_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,NULL,CURRENT_TIMESTAMP)""",
                (source_id, parsed[0], parsed[1], parsed[2], parsed[3],
                 json.dumps(parsed[5], ensure_ascii=False), "maintained-test"),
            ).lastrowid
            opportunity_id = conn.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,topic,url,resource_kind,status)
                VALUES(?,?,?,'零售优惠','home',?,'purchase','pending')""",
                (event_id, source_id, parsed[1], parsed[2]),
            ).lastrowid
            conn.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents)
                VALUES(?,'observed','官方推广价',800)""", (opportunity_id,))
        without_baseline = self.client.get("/?q=清风&buy_platform=taobao&layout=list").get_data(as_text=True)
        self.assertIn("清风 原木 3层抽纸 100抽*6包", without_baseline)
        self.assertNotIn("按维护基准：可捡漏", without_baseline)
        self.client.post("/price-benchmarks/maintained", data=self.form_data())
        page = self.client.get("/?q=清风&buy_platform=taobao&layout=list").get_data(as_text=True)
        self.assertIn("清风 原木 3层抽纸 100抽*6包", page)
        self.assertIn("按维护基准：可捡漏", page)
        self.assertIn("清风原木 3层抽纸 v1", page)


if __name__ == "__main__":
    unittest.main()
