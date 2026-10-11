"""Post-sync audit for official Taobao Alliance material runs.

Collection and SKU verification are deliberately separate network operations.
This module joins them again at the run boundary: every parsed record in one
official sync has an auditable review outcome, even when that outcome is a
documented access restriction rather than an admitted offer.
"""
import json

from db import connect


REQUIRED_EVIDENCE = (
    ("商品身份", "item_identity"),
    ("SKU", "sku"),
    ("规格", "specification"),
    ("库存", "stock"),
    ("报价绑定", "quote"),
    ("优惠依据", "promotion_evidence"),
)


def detail_batch_limit(env):
    """Read a bounded per-request batch size without fixing business volume."""
    try:
        value = int(env.get("TAOBAO_DETAIL_BATCH_LIMIT", "20"))
    except (TypeError, ValueError):
        value = 20
    return min(100, max(1, value))


def _positive_ids(values):
    normalized = set()
    for value in values or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            normalized.add(number)
    return tuple(sorted(normalized))


def _prioritized_ids(new_ids, all_ids):
    """Prefer newly inserted material while retaining a stable fallback order."""
    new_set = set(new_ids)
    return tuple(new_ids) + tuple(item_id for item_id in all_ids if item_id not in new_set)


def _missing_evidence(metadata, detail):
    """Return concrete gaps; never substitute a product-level price for SKU evidence."""
    offer = (metadata or {}).get("taobao_alliance_offer") or {}
    identity = (detail or {}).get("link_identity") or {}
    detail = detail or {}
    missing = []
    if not (identity.get("item_id") or offer.get("item_id")):
        missing.append("商品身份")
    if not detail.get("sku_id"):
        missing.append("SKU")
    if not detail.get("specification"):
        missing.append("规格")
    if not isinstance(detail.get("quantity_observed"), int) or detail["quantity_observed"] <= 0:
        missing.append("库存")
    if detail.get("advertised_cents") is None:
        missing.append("报价绑定")
    # The detailed API can prove an offer in either of two ways: a current
    # promotion path, or a reference price and lower current price bound to
    # the very same SKU.  Requiring a path in every case would discard a real
    # direct SKU price drop merely because it has no coupon-like component.
    reference = detail.get("reference_cents")
    current = detail.get("public_sale_cents")
    direct_sku_price_drop = (
        bool(detail.get("promotional_candidate"))
        and isinstance(reference, int)
        and isinstance(current, int)
        and reference > current > 0
    )
    if not detail.get("promotion_path") and not direct_sku_price_drop:
        missing.append("优惠依据")
    return missing


def _record(row):
    metadata = json.loads(row["metadata_json"] or "{}")
    detail = json.loads(row["detail_json"] or "{}")
    missing = _missing_evidence(metadata, detail)
    comparison_missing = []
    if detail.get("field_state") == "bound" and not detail.get("specification_comparable", True):
        comparison_missing.append("可比规格")
    return {
        "opportunity_id": row["opportunity_id"],
        "state": row["state"],
        "reason": row["reason"],
        "checked_at": row["checked_at"],
        "detail_checked_at": row["detail_checked_at"],
        "next_check_at": row["next_check_at"],
        "detail_state": detail.get("field_state") or "not_checked",
        "sku_detail_api_state": detail.get("sku_detail_api_state") or "not_recorded",
        "missing": missing,
        # This is an evidence-completeness flag, not the product-list decision.
        # The latter must continue to use the shared search admission policy.
        "sku_evidence_complete": not missing,
        # A sole SKU can be a valid purchasable quote while still lacking the
        # attributes needed to compare it against another product listing.
        "comparison_missing": comparison_missing,
        "comparison_ready": not comparison_missing,
    }


def _load_records(source_id, opportunity_ids):
    if not opportunity_ids:
        return []
    placeholders = ",".join("?" for _ in opportunity_ids)
    with connect() as db:
        rows = db.execute(f'''SELECT o.id AS opportunity_id,e.metadata_json,a.state,a.reason,
                                     a.checked_at,a.detail_checked_at,a.next_check_at,a.detail_json
                              FROM opportunities o
                              JOIN events e ON e.id=o.event_id
                              LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
                              WHERE o.source_id=? AND o.id IN ({placeholders})
                              ORDER BY o.id''', (source_id, *opportunity_ids)).fetchall()
    return [_record(row) if row["state"] is not None else {
        "opportunity_id": row["opportunity_id"], "state": "not_reviewed",
        "reason": "本轮记录尚未生成自动核验结果", "checked_at": None,
        "detail_checked_at": None, "next_check_at": None,
        "detail_state": "not_checked", "sku_detail_api_state": "not_recorded",
        "missing": [label for label, _ in REQUIRED_EVIDENCE],
        "sku_evidence_complete": False,
    } for row in rows]


def persist_scan_validation(run_id, receipt):
    """Attach the post-sync receipt to the immutable raw scan evidence object."""
    if not run_id:
        return
    with connect() as db:
        row = db.execute("SELECT evidence_json FROM source_scan_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
        evidence["post_sync_validation"] = receipt
        db.execute("UPDATE source_scan_runs SET evidence_json=? WHERE id=?",
                   (json.dumps(evidence, ensure_ascii=False), run_id))


def validate_taobao_syncs(source_results, *, run_detail_cycle, batch_size):
    """Verify the exact official records produced by this synchronization.

    Existing records are included in the receipt. They retain their normal
    retry cooling period, but their already-recorded state is reported instead
    of being silently treated as fresh verification.
    """
    prepared = []
    for source in source_results:
        if source.get("collection_status") in ("complete", "partial") and source.get("run_id"):
            all_ids = _positive_ids(source.get("opportunity_ids"))
            new_ids = _positive_ids(source.get("new_opportunity_ids"))
            prepared.append({"source": source, "all_ids": all_ids, "new_ids": new_ids,
                             "detail_ids": ()})

    # SKU detail is an enhancement. Divide one cycle's total network budget
    # across sources; a large material page must not starve later topics.
    remaining = batch_size
    with_records = [entry for entry in prepared if entry["all_ids"]]
    for index, entry in enumerate(with_records):
        sources_left = len(with_records) - index
        prioritized = _prioritized_ids(entry["new_ids"], entry["all_ids"])
        quota = min(len(prioritized), (remaining + sources_left - 1) // sources_left) if remaining else 0
        entry["detail_ids"] = prioritized[:quota]
        remaining -= len(entry["detail_ids"])

    receipts = []
    for entry in prepared:
        source = entry["source"]
        source_id = source["source_id"]
        all_ids = entry["all_ids"]
        new_ids = entry["new_ids"]
        if not all_ids:
            receipt = {
                "schema": "taobao_post_sync_validation_v1",
                "source_id": source_id,
                "run_id": source["run_id"],
                "synced_count": 0,
                "new_count": 0,
                "attempted_count": 0,
                "accounted_count": 0,
                "sku_evidence_complete_count": 0,
                "complete": True,
                "records": [],
            }
            persist_scan_validation(source["run_id"], receipt)
            receipts.append(receipt)
            continue

        runs = []
        busy = False
        if entry["detail_ids"]:
            result = run_detail_cycle(limit=len(entry["detail_ids"]), source_method="taobao_alliance",
                                      opportunity_ids=entry["detail_ids"])
            runs.append(result)
            busy = bool(result.get("busy"))
        records = _load_records(source_id, all_ids)
        accounted_ids = {record["opportunity_id"] for record in records if record["state"] != "not_reviewed"}
        attempted_count = sum(int(result.get("probed", 0)) for result in runs)
        receipt = {
            "schema": "taobao_post_sync_validation_v1",
            "source_id": source_id,
            "run_id": source["run_id"],
            "synced_count": len(all_ids),
            "new_count": len(new_ids),
            "attempted_count": attempted_count,
            "deferred_count": max(0, len(all_ids) - attempted_count),
            "detail_budget": len(entry["detail_ids"]),
            "accounted_count": len(accounted_ids),
            "unaccounted_opportunity_ids": [item_id for item_id in all_ids if item_id not in accounted_ids],
            "sku_evidence_complete_count": sum(record["sku_evidence_complete"] for record in records),
            "complete": not busy and len(accounted_ids) == len(all_ids),
            "busy": busy,
            "records": records,
        }
        persist_scan_validation(source["run_id"], receipt)
        receipts.append(receipt)
    return receipts
