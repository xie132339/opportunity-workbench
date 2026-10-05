"""Price benchmark management never fabricates samples or provider connections."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as workbench
import db
from services.price_benchmark_evidence import price_claim_evidence
from services.source_provenance import source_provenance_evidence


class PriceBenchmarkPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "isolated.sqlite3")
        self.db_patch.start()
        db.initialize()
        self.client = workbench.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = "test-csrf"

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_page_uses_existing_public_claims_without_api_or_trade_data(self):
        response = self.client.get("/price-benchmarks")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("不需要申请商业 API", page)
        self.assertIn("不生成高、中、低价格带", page)
        self.assertIn("个人买卖或交易", page)
        self.assertNotIn("什么值得买历史价格 API", page)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)

    def test_page_reports_append_only_source_claim_coverage_and_excludes_bad_rows(self):
        with db.connect() as conn:
            source_ids = []
            for platform, source_url in (("线报甲", "https://feed-a.example/rss"), ("线报乙", "https://feed-b.example/rss")):
                source_ids.append(conn.execute("""INSERT INTO sources(platform,name,category,url,method,status,enabled)
                    VALUES(?,?,'促销','%s','rss','healthy',1)""" % source_url,
                    (platform, platform)).lastrowid)
            conn.execute("""INSERT INTO sources(platform,name,category,url,method,status,enabled)
                VALUES('平台无报价','无报价入口','促销','https://feed-empty.example/rss','rss','healthy',1)""")
            fixtures = [
                (source_ids[0], "observed", "2026-10-04 08:00:00", 1200, "A商品"),
                (source_ids[0], "conditional", "2026-10-05 08:00:00", 900, "B商品"),
                (source_ids[1], "stale", "2026-10-04 09:00:00", 1100, "A商品"),
                (source_ids[1], "conflict", "2026-10-05 09:00:00", 100, "C商品"),
                (source_ids[1], "observed", None, 300, "无原文时间"),
                (source_ids[1], "stale", "2099-01-01 00:00:00", 500, "未来发布时间"),
            ]
            for index, (source_id, state, published, cents, title) in enumerate(fixtures):
                event_id = conn.execute("""INSERT INTO events(source_id,external_key,title,url,snippet,fingerprint,published_at)
                    VALUES(?,?,?,?,?,?,?)""", (source_id, str(index), title, "https://feed.example/item/"+str(index),
                    title, "fingerprint-"+str(index), published)).lastrowid
                opportunity_id = conn.execute("""INSERT INTO opportunities(event_id,source_id,title,category,topic,url)
                    VALUES(?,?,?,'零售优惠','home',?)""", (event_id, source_id, title,
                    "https://feed.example/item/"+str(index))).lastrowid
                conn.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents)
                    VALUES(?,?,?,?)""", (opportunity_id, state, "fixture", cents))
        response = self.client.get("/price-benchmarks")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("历史线索覆盖", page)
        self.assertIn("按大类管理比较与行情规则", page)
        self.assertIn("未来原文时间已排除", page)
        self.assertIn("全部来源平台覆盖", page)
        self.assertIn("平台无报价", page)
        self.assertIn("历史金额线索</th>", page)
        self.assertIn("家居日用（主题识别）", page)
        with db.connect() as conn:
            home = next(row for row in price_claim_evidence(conn)["topics"] if row["topic"] == "home")
            evidence = price_claim_evidence(conn)
            self.assertEqual((home["claim_rows"], home["observed"], home["conditional"], home["stale"],
                              home["platform_count"], home["published_day_count"]), (3, 1, 1, 1, 2, 2))
            self.assertEqual(evidence["future_excluded_rows"], 1)
            empty_platform = next(row for row in evidence["platforms"] if row["platform"] == "平台无报价")
            self.assertEqual((empty_platform["enabled_sources"], empty_platform["healthy_sources"],
                              empty_platform["claim_rows"]), (1, 1, 0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)

    def test_source_provenance_audit_separates_collector_publisher_and_product_links(self):
        with db.connect() as conn:
            source_ids = []
            for platform, name, route in (
                ("入口甲", "关键词订阅", "https://feed-a.example/rss"),
                ("入口乙", "综合订阅", "https://feed-b.example/rss"),
            ):
                source_ids.append(conn.execute("""INSERT INTO sources
                    (platform,name,category,url,method,status,enabled)
                    VALUES(?,?,'零售优惠',?,'rss','healthy',1)""",
                    (platform, name, route)).lastrowid)

            fixtures = [
                (source_ids[0], "a", "  纸巾 好价  ", "活动售价 12 元", "https://publisher-a.example/post/1",
                 '{"activity_links":["https://item.jd.com/123.html","javascript:alert(1)","https://[bad"]}', "observed", 1200),
                (source_ids[1], "b", "纸巾 好价", "活动售价   12 元", "https://publisher-b.example/post/2",
                 '{"activity_links":["https://item.jd.com/123.html?source=feed"]}', "conditional", 1100),
                # Same collector route again is retained, but does not count as another route.
                (source_ids[0], "c", "纸巾 好价", "活动售价 12 元", "https://publisher-a.example/post/3",
                 "{}", "stale", 1200),
            ]
            for source_id, key, title, snippet, content_url, metadata, state, cents in fixtures:
                event_id = conn.execute("""INSERT INTO events
                    (source_id,external_key,title,url,snippet,metadata_json,fingerprint,published_at)
                    VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (source_id, key, title, content_url, snippet, metadata, key)).lastrowid
                opportunity_id = conn.execute("""INSERT INTO opportunities
                    (event_id,source_id,title,category,topic,url)
                    VALUES(?,?,?,'零售优惠','home',?)""",
                    (event_id, source_id, title, content_url)).lastrowid
                conn.execute("""INSERT INTO auto_reviews
                    (opportunity_id,state,reason,advertised_cents,specification,conditions,evidence)
                    VALUES(?,?,'fixture',?,'4包','满20减8','原文标题及正文')""",
                    (opportunity_id, state, cents))

            before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("events", "opportunities", "auto_reviews", "quotes")}
            provenance = source_provenance_evidence(conn)
        response = self.client.get("/price-benchmarks")
        with db.connect() as conn:
            after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("采集入口、原文页面与重复线索", page)
        self.assertIn("疑似同文", page)
        self.assertIn("publisher-a.example", page)
        self.assertIn("item.jd.com", page)
        self.assertNotIn("javascript:alert", page)
        self.assertEqual(before, after)
        self.assertEqual(provenance["published_event_rows"], 3)
        self.assertEqual((provenance["cross_collector_text_groups"],
                          provenance["cross_collector_text_rows"]), (1, 3))
        self.assertEqual((provenance["eligible_overlap_groups"],
                          provenance["eligible_overlap_rows"]), (1, 3))
        self.assertEqual((provenance["cross_platform_label_groups"],
                          provenance["cross_platform_label_rows"]), (1, 3))
        group = provenance["overlap_groups"][0]
        self.assertEqual(group["collector_count"], 2)
        self.assertEqual(set(group["content_hosts"]), {"publisher-a.example", "publisher-b.example"})
        linked = next(item for item in group["records"] if item["outbound_links"])
        self.assertEqual(linked["outbound_links"][0]["host"], "item.jd.com")

    def test_category_create_and_update_only_change_configuration(self):
        response = self.client.post("/price-benchmarks/categories", data={
            "csrf": "test-csrf", "name": "测试纸品", "pricing_unit": "元/100抽",
            "identity_rule": "品牌、层数、抽数一致", "topic_key": "home",
            "match_terms": "可配置抽纸，可配置纸巾",
            "identity_mode": "merchant_id_only", "unit_mode": "count", "count_unit": "抽",
            "max_source_age_minutes": "45", "minimum_comparable_offers": "3",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM benchmark_categories WHERE name='测试纸品'").fetchone()
            self.assertIsNotNone(row)
            category_id = row["id"]
            self.assertEqual((row["topic_key"], __import__("json").loads(row["match_terms_json"]), row["version"]),
                             ("home", ["可配置抽纸", "可配置纸巾"], 1))
            policy = __import__("json").loads(row["policy_json"])
            self.assertEqual((policy["max_source_age_minutes"], policy["minimum_comparable_offers"]), (45, 3))
            comparison_rule = __import__("json").loads(row["comparison_rule_json"])
            self.assertEqual((comparison_rule["identity_mode"],comparison_rule["unit_mode"],
                              comparison_rule["count_unit"]),("merchant_id_only","count","抽"))
        response = self.client.post("/price-benchmarks/categories", data={
            "csrf": "test-csrf", "category_id": str(category_id), "name": "测试纸品新",
            "pricing_unit": "元/100抽", "identity_rule": "品牌、层数、抽数、包数一致",
            "topic_key": "home", "match_terms": "可配置卷纸",
            "identity_mode": "merchant_or_exact_title", "unit_mode": "mass", "count_unit": "",
            "max_source_age_minutes": "30", "minimum_comparable_offers": "4",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM benchmark_categories WHERE name='测试纸品'").fetchone())
            row = conn.execute("SELECT * FROM benchmark_categories WHERE name='测试纸品新'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual((__import__("json").loads(row["match_terms_json"]), row["version"]), (["可配置卷纸"], 2))
            self.assertEqual(__import__("json").loads(row["comparison_rule_json"])["unit_mode"],"mass")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM benchmark_category_rule_audit WHERE category_id=?",
                                          (category_id,)).fetchone()[0], 2)
            from services.category_policy import match_category
            self.assertEqual(match_category("某牌可配置卷纸", "home", [dict(row)])['name'], "测试纸品新")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)

    def test_category_comparison_rule_flows_from_database_into_product_search(self):
        import json
        from comparison import load_comparisons
        from services.category_comparison_rules import encode_rule
        from services.category_policy import encode_policy

        with db.connect() as conn:
            category_id = conn.execute("""INSERT INTO benchmark_categories
                (name,pricing_unit,identity_rule,topic_key,match_terms_json,policy_json,comparison_rule_json)
                VALUES(?,?,?,?,?,?,?)""", ("动态质量品类", "元/克", "品牌与规格一致", "home",
                json.dumps(["可比较坚果"], ensure_ascii=False), encode_policy({
                    "schema_version": 1, "max_source_age_minutes": 120,
                    "minimum_comparable_offers": 2, "reference": {
                        "enabled": False, "price_basis": "merchant_public", "window_days": None,
                        "minimum_independent_sources": None, "below_reference_percent": None,
                    }}), encode_rule({"schema_version": 1, "identity_mode": "merchant_or_exact_title",
                                      "unit_mode": "mass", "count_unit": ""}))).lastrowid
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,status,enabled,interval_minutes,last_success)
                VALUES('测试源','测试源','零售优惠','https://feed.example/rss','rss','healthy',1,10,CURRENT_TIMESTAMP)""").lastrowid
            event_id = conn.execute("""INSERT INTO events
                (source_id,external_key,title,url,snippet,fingerprint,published_at,last_seen_at)
                VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""", (source_id, "dynamic-rule",
                "可比较坚果 400克", "https://new.ixbk.net/test/100.html",
                "活动售价19.90元，下单1件，实付19.90元，该价格商品规格：400克。", "dynamic-rule-fingerprint")).lastrowid
            opportunity_id = conn.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,topic,url,resource_kind)
                VALUES(?,?,?,'零售优惠','home',?,'purchase')""", (event_id, source_id,
                "可比较坚果 400克", "https://new.ixbk.net/test/100.html")).lastrowid
            conn.execute("""INSERT INTO auto_reviews
                (opportunity_id,state,reason,advertised_cents,specification,detail_json)
                VALUES(?,'observed','fixture',1990,'400克',?)""", (opportunity_id,
                json.dumps({"conditions": "活动售价19.90元，下单1件，实付19.90元，该价格商品规格：400克。"}, ensure_ascii=False)))

            result = load_comparisons(conn)[opportunity_id]
            self.assertEqual(result["unit_mode"], "mass")
            self.assertEqual(result["resolved_category_name"], "动态质量品类")
            self.assertEqual(result["items"][0]["normalized_base_unit"], "g")
            self.assertEqual(result["items"][0]["normalized_unit_price"], __import__("fractions").Fraction(199, 40))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM benchmark_categories WHERE id=?", (category_id,)).fetchone()[0], 1)

    def test_topic_policy_is_validated_versioned_and_audited(self):
        response = self.client.post("/price-benchmarks/rules/home", data={
            "csrf": "test-csrf", "max_source_age_minutes": "45",
            "minimum_comparable_offers": "3", "price_basis": "merchant_public",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            row = conn.execute("SELECT policy_json,version FROM benchmark_topic_rules WHERE topic_key='home'").fetchone()
            policy = __import__("json").loads(row["policy_json"])
            self.assertEqual((row["version"],policy["max_source_age_minutes"],
                              policy["minimum_comparable_offers"]),(2,45,3))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM benchmark_topic_rule_audit WHERE topic_key='home'").fetchone()[0],1)
            before = row["policy_json"]
        # A half-configured reference cannot be enabled and must not replace the last valid rule.
        response = self.client.post("/price-benchmarks/rules/home", data={
            "csrf": "test-csrf", "max_source_age_minutes": "45",
            "minimum_comparable_offers": "3", "reference_enabled": "1",
            "price_basis": "source_claim",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            after = conn.execute("SELECT policy_json,version FROM benchmark_topic_rules WHERE topic_key='home'").fetchone()
            self.assertEqual((after["policy_json"],after["version"]),(before,2))
        # Even syntactically valid thresholds are rejected until an engine consumes them.
        response = self.client.post("/price-benchmarks/rules/home", data={
            "csrf": "test-csrf", "max_source_age_minutes": "45",
            "minimum_comparable_offers": "3", "reference_enabled": "1",
            "price_basis": "independent_index", "window_days": "30",
            "minimum_independent_sources": "3", "below_reference_percent": "20",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            after = conn.execute("SELECT policy_json,version FROM benchmark_topic_rules WHERE topic_key='home'").fetchone()
            self.assertEqual((after["policy_json"],after["version"]),(before,2))

    def test_invalid_comparison_rule_does_not_create_category(self):
        response=self.client.post("/price-benchmarks/categories",data={
            "csrf":"test-csrf","name":"不支持的换算","pricing_unit":"元/件",
            "identity_rule":"严格同款","topic_key":"home","match_terms":"测试",
            "identity_mode":"fuzzy_title","unit_mode":"count","count_unit":"任意单位",
            "max_source_age_minutes":"60","minimum_comparable_offers":"2"})
        self.assertEqual(response.status_code,302)
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM benchmark_categories WHERE name='不支持的换算'").fetchone())

    def test_rule_schema_migration_is_idempotent_and_preserves_business_rows(self):
        tables=('events','opportunities','auto_reviews','quotes','benchmark_categories')
        with db.connect() as conn:
            before={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in tables}
            db.initialize_benchmark_rules(conn)
            db.initialize_benchmark_rules(conn)
            after={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in tables}
            columns={row[1] for row in conn.execute('PRAGMA table_info(benchmark_categories)')}
            self.assertEqual(before,after)
            self.assertIn('comparison_rule_json',columns)
            from services.category_comparison_rules import decode_rule
            rules=[decode_rule(row[0]) for row in conn.execute('SELECT comparison_rule_json FROM benchmark_categories')]
            self.assertTrue(rules)
            self.assertTrue(all(error is None for _,error in rules))
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0],'ok')

    def test_watch_toggle_does_not_claim_provider_connected(self):
        with db.connect() as conn:
            provider_id = conn.execute("SELECT id FROM benchmark_providers WHERE provider_key='smzdm_history'").fetchone()[0]
        response = self.client.post(f"/price-benchmarks/providers/{provider_id}/watch", data={
            "csrf": "test-csrf", "watched": "0",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            provider = conn.execute("SELECT watched,access_state,sample_count FROM benchmark_providers WHERE id=?", (provider_id,)).fetchone()
            self.assertEqual(tuple(provider), (0, "not_connected", 0))

    def test_csrf_and_invalid_category_do_not_write(self):
        response = self.client.post("/price-benchmarks/categories", data={
            "name": "未授权纸品", "pricing_unit": "元/件", "identity_rule": "一致",
        })
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/price-benchmarks/categories", data={
            "csrf": "test-csrf", "name": "", "pricing_unit": "元/件", "identity_rule": "一致",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM benchmark_categories WHERE name='未授权纸品'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM benchmark_categories").fetchone()[0], 9)


if __name__ == "__main__":
    unittest.main()
