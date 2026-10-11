import json
import os
import tempfile
import unittest
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import scanner
import autoreview
import app as application_module
from app import app
from services.product_search import is_promotional_price_candidate, listing_quote
from services.taobao_alliance import (
    API_ENDPOINT, TaobaoAllianceAPIError, TaobaoAllianceConfigurationError, _signature, configuration_error,
    fetch_rows, parse_item, search_config, validated_offer,
)
from services.taobao_sku_detail import (
    DETAIL_METHOD, LEGACY_DETAIL_METHOD, SKU_DETAIL_METHODS,
    fetch_item_detail, parse_detail_payload,
)


CHINA = timezone(timedelta(hours=8))


def sample_item(now, *, reserve="10.00", promotion="8.00", coupon="2.00",
                threshold="8.00", starts=None, ends=None, remaining="20", item_id="123456789"):
    starts = starts or (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    ends = ends or (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "num_iid": item_id,
        "title": "测试抽纸 100抽*10包",
        "item_url": f"https://item.taobao.com/item.htm?id={item_id}",
        "reserve_price": reserve,
        "zk_final_price": promotion,
        "coupon_amount": coupon,
        "coupon_start_fee": threshold,
        "coupon_start_time": starts,
        "coupon_end_time": ends,
        "coupon_remain_count": remaining,
        "coupon_total_count": "100",
        "shop_title": "测试官方店",
        "user_type": 0,
    }


class TaobaoAllianceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=CHINA)

    def test_entry_url_is_exact_official_endpoint_and_query_is_dynamic(self):
        url = "https://eco.taobao.com/router/rest?q=%E6%8A%BD%E7%BA%B8&start_price=5&end_price=50&is_tmall=0"
        self.assertEqual(search_config(url), {
            "q": "抽纸", "start_price": "5", "end_price": "50", "is_tmall": "false",
            "page_no": "1", "page_size": "100", "max_pages": "all",
        })
        for invalid in (
            "https://example.test/router/rest?q=抽纸",
            "http://eco.taobao.com/router/rest?q=抽纸",
            "https://eco.taobao.com/router/rest?q=抽纸&app_key=secret",
            "https://eco.taobao.com/router/rest?q=抽纸&q=纸巾",
            "https://eco.taobao.com/router/rest?q=抽纸&start_price=90&end_price=10",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                search_config(invalid)

    def test_price_drop_is_a_direct_taobao_product_and_coupon_is_not_stacked(self):
        record = parse_item(sample_item(self.now), now=self.now)
        self.assertIsNotNone(record)
        self.assertEqual(record[2], "https://item.taobao.com/item.htm?id=123456789")
        evidence = record[5]["taobao_alliance_offer"]
        self.assertEqual(evidence["reserve_price_cents"], 1000)
        self.assertEqual(evidence["promotion_price_cents"], 800)
        self.assertEqual(evidence["quote_price_cents"], 800)
        self.assertEqual(evidence["quote_basis"], "alliance_promotion_price")
        self.assertTrue(evidence["coupon_candidate"])
        self.assertIn("账号适用与结算扣减未核实", record[3])
        self.assertEqual(record[5]["taobao_alliance_source_fields"]["zk_final_price"], "8.00")

    def test_only_documented_taobao_ecosystem_seller_types_are_admitted(self):
        for seller_type in (None, 3, "", "unknown"):
            item = sample_item(self.now)
            if seller_type is None:
                item.pop("user_type")
            else:
                item["user_type"] = seller_type
            with self.subTest(seller_type=seller_type):
                self.assertIsNone(parse_item(item, now=self.now))
        tmall = sample_item(self.now)
        tmall.update(user_type=1, item_url="https://detail.tmall.com/item.htm?id=123456789")
        row = parse_item(tmall, now=self.now)
        self.assertIsNotNone(row)
        self.assertEqual(row[2], "https://detail.tmall.com/item.htm?id=123456789")
        self.assertEqual(row[5]["taobao_alliance_offer"]["marketplace_key"], "tmall")

    def test_coupon_only_item_requires_current_coupon_and_computes_conditional_one_item_price(self):
        row = parse_item(sample_item(self.now, reserve="10", promotion="10", coupon="2",
                                     threshold="8"), now=self.now)
        self.assertIsNotNone(row)
        evidence = row[5]["taobao_alliance_offer"]
        self.assertEqual(evidence["quote_price_cents"], 800)
        self.assertEqual(evidence["quote_basis"], "single_coupon_conditional_estimate")
        self.assertEqual(evidence["coupon_applied_cents"], 200)
        self.assertIn("试算券后价¥8.00", row[3])

    def test_quotes_survive_without_promotions_but_wrong_product_is_rejected(self):
        self.assertFalse(parse_item(sample_item(self.now, reserve="10", promotion="10", coupon=""), now=self.now)[5]["taobao_alliance_offer"]["price_drop_candidate"])
        expired = sample_item(self.now, reserve="10", promotion="10", coupon="2",
                              starts="2026-10-01 00:00:00", ends="2026-10-02 00:00:00")
        self.assertEqual(parse_item(expired, now=self.now)[5]["taobao_alliance_offer"]["coupon_state"], "expired")
        unknown_inventory = sample_item(self.now, reserve="10", promotion="10", coupon="2",
                                        threshold="8", remaining="")
        self.assertEqual(parse_item(unknown_inventory, now=self.now)[5]["taobao_alliance_offer"]["coupon_state"], "remaining_unknown")
        threshold_not_met = sample_item(self.now, reserve="10", promotion="7", coupon="2", threshold="9")
        parsed = parse_item(threshold_not_met, now=self.now)
        self.assertIsNotNone(parsed)  # The actual API promotion is still below reserve.
        self.assertFalse(parsed[5]["taobao_alliance_offer"]["coupon_candidate"])
        wrong_id = sample_item(self.now)
        wrong_id["item_url"] = "https://item.taobao.com/item.htm?id=999999999"
        self.assertIsNone(parse_item(wrong_id, now=self.now))

    def test_signature_is_hmac_md5_over_sorted_top_parameter_pairs(self):
        params = {"timestamp": "2026-10-08 12:00:00", "method": "taobao.tbk.dg.material.optional",
                  "app_key": "123"}
        self.assertEqual(_signature(params, "secret"), "E5371F23520F84C7B5AC3AF87F42D114")

    def test_detailed_api_binds_price_to_exact_in_stock_sku(self):
        start = int((self.now - timedelta(hours=1)).timestamp() * 1000)
        end = int((self.now + timedelta(hours=1)).timestamp() * 1000)
        sku = {
                "sku_id": 9001, "quantity": "8",
                "property_list": {"tbk_item_detail_sku_prop": [
                    {"property_text": "规格", "value_text": "100抽×10包"},
                    {"property_text": "层数", "value_alias_text": "4层"},
                ]},
                "sku_price_promotion_info": {
                    "sku_zk_final_price": "10.00", "sku_final_promotion_price": "8.00",
                    "sku_final_promotion_path_list": {"sku_final_promotion_path_map_data": [{
                        "promotion_title": "商品券", "promotion_desc": "减2元",
                        "promotion_fee": "2.00", "promotion_start_time": str(start),
                        "promotion_end_time": str(end),
                    }]},
                },
            }
        item = {"input_item_iid": "opaque-new-id", "item_id": "123456789", "item_basic_info": {"title": "测试抽纸"},
                "sku_list": {"tbk_item_detail_sku": [sku]}}
        payload = {"tbk_item_details_upgrade_get_response": {
            "results": {"tbk_item_detail": [item]}}}
        detail = parse_detail_payload(payload, "opaque-new-id", now=self.now)
        self.assertEqual(detail["field_state"], "bound")
        self.assertEqual(detail["sku_id"], "9001")
        self.assertEqual(detail["specification"], "规格：100抽×10包；层数：4层")
        self.assertEqual(detail["advertised_cents"], 800)
        self.assertTrue(detail["promotional_candidate"])
        source = parse_item(sample_item(self.now), now=self.now)
        display_row = {"source_method": "taobao_alliance", "auto_state": "observed",
                       "metadata_json": json.dumps(source[5]), "detail_json": json.dumps(detail)}
        display_row["display_quote"] = listing_quote(display_row, {})
        self.assertEqual(display_row["display_quote"]["amount_cents"], 800)
        self.assertTrue(is_promotional_price_candidate(display_row, {}))

    def test_direct_sku_price_drop_is_complete_without_a_coupon_path(self):
        from services.taobao_sync_validation import _missing_evidence
        detail = {
            "field_state": "bound", "sku_id": "9001", "specification": "规格：100抽×10包",
            "quantity_observed": 8, "advertised_cents": 800, "public_sale_cents": 800,
            "reference_cents": 1000, "promotion_path": [], "promotional_candidate": True,
            "link_identity": {"item_id": "opaque-new-id"},
        }
        self.assertEqual(_missing_evidence({"taobao_alliance_offer": {"item_id": "opaque-new-id"}}, detail), [])

    def test_sole_sku_without_properties_binds_price_but_is_not_comparable(self):
        """A single returned SKU needs no guessed variant label to bind price."""
        sku = {
            "sku_id": 9001, "quantity": "8",
            "sku_final_price": "8.00", "sku_reserve_price": "10.00",
        }
        payload = {"tbk_item_details_get_response": {"results": {"tbk_item_detail": [{
            "input_item_iid": "opaque-new-id", "item_id": "123456789",
            "item_basic_info": {"title": "测试抽纸"},
            "sku_list": {"tbk_item_detail_sku": [sku]},
        }]}}}
        detail = parse_detail_payload(payload, "opaque-new-id", now=self.now)
        self.assertEqual(detail["field_state"], "bound")
        self.assertEqual(detail["sku_binding_mode"], "sole_sku")
        self.assertFalse(detail["specification_comparable"])
        self.assertEqual(detail["specification"], "官方唯一 SKU（未返回规格属性）")
        self.assertEqual(detail["advertised_cents"], 800)
        self.assertTrue(detail["promotional_candidate"])
        source = parse_item(sample_item(self.now), now=self.now)
        row = {"source_method": "taobao_alliance", "auto_state": "conditional",
               "metadata_json": json.dumps(source[5]), "detail_json": json.dumps(detail)}
        self.assertEqual(listing_quote(row, {})["amount_cents"], 800)
        from services.taobao_sync_validation import _record
        record = _record({"opportunity_id": 1, "state": "conditional", "reason": "",
                          "checked_at": None, "detail_checked_at": None, "next_check_at": None,
                          "metadata_json": json.dumps(source[5]), "detail_json": json.dumps(detail)})
        self.assertTrue(record["sku_evidence_complete"])
        self.assertEqual(record["comparison_missing"], ["可比规格"])
        self.assertFalse(record["comparison_ready"])

    def test_current_detail_product_level_response_stays_out_of_sku_quote(self):
        payload = {"tbk_item_details_upgrade_get_response": {"results": {"tbk_item_detail": [{
            "input_item_iid": "opaque-new-id", "item_id": "123456789",
            "item_basic_info": {"title": "抽纸100抽10包 8元"},
            "price_promotion_info": {"zk_final_price": "8.00"},
        }]}}}
        detail = parse_detail_payload(payload, "opaque-new-id", now=self.now)
        self.assertEqual(detail["field_state"], "field_not_provided")
        self.assertEqual(detail["specification"], "")
        self.assertIsNone(detail["advertised_cents"])
        self.assertTrue(detail["product_level_price_observed"])
        self.assertEqual(detail["item_id"], "123456789")
        self.assertIn("商品级信息", detail["field_reason"])
        self.assertFalse(detail["promotional_candidate"])

    def test_detailed_api_request_uses_sku_capable_methods_and_never_sends_secret(self):
        payload = {"tbk_item_details_upgrade_get_response": {"results": {"tbk_item_detail": []}}}
        class Response:
            status_code = 200
            def iter_content(self, size): yield json.dumps(payload).encode()
            def close(self): pass
        calls = []
        def get(url, **options):
            calls.append((url, options))
            return Response()
        env = {"TAOBAO_ALLIANCE_APP_KEY": "123", "TAOBAO_ALLIANCE_APP_SECRET": "secret",
               "TAOBAO_ALLIANCE_ADZONE_ID": "456"}
        report = {}
        detail = fetch_item_detail("opaque-new-id", http_get=get, env=env, now=self.now, report=report)
        self.assertEqual(detail["field_state"], "field_not_provided")
        self.assertEqual(calls[0][1]["params"]["method"], DETAIL_METHOD)
        self.assertEqual(calls[0][1]["params"]["biz_scene_id"], "1")
        self.assertEqual(calls[0][1]["params"]["get_topn_rate"], "0")
        self.assertNotIn("secret", json.dumps(calls))
        self.assertEqual([call[1]["params"]["method"] for call in calls], list(SKU_DETAIL_METHODS))
        self.assertEqual(report["method"], LEGACY_DETAIL_METHOD)

    def test_permission_denied_detail_probes_only_sku_capable_peer(self):
        class Response:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def iter_content(self, size):
                yield json.dumps(self.payload).encode()

            def close(self):
                pass

        calls = []

        def get(url, **options):
            method = options["params"]["method"]
            calls.append(method)
            return Response({"error_response": {
                "sub_code": "isv.permission-api-package-limit",
            }})

        env = {"TAOBAO_ALLIANCE_APP_KEY": "123", "TAOBAO_ALLIANCE_APP_SECRET": "secret",
               "TAOBAO_ALLIANCE_ADZONE_ID": "456"}
        with self.assertRaises(TaobaoAllianceAPIError) as raised:
            fetch_item_detail("opaque-new-id", http_get=get, env=env, now=self.now)
        self.assertEqual(raised.exception.code, "api_package_unavailable")
        self.assertEqual(calls, list(SKU_DETAIL_METHODS))
        self.assertEqual(
            [attempt["remote_code"] for attempt in raised.exception.attempts],
            ["isv.permission-api-package-limit", "isv.permission-api-package-limit"],
        )

    def test_legacy_sku_detail_is_accepted_only_with_bound_sku_fields(self):
        start = int((self.now - timedelta(hours=1)).timestamp() * 1000)
        end = int((self.now + timedelta(hours=1)).timestamp() * 1000)
        legacy_payload = {"tbk_item_details_get_response": {"results": {"tbk_item_detail": [{
            "input_item_iid": "opaque-new-id", "item_id": "123456789",
            "item_basic_info": {"title": "测试抽纸"},
            "sku_list": {"tbk_item_detail_sku": [{
                "sku_id": "9001", "quantity": "4",
                "property_list": {"tbk_item_detail_sku_prop": [
                    {"property_text": "规格", "value_text": "100抽×10包"},
                ]},
                "sku_price_promotion_info": {
                    "sku_zk_final_price": "10.00", "sku_final_promotion_price": "8.00",
                    "sku_final_promotion_path_list": {"sku_final_promotion_path_map_data": [{
                        "promotion_title": "商品券", "promotion_desc": "减2元", "promotion_fee": "2.00",
                        "promotion_start_time": str(start), "promotion_end_time": str(end),
                    }]},
                },
            }]},
        }]}}}

        class Response:
            status_code = 200
            def __init__(self, payload): self.payload = payload
            def iter_content(self, size): yield json.dumps(self.payload).encode()
            def close(self): pass

        calls = []
        def get(url, **options):
            method = options["params"]["method"]
            calls.append(method)
            if method == DETAIL_METHOD:
                return Response({"error_response": {"sub_code": "isv.permission-api-package-limit"}})
            return Response(legacy_payload)

        env = {"TAOBAO_ALLIANCE_APP_KEY": "123", "TAOBAO_ALLIANCE_APP_SECRET": "secret",
               "TAOBAO_ALLIANCE_ADZONE_ID": "456"}
        detail = fetch_item_detail("opaque-new-id", http_get=get, env=env, now=self.now)
        self.assertEqual(calls, list(SKU_DETAIL_METHODS))
        self.assertEqual(detail["field_state"], "bound")
        self.assertEqual(detail["sku_id"], "9001")
        self.assertEqual(detail["advertised_cents"], 800)
        self.assertEqual(detail["detail_api_attempts"][0]["remote_code"], "isv.permission-api-package-limit")
        self.assertEqual(detail["detail_api_attempts"][1], {"method": LEGACY_DETAIL_METHOD, "state": "returned"})


    def test_fetch_preserves_quotes_and_audits_promotion_candidates(self):
        item = sample_item(self.now)
        payload = {"tbk_dg_material_optional_response": {
            "result_list": {"map_data": [item, sample_item(self.now, reserve="10", promotion="10", coupon="", item_id="987654321")]}
        }}

        class Response:
            status_code = 200

            def close(self):
                pass

            def iter_content(self, size):
                yield json.dumps(payload).encode()

        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        env = {"TAOBAO_ALLIANCE_APP_KEY": "123", "TAOBAO_ALLIANCE_APP_SECRET": "secret",
               "TAOBAO_ALLIANCE_ADZONE_ID": "456"}
        report = {}
        rows = fetch_rows("https://eco.taobao.com/router/rest?q=抽纸", http_get=fake_get,
                          env=env, now=self.now, report=report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(report['raw_count'], 2)
        self.assertEqual(report['promotion_count'], 1)
        self.assertEqual(len(report['pages'][0]['response_sha256']), 64)
        self.assertEqual(rows[0][2], "https://item.taobao.com/item.htm?id=123456789")
        requested_url, options = calls[0]
        self.assertEqual(requested_url, API_ENDPOINT)
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["timeout"], (5, 20))
        self.assertEqual(options["params"]["q"], "抽纸")
        self.assertEqual(options["params"]["has_coupon"], "false")
        self.assertEqual(options["params"]["page_size"], "100")
        self.assertEqual(options["params"]["sign_method"], "hmac")
        # The legacy material.optional contract returns its documented fields
        # without a `fields` request parameter.
        self.assertNotIn("fields", options["params"])
        self.assertEqual(len(options["params"]["sign"]), 32)

    def test_upgraded_response_maps_only_direct_taobao_public_price(self):
        from services.taobao_alliance import _rows_from_payload, API_METHOD
        self.assertEqual(API_METHOD, "taobao.tbk.dg.material.optional.upgrade")
        item = {
            "item_id": "opaque-new-id",
            "publish_info": {"click_url": "//item.taobao.com/item.htm?id=123456789&scm=x"},
            "item_basic_info": {"title": "测试抽纸", "user_type": 0, "shop_title": "测试店"},
            "price_promotion_info": {"reserve_price": "10.00", "zk_final_price": "8.00",
                                     "final_promotion_price": "6.00"},
        }
        payload = {"tbk_dg_material_optional_upgrade_response": {
            "result_list": {"map_data": [item]}}}
        report = {}
        rows = _rows_from_payload(payload, now=self.now, report=report)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "https://item.taobao.com/item.htm?id=123456789")
        self.assertEqual(rows[0][5]["taobao_alliance_offer"]["quote_price_cents"], 800)
        self.assertNotIn("6.00", rows[0][3])  # Estimated effective price is not checkout proof.
        item["publish_info"]["click_url"] = "//uland.taobao.com/coupon/edetail?e=abc"
        self.assertEqual(_rows_from_payload(payload, now=self.now), [])

    def test_upgraded_response_accepts_only_official_promotion_link_with_new_item_id(self):
        from services.taobao_alliance import _rows_from_payload
        item = {
            "item_id": "opaque-new-id",
            "publish_info": {"click_url": "//s.click.taobao.com/t?e=official-token"},
            "item_basic_info": {"title": "测试抽纸", "user_type": 0, "shop_title": "测试店"},
            "price_promotion_info": {"reserve_price": "10.00", "zk_final_price": "8.00"},
        }
        payload = {"tbk_dg_material_optional_upgrade_response": {"result_list": {"map_data": [item]}}}
        rows = _rows_from_payload(payload, now=self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "taobao:opaque-new-id")
        self.assertEqual(rows[0][2], "https://s.click.taobao.com/t?e=official-token")
        self.assertEqual(rows[0][5]["taobao_alliance_offer"]["link_kind"], "official_promotion_link")
        self.assertEqual(validated_offer(rows[0][5], now=self.now)["quote_price_cents"], 800)
        self.assertIn("尚未独立核实跳转", rows[0][3])
        from services.marketplace_identity import attach_product_marketplace
        classified = attach_product_marketplace({"source_method": "taobao_alliance",
            "metadata_json": json.dumps(rows[0][5]), "product_links": []})
        self.assertEqual(classified["marketplace_key"], "taobao")
        self.assertIn("尚未独立解析", classified["marketplace_evidence"])
        for bad in ("//evil.example/t?e=official-token", "//s.click.taobao.com.evil.example/t",
                    "http://s.click.taobao.com/t", "//s.click.taobao.com:9999/t",
                    "//s.click.taobao.com/t#frag"):
            with self.subTest(bad=bad):
                item["publish_info"]["click_url"] = bad
                self.assertEqual(_rows_from_payload(payload, now=self.now), [])
        item["publish_info"]["click_url"] = "//s.click.taobao.com/t?e=official-token"
        item["item_id"] = "<invalid>"
        self.assertEqual(_rows_from_payload(payload, now=self.now), [])

    def test_current_official_promotion_path_is_a_lead_without_double_deduction(self):
        from services.taobao_alliance import _rows_from_payload
        self.now = datetime.now(CHINA)
        item = {
            "item_id": "opaque-new-id",
            "publish_info": {"click_url": "//s.click.taobao.com/t?e=official-token"},
            "item_basic_info": {"title": "测试抽纸", "user_type": 0, "shop_title": "测试店"},
            "price_promotion_info": {
                "reserve_price": "12", "zk_final_price": "12", "final_promotion_price": "1.91",
                "predict_rounding_up_price_desc": "需凑单",
                "final_promotion_path_list": {"final_promotion_path_map_data": [
                    {"promotion_title": "秒杀直降", "promotion_desc": "直降4.8元", "promotion_fee": "4.8",
                     "promotion_start_time": str(int((self.now-timedelta(hours=1)).timestamp()*1000)),
                     "promotion_end_time": str(int((self.now+timedelta(hours=1)).timestamp()*1000))},
                    {"promotion_title": "限时补贴", "promotion_desc": "补贴5.29元", "promotion_fee": "5.29",
                     "promotion_start_time": str(int((self.now-timedelta(hours=1)).timestamp()*1000)),
                     "promotion_end_time": str(int((self.now+timedelta(hours=1)).timestamp()*1000))},
                ]},
            },
        }
        payload = {"tbk_dg_material_optional_upgrade_response": {"result_list": {"map_data": [item]}}}
        row = _rows_from_payload(payload, now=self.now)[0]
        evidence = row[5]["taobao_alliance_offer"]
        self.assertEqual(evidence["quote_price_cents"], 1200)
        self.assertEqual(evidence["promotion_path"]["estimate_cents"], 191)
        self.assertTrue(evidence["promotion_path_candidate"])
        self.assertIn("需凑单", row[3])
        brief = {"quantity": 1}
        display = listing_quote({"source_method":"taobao_alliance", "auto_state":"observed",
                                 "metadata_json": json.dumps(row[5])}, brief)
        self.assertEqual(display["amount_cents"], 1200)
        self.assertEqual(display["kind"], "official_promotion_observation")
        from services.product_search import is_promotional_price_candidate
        self.assertTrue(is_promotional_price_candidate({"source_method":"taobao_alliance",
            "metadata_json": json.dumps(row[5]), "display_quote": display}))
        item["price_promotion_info"]["final_promotion_price"] = "1.90"
        partial = _rows_from_payload(payload, now=self.now)[0][5]["taobao_alliance_offer"]
        self.assertTrue(partial["promotion_path_candidate"])
        self.assertFalse(partial["promotion_path_complete"])
        self.assertIsNone(partial["promotion_path"]["estimate_cents"])
        item["price_promotion_info"]["final_promotion_price"] = "1.91"
        item["price_promotion_info"]["final_promotion_path_list"]["final_promotion_path_map_data"][0]["promotion_end_time"] = str(int((self.now-timedelta(minutes=1)).timestamp()*1000))
        self.assertFalse(_rows_from_payload(payload, now=self.now)[0][5]["taobao_alliance_offer"]["promotion_path_candidate"])

    def test_permission_package_error_is_audited_without_credentials(self):
        from services.taobao_alliance import _fetch_page
        env = {"TAOBAO_ALLIANCE_APP_KEY": "123", "TAOBAO_ALLIANCE_APP_SECRET": "secret",
               "TAOBAO_ALLIANCE_ADZONE_ID": "456"}
        class Response:
            status_code = 200
            def iter_content(self, size):
                yield json.dumps({"error_response": {"code": 11,
                    "sub_code": "isv.permission-api-package-limit"}}).encode()
            def close(self): pass
        report = {}
        with self.assertRaises(TaobaoAllianceAPIError) as caught:
            _fetch_page(API_ENDPOINT + "?q=抽纸", http_get=lambda *a, **kw: Response(),
                        env=env, now=self.now, report=report)
        self.assertEqual(caught.exception.code, "api_package_unavailable")
        self.assertEqual(report["top_sub_code"], "isv.permission-api-package-limit")
        self.assertNotIn("secret", json.dumps(report))

    def test_credentials_are_required_and_taobao_metadata_must_be_valid(self):
        self.assertIn("TAOBAO_ALLIANCE_APP_KEY", configuration_error({}))
        with self.assertRaises(TaobaoAllianceConfigurationError):
            fetch_rows("https://eco.taobao.com/router/rest?q=抽纸", env={})
        self.assertIsNone(validated_offer({"taobao_alliance_offer": {
            "reserve_price_cents": 1000, "promotion_price_cents": 1000,
            "quote_price_cents": 1000, "price_drop_candidate": False,
            "coupon_candidate": False,
        }}))

    def test_missing_reference_keeps_quote_and_coupon_without_inventing_stackability(self):
        item = sample_item(self.now, reserve=None, promotion="49.90", coupon="15", threshold="40")
        offer = parse_item(item, now=self.now)[5]['taobao_alliance_offer']
        self.assertEqual(offer['quote_price_cents'], 4990)
        self.assertIsNone(offer['reserve_price_cents'])
        self.assertIsNone(offer['promotion_discount_cents'])
        self.assertTrue(offer['coupon_candidate'])
        self.assertEqual(offer['coupon_amount_basis'], 'stacking_unknown')
        self.assertIsNone(offer['coupon_applied_cents'])
        self.assertEqual(offer['coupon_rule_estimate_cents'],3490)
        self.assertEqual(offer['coupon_user_eligibility'],'unknown')

    def test_coupon_zero_unknown_and_availability_do_not_destroy_public_quote(self):
        for raw, state in ((0,'depleted'),('0','depleted'),(None,'remaining_unknown'),('', 'remaining_unknown'),('20','active_candidate')):
            with self.subTest(raw=raw):
                offer = parse_item(sample_item(self.now, remaining=raw), now=self.now)[5]['taobao_alliance_offer']
                self.assertEqual(offer['coupon_state'], state)
                self.assertEqual(offer['coupon_candidate'], state == 'active_candidate')
                self.assertEqual(offer['quote_price_cents'], 800)

    def test_date_precision_and_end_exclusion(self):
        item = sample_item(self.now, starts='2026-10-08', ends='2026-10-10')
        for now, expected in ((self.now,'validity_precision_unknown'),(self.now+timedelta(days=1),'active_candidate'),(self.now+timedelta(days=2),'validity_precision_unknown'),(self.now+timedelta(days=3),'expired')):
            with self.subTest(now=now):
                offer = parse_item(item, now=now)[5]['taobao_alliance_offer']
                self.assertEqual(offer['coupon_state'], expected)
        item = sample_item(self.now, ends=self.now.strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(parse_item(item, now=self.now)[5]['taobao_alliance_offer']['coupon_state'],'expired')
        item['event_end_time'] = self.now.strftime('%Y-%m-%d %H:%M:%S')
        self.assertIsNone(parse_item(item, now=self.now))

    def test_cached_flags_cannot_extend_expiry_or_fabricate_discounts(self):
        row = parse_item(sample_item(self.now,reserve='10',promotion='10'),now=self.now)
        row[5]['taobao_alliance_offer']['quote_price_cents'] = 1
        offer = validated_offer(row[5],now=self.now+timedelta(hours=2))
        self.assertFalse(offer['coupon_candidate'])
        self.assertEqual(offer['quote_price_cents'],1000)
        self.assertEqual(offer['coupon_state'],'expired')

    def test_invalid_prices_and_unknown_marketplaces_are_rejected_not_coerced(self):
        for raw in ('NaN','Infinity',-1,'',0,'12-19',True):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_item(sample_item(self.now,promotion=raw),now=self.now))
        item=sample_item(self.now);item['user_type']=3
        self.assertIsNone(parse_item(item,now=self.now))
        self.assertEqual(parse_item(sample_item(self.now,promotion='12.34'),now=self.now)[5]['taobao_alliance_offer']['quote_price_cents'],1234)

    def test_http_200_login_and_schema_drift_have_distinct_audit_errors(self):
        env={'TAOBAO_ALLIANCE_APP_KEY':'123','TAOBAO_ALLIANCE_APP_SECRET':'secret','TAOBAO_ALLIANCE_ADZONE_ID':'456'}
        class Response:
            status_code=200
            def __init__(self,body): self.body=body
            def iter_content(self,size): yield self.body
            def close(self): pass
        for body,code in ((b'<html>login.taobao.com</html>','access_restricted'),(b'{"result_list":{"changed":[]}}','schema_mismatch')):
            report={}
            with self.subTest(code=code),self.assertRaises(TaobaoAllianceAPIError):
                fetch_rows(API_ENDPOINT+'?q=牛奶',env=env,now=self.now,report=report,http_get=lambda *a,**kw:Response(body))
            self.assertEqual(report['error_code'],code)
            self.assertEqual(len(report['failed_page_evidence']['response_sha256']),64)
            self.assertIsNone(report['raw_count'])

    def test_presale_is_quarantined_and_raw_input_is_retained(self):
        from services.taobao_alliance import _rows_from_payload
        item=sample_item(self.now);item['presale_deposit']='20';item['zk_final_price']='200'
        report={}
        self.assertEqual(_rows_from_payload({'result_list':[item]},now=self.now,report=report),[])
        self.assertEqual(report['raw_items'][0]['presale_deposit'],'20')
        self.assertEqual(report['rejections'][0]['reason'],'presale_requires_plan')

    def test_paging_partial_failure_rejections_limit_and_no_secret_leak(self):
        env={'TAOBAO_ALLIANCE_APP_KEY':'123','TAOBAO_ALLIANCE_APP_SECRET':'secret','TAOBAO_ALLIANCE_ADZONE_ID':'456'}
        calls, closed = [], []
        class Response:
            status_code=200
            def __init__(self, payload): self.payload=payload
            def iter_content(self,size): yield json.dumps(self.payload).encode()
            def close(self): closed.append(True)
        def get(url,**kw):
            page=int(kw['params']['page_no']);calls.append(page)
            self.assertNotIn('max_pages',kw['params'])
            self.assertEqual(kw['params']['has_coupon'],'true')
            if page==3: raise requests.Timeout('URL with secret')
            items=[sample_item(self.now,item_id=str(i)) for i in ([1,2] if page==1 else [2,3])]
            return Response({'result_list':{'map_data':items}})
        report={}
        rows=fetch_rows(API_ENDPOINT+'?q=牛奶&page_size=2&max_pages=3&has_coupon=1',http_get=get,env=env,now=self.now,report=report)
        self.assertEqual(calls,[1,2,3]);self.assertEqual(len(closed),2)
        self.assertEqual([r[0] for r in rows],['taobao:1','taobao:2','taobao:3'])
        self.assertEqual((report['status'],report['raw_count'],report['parsed_count'],report['duplicate_count']),('partial',4,3,1))
        self.assertEqual(report['next_page'],3)
        self.assertNotIn('secret',json.dumps(report))
        for payload, expected in (({'result_list':[]},'complete'),({'result_list':{'renamed':[]}},'failed'),('login html','failed')):
            report={}
            with self.subTest(payload=payload):
                if expected=='failed':
                    with self.assertRaises(TaobaoAllianceAPIError): fetch_rows(API_ENDPOINT+'?q=牛奶',http_get=lambda *a,**kw:Response(payload),env=env,now=self.now,report=report)
                else:
                    self.assertEqual(fetch_rows(API_ENDPOINT+'?q=牛奶',http_get=lambda *a,**kw:Response(payload),env=env,now=self.now,report=report),[])
                self.assertEqual(report['status'],expected)
        report={}
        fetch_rows(API_ENDPOINT+'?q=牛奶&page_size=1&max_pages=1',http_get=lambda *a,**kw:Response({'result_list':[sample_item(self.now)]}),env=env,now=self.now,report=report)
        self.assertEqual(report['stop_reason'],'page_limit');self.assertEqual(report['next_page'],2)


    def test_auto_paging_fetches_every_page_until_the_official_result_is_short(self):
        env={'TAOBAO_ALLIANCE_APP_KEY':'123','TAOBAO_ALLIANCE_APP_SECRET':'secret','TAOBAO_ALLIANCE_ADZONE_ID':'456'}
        calls=[]
        class Response:
            status_code=200
            def __init__(self,payload): self.payload=payload
            def iter_content(self,size): yield json.dumps(self.payload).encode()
            def close(self): pass
        def get(url,**kw):
            page=int(kw['params']['page_no']); calls.append(page)
            item_ids={1:['11','12'],2:['21','22'],3:['31']}[page]
            return Response({'result_list':{'map_data':[sample_item(self.now,item_id=item_id) for item_id in item_ids]}})
        report={}
        rows=fetch_rows(API_ENDPOINT+'?q=牛奶&page_size=2&max_pages=all',http_get=get,env=env,now=self.now,report=report)
        self.assertEqual(calls,[1,2,3])
        self.assertEqual(len(rows),5)
        self.assertEqual(report['max_pages'],'all')
        self.assertEqual(report['stop_reason'],'page_exhausted')
        self.assertIsNone(report['next_page'])


    def test_auto_paging_stops_when_the_official_api_repeats_a_full_page(self):
        env={'TAOBAO_ALLIANCE_APP_KEY':'123','TAOBAO_ALLIANCE_APP_SECRET':'secret','TAOBAO_ALLIANCE_ADZONE_ID':'456'}
        calls=[]
        payload={'result_list':{'map_data':[
            sample_item(self.now,item_id='11'), sample_item(self.now,item_id='12')
        ]}}
        class Response:
            status_code=200
            def iter_content(self,size): yield json.dumps(payload).encode()
            def close(self): pass
        def get(url,**kw):
            calls.append(int(kw['params']['page_no']))
            return Response()
        report={}
        rows=fetch_rows(API_ENDPOINT+'?q=牛奶&page_size=2&max_pages=all',http_get=get,env=env,now=self.now,report=report)
        self.assertEqual(calls,[1,2])
        self.assertEqual(len(rows),2)
        self.assertEqual(report['stop_reason'],'repeated_page')
        self.assertIsNone(report['next_page'])



class TaobaoAlliancePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "workbench.sqlite3")
        self.db_patch.start()
        db.initialize()
        with db.connect() as conn:
            self.source_id = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES('淘宝联盟','抽纸搜索','零售优惠',?,'taobao_alliance','taobao_alliance','pending',1,60)""",
                ("https://eco.taobao.com/router/rest?q=抽纸",)).lastrowid

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_taobao_only_scan_does_not_run_other_enabled_sources(self):
        with db.connect() as conn:
            conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES('其他平台','未选中','零售优惠','https://example.test/feed','html','suning','pending',1,60)""")
        with patch("scanner.scan_source", return_value={"status": "healthy"}) as scan, \
                patch("autoreview.review_all"):
            scanner.scan_all(method="taobao_alliance")
        scan.assert_called_once_with(self.source_id, review=False)

    def test_taobao_detail_cycle_never_probes_another_source_method(self):
        with db.connect() as conn:
            other_source = conn.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes,last_success)
                VALUES('其他平台','不得探测','零售优惠','https://www.smzdm.com/','html','html','healthy',1,60,CURRENT_TIMESTAMP)""").lastrowid
            event_id = conn.execute("""INSERT INTO events
                (source_id,external_key,title,url,fingerprint,published_at,last_seen_at)
                VALUES(?, 'other-1', '其他来源商品', 'https://www.smzdm.com/p/200/',
                       'other-fingerprint', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""", (other_source,)).lastrowid
            opportunity_id = conn.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,url)
                VALUES(?, ?, '其他来源商品', '零售优惠', 'https://www.smzdm.com/p/200/')""",
                (event_id, other_source)).lastrowid
            conn.execute("INSERT INTO auto_reviews(opportunity_id,state,reason) VALUES(?, 'queued', '待验证')",
                         (opportunity_id,))
        with patch("scanner._fetch") as fetch:
            result = autoreview.run_cycle(limit=10, source_method="taobao_alliance")
        self.assertEqual(result["probed"], 0)
        fetch.assert_not_called()

    def test_missing_credentials_set_not_configured_and_do_not_create_fake_events(self):
        with patch.dict(os.environ, {
            "TAOBAO_ALLIANCE_APP_KEY": "", "TAOBAO_ALLIANCE_APP_SECRET": "",
            "TAOBAO_ALLIANCE_ADZONE_ID": "",
        }):
            result = scanner.scan_source(self.source_id)
        self.assertEqual(result["status"], "not_configured")
        self.assertIn("TAOBAO_ALLIANCE_APP_KEY", result["error"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE source_id=?",
                                          (self.source_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM sources WHERE id=?",
                                          (self.source_id,)).fetchone()[0], "not_configured")

    def test_successful_api_scan_with_no_qualifying_promotions_is_empty(self):
        with patch("scanner.taobao_alliance_rows", return_value=[]):
            result = scanner.scan_source(self.source_id)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["new"], 0)
        self.assertIn("未形成可解析报价", result["message"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE source_id=?",
                                          (self.source_id,)).fetchone()[0], 0)

    def test_partial_scan_without_usable_rows_is_not_healthy_empty(self):
        def partial_empty(_url, report):
            report.update(status="partial", failed_page=2, raw_count=0,
                          parsed_count=0, promotion_count=0, rejected_count=0)
            return []

        with patch("scanner.taobao_alliance_rows", side_effect=partial_empty):
            result = scanner.scan_source(self.source_id)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["collection_status"], "partial")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM sources WHERE id=?",
                                          (self.source_id,)).fetchone()[0], "partial")

    def test_offer_flows_into_current_taobao_search_with_auditable_basis(self):
        now = datetime.now(CHINA)
        row = parse_item(sample_item(now), now=now)
        with patch("scanner.taobao_alliance_rows", return_value=[row]):
            result = scanner.scan_source(self.source_id)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["new"], 1)
        with db.connect() as conn:
            saved = conn.execute("""SELECT o.resource_kind,e.url,e.published_at,a.state,a.advertised_cents,
                e.metadata_json,s.method FROM opportunities o JOIN events e ON e.id=o.event_id
                JOIN auto_reviews a ON a.opportunity_id=o.id JOIN sources s ON s.id=o.source_id
                WHERE o.source_id=?""", (self.source_id,)).fetchone()
        self.assertEqual(saved["resource_kind"], "purchase")
        self.assertEqual(saved["url"], "https://item.taobao.com/item.htm?id=123456789")
        self.assertIsNone(saved["published_at"])  # Observation time is not fabricated as publish time.
        self.assertEqual(saved["state"], "observed")
        self.assertEqual(saved["advertised_cents"], 800)
        evidence = validated_offer(saved["metadata_json"])
        display_row = {"source_method": saved["method"], "metadata_json": saved["metadata_json"],
                       "auto_state": saved["state"]}
        quote = listing_quote(display_row, {})
        display_row["display_quote"] = quote
        self.assertEqual(quote["amount_cents"], 800)
        self.assertEqual(quote["kind"], "official_promotion_observation")
        self.assertTrue(is_promotional_price_candidate(display_row, {}))
        page = app.test_client().get("/?q=抽纸&buy_platform=taobao").get_data(as_text=True)
        self.assertIn("测试抽纸 100抽*10包", page)
        self.assertIn("淘宝联盟官方推广价", page)
        clue_page = app.test_client().get(
            "/?q=抽纸&buy_platform=taobao&view=current").get_data(as_text=True)
        self.assertIn("测试抽纸 100抽*10包", clue_page)
        self.assertIn("淘宝联盟官方推广价", clue_page)
        self.assertNotIn("¥0.80/包", clue_page)  # Title size is not the selected priced variant.
        self.assertIn("800", str(evidence["quote_price_cents"]))

    def test_new_promotion_link_keeps_old_observation_but_only_one_current_product(self):
        now = datetime.now(CHINA)
        item = sample_item(now, item_id="123456789")
        item.pop("num_iid")
        item["item_id"] = "official-opaque-id"
        item["item_url"] = "https://s.click.taobao.com/t?e=first-link"
        first = parse_item(item, now=now)
        with patch("scanner.taobao_alliance_rows", return_value=[first]):
            scanner.scan_source(self.source_id)
        item["item_url"] = "https://s.click.taobao.com/t?e=second-link"
        second = parse_item(item, now=now)
        with patch("scanner.taobao_alliance_rows", return_value=[second]):
            scanner.scan_source(self.source_id)
        with db.connect() as conn:
            rows = conn.execute("""SELECT o.id,a.state FROM opportunities o
                JOIN auto_reviews a ON a.opportunity_id=o.id
                WHERE o.source_id=? ORDER BY o.id""", (self.source_id,)).fetchall()
        self.assertEqual(len(rows), 2)  # Raw history is retained.
        self.assertEqual(rows[0]["state"], "excluded")
        self.assertEqual(rows[1]["state"], "observed")
        page = app.test_client().get(
            "/?q=测试抽纸&buy_platform=taobao&view=current").get_data(as_text=True)
        self.assertIn('1 条新鲜来源记录', page)
        self.assertIn('href="/opportunities/2"', page)
        self.assertNotIn('href="/opportunities/1"', page)

    def test_new_material_item_id_goes_directly_to_current_detail_api(self):
        now = datetime.now(CHINA)
        item = sample_item(now)
        item.pop("num_iid")
        item["item_id"] = "official-opaque-id"
        item["item_url"] = "https://s.click.taobao.com/t?e=current-link"
        row = parse_item(item, now=now)
        with patch("scanner.taobao_alliance_rows", return_value=[row]):
            scanner.scan_source(self.source_id)
        detail = {
            'evidence_kind': 'taobao_alliance_sku_detail', 'field_state': 'bound',
            'field_reason': '已由官方详细接口绑定商品ID、SKU、规格、库存和价格',
            'title': '测试抽纸 100抽*10包', 'item_id': '123456789', 'sku_id': '9001', 'specification': '规格：100抽×10包',
            'advertised_cents': 800, 'public_sale_cents': 1000,
            'promotion_estimate_cents': 800, 'promotion_path': [{'title': '商品券'}],
            'quantity_observed': 8, 'conditions': '商品券减2元', 'published_at': None,
            'promotional_candidate': True,
        }
        with patch('autoreview.fetch_taobao_sku_detail', return_value=detail) as fetch:
            result = autoreview.run_cycle(limit=1, source_method="taobao_alliance")
        self.assertEqual(result['probed'], 1)
        fetch.assert_called_once_with('official-opaque-id')
        with db.connect() as conn:
            saved = conn.execute('SELECT detail_json FROM auto_reviews').fetchone()
        persisted = json.loads(saved['detail_json'])
        self.assertEqual(persisted['link_identity']['resolution_method'], 'material_item_id_dynamic_scene')
        self.assertEqual(persisted['link_identity']['item_id'], 'official-opaque-id')

    def test_scheduled_taobao_cycle_runs_only_its_detail_queue(self):
        with db.connect() as conn:
            run_id = conn.execute("INSERT INTO source_scan_runs(source_id,status,evidence_json) VALUES(?, 'complete', '{}')",
                                  (self.source_id,)).lastrowid
            event_id = conn.execute("""INSERT INTO events
                (source_id,external_key,title,url,fingerprint,published_at,last_seen_at,metadata_json)
                VALUES(?, 'scheduled-1', '测试抽纸', 'https://item.taobao.com/item.htm?id=123',
                       'scheduled-fingerprint', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}')""",
                                    (self.source_id,)).lastrowid
            opportunity_id = conn.execute("""INSERT INTO opportunities(event_id,source_id,title,category,url)
                VALUES(?, ?, '测试抽纸', '零售优惠', 'https://item.taobao.com/item.htm?id=123')""",
                                         (event_id, self.source_id)).lastrowid
            conn.execute("""INSERT INTO auto_reviews(opportunity_id,state,reason,detail_json)
                VALUES(?, 'conditional', 'SKU详情受限', ? )""",
                         (opportunity_id, json.dumps({'field_state': 'access_restricted',
                                                       'sku_detail_api_state': 'permission_unavailable'})))
        scan_result = [{"source_id": self.source_id, "seen": 1, "run_id": run_id,
                        "collection_status": "complete", "opportunity_ids": [opportunity_id],
                        "new_opportunity_ids": [opportunity_id]}]
        detail_result = {"counts": {"conditional": 1}, "probed": 1,
                         "target_opportunity_ids": [opportunity_id],
                         "probed_opportunity_ids": [opportunity_id]}
        with patch.object(application_module, "scan_all", return_value=scan_result) as scan, \
             patch.object(application_module, "run_cycle", return_value=detail_result) as detail:
            result = application_module.run_taobao_cycle()
        self.assertEqual(result["sources"], scan_result)
        receipt = result["post_sync_validation"][0]
        self.assertEqual((receipt["synced_count"], receipt["attempted_count"], receipt["accounted_count"]), (1, 1, 1))
        self.assertEqual(receipt["sku_evidence_complete_count"], 0)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["records"][0]["opportunity_id"], opportunity_id)
        scan.assert_called_once_with(due_only=True, method="taobao_alliance")
        detail.assert_called_once_with(limit=1, source_method="taobao_alliance",
                                       opportunity_ids=(opportunity_id,))
        with db.connect() as conn:
            saved = json.loads(conn.execute("SELECT evidence_json FROM source_scan_runs WHERE id=?", (run_id,)).fetchone()[0])
        self.assertEqual(saved["post_sync_validation"]["records"][0]["state"], "conditional")
        page = app.test_client().get("/sources").get_data(as_text=True)
        self.assertIn("同步后逐条核验：同步 1 条", page)
        self.assertIn("SKU详情：permission_unavailable", page)

    def test_scheduled_taobao_cycle_skips_detail_when_no_source_is_due(self):
        with patch.object(application_module, "scan_all", return_value=[]) as scan, \
             patch.object(application_module, "run_cycle") as detail:
            self.assertEqual(application_module.run_taobao_cycle(), {})
        scan.assert_called_once_with(due_only=True, method="taobao_alliance")
        detail.assert_not_called()

    def test_scan_result_tracks_new_and_reused_opportunities_for_post_sync_audit(self):
        now = datetime.now(CHINA)
        row = parse_item(sample_item(now), now=now)
        with patch("scanner.taobao_alliance_rows", return_value=[row]):
            first = scanner.scan_source(self.source_id, review=False)
        self.assertEqual(first["new"], 1)
        self.assertEqual(len(first["new_opportunity_ids"]), 1)
        self.assertEqual(first["opportunity_ids"], first["new_opportunity_ids"])
        with patch("scanner.taobao_alliance_rows", return_value=[row]):
            repeated = scanner.scan_source(self.source_id, review=False)
        self.assertEqual(repeated["new"], 0)
        self.assertEqual(repeated["new_opportunity_ids"], [])
        self.assertEqual(repeated["opportunity_ids"], first["opportunity_ids"])

    def test_sources_page_explains_setup_and_scope(self):
        page = app.test_client().get("/sources").get_data(as_text=True)
        self.assertIn("淘宝联盟官方优惠 API", page)
        self.assertIn("TAOBAO_ALLIANCE_APP_SECRET", page)
        self.assertIn("不是淘宝全站", page)

    def test_source_form_saves_dynamic_query_but_marks_missing_credentials(self):
        client = app.test_client()
        client.get("/sources")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        with patch.dict(os.environ, {
            "TAOBAO_ALLIANCE_APP_KEY": "", "TAOBAO_ALLIANCE_APP_SECRET": "",
            "TAOBAO_ALLIANCE_ADZONE_ID": "",
        }):
            response = client.post("/sources", data={
                "csrf": csrf, "platform": "淘宝联盟", "name": "牛奶搜索第2页",
                "category": "零售优惠", "method": "taobao_alliance",
                "url": "https://eco.taobao.com/router/rest?q=牛奶&page_no=2&page_size=20",
                "interval_minutes": "60",
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("尚未连接", response.get_data(as_text=True))
        with db.connect() as conn:
            source = conn.execute("SELECT * FROM sources WHERE name='牛奶搜索第2页'").fetchone()
        self.assertIsNotNone(source)
        self.assertEqual(source["method"], "taobao_alliance")
        self.assertEqual(source["status"], "not_configured")
        self.assertEqual(source["url"], "https://eco.taobao.com/router/rest?q=牛奶&page_no=2&page_size=20")

    def test_audit_raw_rejections_and_failure_rollback(self):
        now=datetime.now(CHINA)
        row=parse_item(sample_item(now),now=now)
        def fixture(url,report):
            report.update(status='partial',raw_count=2,parsed_count=1,promotion_count=1,rejected_count=1,
                          next_page=2,pages=[{'page_no':1,'raw_items':[sample_item(now),{'title':'bad'}],
                                          'rejections':[{'index':1,'reason':'invalid_product_identity'}]}])
            return [row]
        with patch('scanner.taobao_alliance_rows',side_effect=fixture): scanner.scan_source(self.source_id,review=False)
        with db.connect() as conn:
            run=conn.execute('SELECT * FROM source_scan_runs ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual((run['status'],run['raw_count'],run['rejected_count']),('partial',2,1))
            self.assertEqual(json.loads(run['evidence_json'])['pages'][0]['raw_items'][1]['title'],'bad')
            before=conn.execute('SELECT count(*) FROM events').fetchone()[0]
        page=app.test_client().get('/sources').get_data(as_text=True)
        self.assertIn('部分页失败',page);self.assertIn('invalid_product_identity',page)
        malformed=list(row);malformed[0]='new';malformed[4]='bad timestamp'
        with patch('scanner.taobao_alliance_rows',return_value=[tuple(malformed)]),self.assertRaises(ValueError): scanner.scan_source(self.source_id,review=False)
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0],before)
            self.assertEqual(conn.execute('SELECT status FROM source_scan_runs ORDER BY id DESC LIMIT 1').fetchone()[0],'failed')

    def test_plain_quote_does_not_enter_default_offers(self):
        now=datetime.now(CHINA)
        row=parse_item(sample_item(now,reserve='19.90',promotion='19.90',coupon=''),now=now)
        with patch('scanner.taobao_alliance_rows',return_value=[row]): scanner.scan_source(self.source_id)
        with db.connect() as conn:
            saved=conn.execute('SELECT e.metadata_json,a.state FROM events e JOIN opportunities o ON o.event_id=e.id JOIN auto_reviews a ON a.opportunity_id=o.id WHERE e.source_id=?',(self.source_id,)).fetchone()
        self.assertEqual(saved['state'], 'conditional')
        display={'source_method':'taobao_alliance','metadata_json':saved['metadata_json'],'auto_state':saved['state']}
        quote=listing_quote(display,{})
        self.assertIsNone(quote)
        display['display_quote']=quote
        self.assertFalse(is_promotional_price_candidate(display,{}))
        page=app.test_client().get('/?q=测试抽纸&buy_platform=taobao').get_data(as_text=True)
        self.assertNotIn('测试抽纸 100抽*10包</a>',page)

    def test_missing_detail_permission_isolated_and_rechecked_daily(self):
        now = datetime.now(CHINA)
        row = parse_item(sample_item(now), now=now)
        with patch('scanner.taobao_alliance_rows', return_value=[row]):
            scanner.scan_source(self.source_id)
        blocked = TaobaoAllianceAPIError('permission blocked', 'api_package_unavailable')
        with patch('autoreview.fetch_taobao_sku_detail', side_effect=blocked):
            result = autoreview.run_cycle(limit=1)
        self.assertEqual(result['probed'], 1)
        with db.connect() as conn:
            saved = conn.execute('SELECT state,detail_json,detail_error,next_check_at FROM auto_reviews').fetchone()
        detail = json.loads(saved['detail_json'])
        self.assertEqual(saved['state'], 'observed')
        self.assertEqual(detail['field_state'], 'access_restricted')
        self.assertEqual(detail['sku_detail_api_state'], 'permission_unavailable')
        self.assertEqual(detail['detail_api_attempts'][0]['method'], DETAIL_METHOD)
        self.assertIsNone(saved['detail_error'])
        self.assertIsNotNone(saved['next_check_at'])

        # The denied row is not retried on every minute-long worker cycle.
        with patch('autoreview.fetch_taobao_sku_detail') as fetch:
            second = autoreview.run_cycle(limit=1)
        self.assertEqual(second['probed'], 0)
        fetch.assert_not_called()
        # A post-sync receipt may target the record, but it preserves the
        # 24-hour capability cooldown instead of hammering the official API.
        with patch('autoreview.fetch_taobao_sku_detail') as fetch:
            targeted = autoreview.run_cycle(limit=1, source_method='taobao_alliance',
                                             opportunity_ids=[1])
        self.assertEqual(targeted['probed'], 0)
        self.assertEqual(targeted['target_opportunity_ids'], [1])
        fetch.assert_not_called()
        # An explicitly selected record is an operator-requested refresh, so
        # it can retry before the normal 24-hour capability backoff expires.
        with patch('autoreview.fetch_taobao_sku_detail', return_value=detail) as fetch:
            manual = autoreview.run_cycle(limit=1, opportunity_id=1)
        self.assertEqual(manual['probed'], 1)
        fetch.assert_called_once()

    def test_current_detail_product_level_result_is_auditable_but_not_a_quote(self):
        now = datetime.now(CHINA)
        row = parse_item(sample_item(now), now=now)
        with patch('scanner.taobao_alliance_rows', return_value=[row]):
            scanner.scan_source(self.source_id)
        detail = {
            'evidence_kind': 'taobao_alliance_sku_detail', 'field_state': 'field_not_provided',
            'field_reason': '官方详情接口仅返回商品级信息，未提供可绑定的SKU、规格和库存；商品级优惠不作为可用报价',
            'title': '测试抽纸 100抽*10包', 'item_id': '123456789', 'advertised_cents': None,
            'specification': '', 'conditions': '', 'published_at': None, 'promotional_candidate': False,
            'product_level_price_observed': True,
            'product_level_prices': {'public_sale_cents': 800, 'promotion_estimate_cents': None,
                                     'reference_cents': 1000},
            'detail_api_attempts': [{'method': DETAIL_METHOD, 'state': 'returned'}],
        }
        with patch('autoreview.fetch_taobao_sku_detail', return_value=detail):
            result = autoreview.run_cycle(limit=1)
        self.assertEqual(result['probed'], 1)
        with db.connect() as conn:
            saved = conn.execute('SELECT state,advertised_cents,detail_json,next_check_at FROM auto_reviews').fetchone()
        persisted = json.loads(saved['detail_json'])
        self.assertEqual(saved['state'], 'observed')
        self.assertEqual(saved['advertised_cents'], 800)
        self.assertEqual(persisted['detail_api_attempts'][0]['method'], DETAIL_METHOD)
        default_page = app.test_client().get('/?q=抽纸&buy_platform=taobao').get_data(as_text=True)
        self.assertIn('测试抽纸 100抽*10包', default_page)
        detail_page = app.test_client().get('/opportunities/1').get_data(as_text=True)
        self.assertIn('淘宝联盟 SKU 报价核验', detail_page)
        self.assertIn('淘宝联盟官方推广价', detail_page)
        self.assertIn('公开推广价 ¥8.00', detail_page)
        self.assertIn('不构成 SKU 报价', detail_page)
        with patch('autoreview.fetch_taobao_sku_detail') as fetch:
            second = autoreview.run_cycle(limit=1)
        self.assertEqual(second['probed'], 0)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
