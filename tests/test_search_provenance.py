import unittest

from services.search_diagnostics import summarize_search_funnel
from services.source_provenance import annotate_search_sources


class SearchProvenanceTests(unittest.TestCase):
    def test_related_origins_are_visible_without_merging_rows_or_claiming_independence(self):
        body = "优惠券满3元减2元，实付2.98元，下单1件"
        rows = [
            dict(id=1, source_id=1, platform="线报酷", source_name="综合优惠",
                 title="白猫洗洁精500g", snippet=body, advertised_cents=298,
                 event_url="https://news.example/one",
                 metadata_json='{"activity_links":["https://u.jd.com/shared"]}'),
            dict(id=2, source_id=2, platform="逛丢", source_name="日化关键词",
                 title="白猫洗洁精500g", snippet="  " + body + "  ", advertised_cents=298,
                 event_url="https://news.example/two",
                 metadata_json='{"activity_links":["https://u.jd.com/shared"]}'),
            dict(id=3, source_id=3, platform="社区", source_name="独立发帖",
                 title="白猫洗洁精500g", snippet="优惠券满5元减1元，实付2.98元，下单1件",
                 advertised_cents=298, event_url="javascript:alert(1)", metadata_json="{}"),
        ]

        annotated = annotate_search_sources(rows)

        self.assertEqual(len(annotated), 3)
        self.assertEqual([row["source_count"] for row in annotated], [2, 2, 1])
        self.assertEqual(set(annotated[0]["source_channels"]), {"线报酷 · 综合优惠", "逛丢 · 日化关键词"})
        self.assertIn("标题与正文完全相同", annotated[0]["source_provenance_reasons"])
        self.assertIn("商品/活动目标链接相同", annotated[0]["source_provenance_reasons"])
        self.assertIn("来源独立性未证", annotated[0]["source_provenance_label"])
        self.assertEqual(annotated[2]["source_origins"][0]["url"], None)

    def test_funnel_counts_currentness_reasons_without_reclassifying_records(self):
        matches = [
            {"auto_state": "stale"}, {"auto_state": "source_unavailable"},
            {"auto_state": "retry"}, {"auto_state": "excluded"},
            {"auto_state": "missing_time"}, {"auto_state": "observed"},
            {"auto_state": "queued"},
        ]
        current = [
            {"id": 6, "display_quote": {"amount_cents": 298}},
            {"id": 7, "display_quote": None},
        ]

        result = summarize_search_funnel(matches, current)

        self.assertEqual(result, {
            "matched_count": 7, "current_count": 2, "current_quote_count": 1,
            "current_unpriced_count": 1, "stale_count": 1,
            "source_unavailable_count": 1, "retry_count": 1,
            "excluded_state_count": 1, "other_freshness_count": 1,
        })


if __name__ == "__main__":
    unittest.main()
