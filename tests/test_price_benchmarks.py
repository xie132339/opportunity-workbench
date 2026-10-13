"""Price benchmark management never fabricates samples or provider connections."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app as workbench
import db
from services.price_benchmark_evidence import price_claim_evidence
from services.source_provenance import source_provenance_evidence
from services.taobao_alliance import parse_item


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

    def test_page_starts_empty_and_uses_only_operator_maintained_price_bands(self):
        response = self.client.get("/price-benchmarks")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("维护价格基准", page)
        self.assertIn("不会调用第三方价格平台", page)
        self.assertIn("还没有维护价格带", page)
        self.assertNotIn("什么值得买历史价格 API", page)
        self.assertNotIn("历史线索覆盖", page)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintained_price_baselines").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)

    def test_page_read_does_not_mutate_existing_source_history(self):
        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources(platform,name,category,url,method,status,enabled)
                VALUES('测试入口','不会成为价格带','促销','https://feed-a.example/rss','rss','healthy',1)""").lastrowid
            event_id = conn.execute("""INSERT INTO events(source_id,external_key,title,url,snippet,fingerprint,published_at)
                VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (source_id, 'existing-event', '原始线索', 'https://feed.example/item', '原始内容', 'fixture')).lastrowid
            conn.execute("""INSERT INTO opportunities(event_id,source_id,title,category,topic,url)
                VALUES(?,?,?,'零售优惠','home',?)""",
                (event_id, source_id, '原始线索', 'https://feed.example/item'))
            before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ('events', 'opportunities', 'auto_reviews', 'quotes')}
        response = self.client.get("/price-benchmarks")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("这里由你维护具体品牌、商品、规格和单位的价格带", page)
        self.assertNotIn("原始线索", page)
        with db.connect() as conn:
            after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
        self.assertEqual(before, after)

    def test_page_lists_only_current_measurable_official_discount_as_maintenance_candidate(self):
        now = datetime.now(timezone(timedelta(hours=8)))
        item = {
            "num_iid": "123456789", "title": "可配置抽纸 3层 100抽10包",
            "item_url": "https://item.taobao.com/item.htm?id=123456789",
            "reserve_price": "30.00", "zk_final_price": "18.00",
            "coupon_amount": "", "shop_title": "官方店", "user_type": 0,
        }
        parsed = parse_item(item, now=now)
        self.assertIsNotNone(parsed)
        with db.connect() as conn:
            category_id = conn.execute("""INSERT INTO benchmark_categories
                (name,pricing_unit,identity_rule,topic_key,match_terms_json,policy_json,comparison_rule_json)
                VALUES('测试抽纸','元/100抽','品牌、层数、抽数、包数一致','home','[\"可配置抽纸\"]','{}','{}')""").lastrowid
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes,last_success)
                VALUES('淘宝联盟','测试官方物料','零售优惠','https://eco.taobao.com/router/rest?q=抽纸',
                       'taobao_alliance','taobao_alliance','healthy',1,60,CURRENT_TIMESTAMP)""").lastrowid
            event_id = conn.execute("""INSERT INTO events
                (source_id,external_key,title,url,metadata_json,fingerprint,last_seen_at)
                VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (source_id, parsed[0], parsed[1], parsed[2], json.dumps(parsed[5]), 'candidate-1')).lastrowid
            opportunity_id = conn.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,topic,url)
                VALUES(?,?,?,'零售优惠','other',?)""", (event_id, source_id, parsed[1], parsed[2])).lastrowid
            conn.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents)
                VALUES(?,'observed','官方优惠观察',1800)""", (opportunity_id,))
        response = self.client.get("/price-benchmarks")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("当前官方优惠中的维护候选", page)
        self.assertIn("可配置抽纸 3层 100抽10包", page)
        self.assertIn("测试抽纸", page)
        self.assertIn("官方推广价比接口参考价低", page)
        self.assertIn(f'/opportunities/{opportunity_id}', page)
        self.assertIn(f'/price-benchmarks?candidate={opportunity_id}#new-baseline', page)
        draft_page = self.client.get(f"/price-benchmarks?candidate={opportunity_id}").get_data(as_text=True)
        self.assertIn('正在使用官方候选', draft_page)
        self.assertIn('value="可配置抽纸 3层 100抽10包"', draft_page)
        self.assertIn(f'value="{category_id}" selected', draft_page)
        self.assertIn(f'淘宝联盟官方候选 #{opportunity_id}', draft_page)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM maintained_price_baselines").fetchone()[0], 0)

    def test_maintenance_candidate_card_fields_and_sorting(self):
        from services.maintained_price_candidates import load_maintained_price_candidates, spec_summary
        self.assertEqual(spec_summary("葵王物理压榨一级浓香花生油6.08L 大桶装家用食用油"), "6.08l")
        self.assertEqual(spec_summary("【洗衣液囤货装】网易严选酵素洗衣液6kg强效抗菌"), "6kg")
        self.assertIn("400ml", spec_summary("金龙鱼零反式脂肪玉米胚芽油400ml小瓶家用"))
        # 候选按降幅从大到小排序，且带规格摘要与预估到手字段
        now = datetime.now(timezone(timedelta(hours=8)))
        items = [
            {"num_iid": "200", "title": "大降商品 5kg 实惠装", "item_url": "https://item.taobao.com/item.htm?id=200",
             "reserve_price": "100.00", "zk_final_price": "50.00", "coupon_amount": "", "shop_title": "店A", "user_type": 0},
            {"num_iid": "201", "title": "小降商品 2kg 实惠装", "item_url": "https://item.taobao.com/item.htm?id=201",
             "reserve_price": "20.00", "zk_final_price": "18.00", "coupon_amount": "", "shop_title": "店B", "user_type": 0},
        ]
        with db.connect() as conn:
            for i, item in enumerate(items):
                parsed = parse_item(item, now=now)
                source_id = conn.execute("""INSERT INTO sources
                    (platform,name,category,url,method,parser,status,enabled,interval_minutes,last_success)
                    VALUES('淘宝联盟','测试候选%d','零售优惠','https://eco.taobao.com/router/rest?q=x%d',
                           'taobao_alliance','taobao_alliance','healthy',1,60,CURRENT_TIMESTAMP)""" % (i, i)).lastrowid
                event_id = conn.execute("""INSERT INTO events
                    (source_id,external_key,title,url,metadata_json,fingerprint,last_seen_at)
                    VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (source_id, parsed[0], parsed[1], parsed[2], json.dumps(parsed[5]), 'cand-%d' % i)).lastrowid
                conn.execute("""INSERT INTO opportunities
                    (event_id,source_id,title,category,topic,url)
                    VALUES(?,?,?,'零售优惠','other',?)""", (event_id, source_id, parsed[1], parsed[2])).lastrowid
                conn.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents)
                    VALUES(?,'observed','官方优惠观察',1800)""", (event_id,))
            rows = load_maintained_price_candidates(conn, [])
            self.assertGreaterEqual(len(rows), 1)
            if len(rows) >= 2:
                self.assertGreater(rows[0]["drop_cents"], rows[1]["drop_cents"])
            self.assertTrue(all(r["spec_summary"] for r in rows))
            self.assertTrue(all("drop_cents" in r and "price_cents" in r for r in rows))
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
        # Provenance remains an audit helper; the maintained-price page does
        # not use collector or publisher claims as a price baseline.
        with db.connect() as conn:
            after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
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

    def test_paused_source_peer_is_excluded_from_current_product_comparison(self):
        import json
        from comparison import load_comparisons

        with db.connect() as conn:
            source_ids=[]
            for platform, enabled in (("在用来源",1),("已暂停聚合源",0)):
                source_ids.append(conn.execute("""INSERT INTO sources
                    (platform,name,category,url,method,status,enabled,interval_minutes,last_success)
                    VALUES(?,?,'零售优惠',?,'rss','healthy',?,10,CURRENT_TIMESTAMP)""",
                    (platform,platform,f'https://{platform}.example/feed',enabled)).lastrowid)
            opportunity_ids=[]
            for index,source_id in enumerate(source_ids,1):
                metadata=json.dumps({'activity_links':['https://item.jd.com/987654321.html']})
                event_id=conn.execute("""INSERT INTO events
                    (source_id,external_key,title,url,snippet,metadata_json,fingerprint,published_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (source_id,f'peer-{index}','同款抽纸 100抽3层6包','https://feed.example/post/'+str(index),
                     '活动售价9.90元，下单1件，实付9.90元，该价格商品规格：100抽3层6包。',metadata,
                     f'peer-fingerprint-{index}', '2026-10-06 12:00:00')).lastrowid
                opportunity_ids.append(conn.execute("""INSERT INTO opportunities
                    (event_id,source_id,title,category,topic,url,resource_kind)
                    VALUES(?,?,?,'零售优惠','home',?,'purchase')""",
                    (event_id,source_id,'同款抽纸 100抽3层6包','https://feed.example/post/'+str(index))).lastrowid)
                conn.execute("""INSERT INTO auto_reviews
                    (opportunity_id,state,reason,advertised_cents,specification,detail_json)
                    VALUES(?,'observed','fixture',990,'100抽3层6包',?)""",
                    (opportunity_ids[-1],json.dumps({'conditions':'下单1件，实付9.90元，商品规格：100抽3层6包。'})))

            current=load_comparisons(conn,[opportunity_ids[0]])[opportunity_ids[0]]
            self.assertEqual([item['id'] for item in current['items']],[opportunity_ids[0]])

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

    def test_large_target_set_uses_active_corpus_without_sql_expression_overflow(self):
        from comparison import load_comparisons

        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,status,enabled,interval_minutes,last_success)
                VALUES('测试','大结果集','零售优惠','https://example.test/feed','rss','healthy',1,60,CURRENT_TIMESTAMP)""").lastrowid
            event_id = conn.execute("""INSERT INTO events
                (source_id,external_key,title,url,fingerprint,published_at,last_seen_at)
                VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (source_id, 'large-target', '大结果集测试商品', 'https://example.test/product',
                 'large-target', '2026-10-11 10:00:00')).lastrowid
            opportunity_id = conn.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,topic,url,resource_kind)
                VALUES(?,?,?,'零售优惠','home',?,'purchase')""",
                (event_id, source_id, '大结果集测试商品', 'https://example.test/product')).lastrowid
            conn.execute("INSERT INTO auto_reviews(opportunity_id,state,reason) VALUES(?,'observed','fixture')", (opportunity_id,))
            result = load_comparisons(conn, range(1, 102))
        self.assertIn(opportunity_id, result)

    def test_large_current_list_skips_optional_source_comparison(self):
        """A large ordinary list must not turn an optional diagnostic into a 500."""
        with db.connect() as conn:
            source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,status,enabled,interval_minutes,last_success)
                VALUES('测试','大结果集页面','零售优惠','https://example.test/feed','rss','healthy',1,60,CURRENT_TIMESTAMP)""").lastrowid
            for index in range(101):
                title = f"大结果集商品 {index}"
                event_id = conn.execute("""INSERT INTO events
                    (source_id,external_key,title,url,fingerprint,published_at,last_seen_at)
                    VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                    (source_id, f'page-{index}', title, f'https://example.test/product/{index}',
                     f'page-{index}')).lastrowid
                opportunity_id = conn.execute("""INSERT INTO opportunities
                    (event_id,source_id,title,category,topic,url,resource_kind)
                    VALUES(?,?,?,'零售优惠','home',?,'purchase')""",
                    (event_id, source_id, title, f'https://example.test/product/{index}')).lastrowid
                conn.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason)
                    VALUES(?,'observed','fixture')""", (opportunity_id,))

        # Source comparison is a diagnostic only.  If the normal large list
        # tries to call it, the mock makes the route fail immediately.
        with patch("routes.search.load_comparisons", side_effect=AssertionError("unexpected comparison")):
            response = self.client.get("/?view=current&buy_platform=all&layout=list")
            offers_response = self.client.get("/?view=offers&buy_platform=all&layout=list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(offers_response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("大结果集商品 100", page)
        self.assertIn("未运行来源之间的可选归并比较", page)
        self.assertIn("未运行来源之间的可选归并比较", offers_response.get_data(as_text=True))

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
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/price-benchmarks/categories", data={
            "csrf": "test-csrf", "name": "", "pricing_unit": "元/件", "identity_rule": "一致",
        })
        self.assertEqual(response.status_code, 302)
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM benchmark_categories WHERE name='未授权纸品'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM benchmark_categories").fetchone()[0], 9)


if __name__ == "__main__":
    unittest.main()
