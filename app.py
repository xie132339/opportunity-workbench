"""Local, source-first Chinese opportunity workbench."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


# Load local settings before importing modules such as db.py that read settings
# during import. Environment variables already supplied by the shell take priority.
load_env()

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
import requests

from db import connect, initialize
from link_resolution import run_cycle as resolve_links, enrich as enrich_links
from merchant_verification import run_cycle as verify_merchant_pages, summarize as summarize_merchant_page
from price_history import for_product as assess_public_price_history
from comparison import load_comparisons, assess_readiness, merchant_identity
from benefits import (listing as list_benefits, run_cycle as refresh_benefits,
                      add as add_benefit, stats as benefit_stats,
                      KINDS as BENEFIT_KINDS, related as related_benefits,
                      linked_counts as linked_benefit_counts,
                      product_match_candidates as match_product_benefits,
                      import_authorized_record)
from autoreview import LABELS as REVIEW_LABELS, review_all, run_cycle, public_offer, offer_summary
from offer import detected_offer_type, RESOURCE_LABELS, TOPIC_LABELS, product_subcategory, paper_package_prices
from pricing import public_market_profit
from xianbao import validate_url as validate_xianbao_url
from scanner import _local_adapter_url, _public_url, scan_all, scan_source, strategy_matches
from notifier import (CHANNEL_LABELS, active_channels, channel_ready, deliver,
                      notification_settings, save_notification_settings)
from xianyu import XianyuError, api as xianyu_api, create_task as create_xianyu_task
from xianyu import import_account as import_xianyu_account, result_url, snapshot as xianyu_snapshot
from xianyu import update_task as update_xianyu_task
from channel_discovery import refresh as refresh_channel_candidates
from channel_discovery import validate_many as validate_channel_candidates
from channel_discovery import promote as promote_channel_candidate

app = Flask(__name__)
app.secret_key = os.environ.get("WORKBENCH_SECRET") or secrets.token_hex(32)


@app.template_filter("cn_time")
def cn_time(value):
    if not value:
        return "尚未记录"
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间")
    except ValueError:
        return str(value)


@app.before_request
def csrf_protection():
    if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
        abort(400, "Invalid form token")


@app.context_processor
def common():
    token = session.setdefault("csrf", secrets.token_urlsafe(24))
    return {"csrf": token, "money": money, "profit": estimated_profit, "review_labels": REVIEW_LABELS,
            "offer_summary": offer_summary, "resource_labels":RESOURCE_LABELS,"topic_labels":TOPIC_LABELS,
            "product_subcategory": product_subcategory, "paper_package_prices": paper_package_prices}


def money(cents):
    return "待核实" if cents is None else f"¥{Decimal(cents) / 100:,.2f}"


def cents(value, required=False):
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError("必填金额不能为空")
        return None
    try:
        n = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("金额格式错误")
    if not n.is_finite() or n < 0 or n.as_tuple().exponent < -2:
        raise ValueError("金额须为非负数，最多两位小数")
    return int(n * 100)


COST_FIELDS = ("buy_cents", "buy_shipping_cents", "sell_shipping_cents",
               "platform_fee_cents", "processing_cents", "other_cents", "reserve_cents")
RECENT_WINDOW = timedelta(hours=2)
CHECKOUT_WINDOW = timedelta(minutes=15)


def estimated_profit(opp, quotes):
    if opp["category"] == "服务与合作":
        return None, "服务商机需先确认资格与合同条件"
    if opp["status"] != "verified" or not opp["specification"]:
        return None, "买入条件与完整规格尚未核实"
    if opp["offer_type"] in ("unknown", "suspected_new_user") or opp["eligibility"] != "eligible":
        return None, "优惠类型或本人账号资格尚未确认"
    try:
        checked = datetime.fromisoformat(opp["buy_checked_at"] or "")
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except ValueError:
        return None, "缺少实际到手价的核实时间"
    if not timedelta(0) <= datetime.now(timezone.utc) - checked <= CHECKOUT_WINDOW:
        return None, "买入价格核实时间在未来或超过 15 分钟，有效性不足"
    missing = [field for field in COST_FIELDS if opp[field] is None]
    if missing:
        return None, "缺少成本：" + "、".join(missing)
    sold = {}
    recycler = []
    today = date.today()
    spec = " ".join(opp["specification"].split()).lower()
    for q in quotes:
        if (not q["same_spec"] or not q["evidence_url"] or not q["conditions"]
                or " ".join(q["specification"].split()).lower() != spec):
            continue
        try:
            price_at = date.fromisoformat(q["price_at"] or "")
        except ValueError:
            continue
        age = today - price_at
        if q["kind"] == "sold" and timedelta(0) <= age <= timedelta(days=30):
            url = q["evidence_url"]
            sold[url] = min(sold.get(url, q["amount_cents"]), q["amount_cents"])
        elif q["kind"] == "recycler" and q["final_quote"] and timedelta(0) <= age <= timedelta(days=7):
            try:
                if date.fromisoformat(q["valid_until"] or "") >= today:
                    recycler.append(q)
            except ValueError:
                pass
    references = []
    if len(sold) >= 3:
        references.append((min(sold.values()), "近 30 天至少 3 笔独立成交，取最低价"))
    if recycler:
        references.append((min(q["amount_cents"] for q in recycler), "仍有效的同规格回收报价，取最低价"))
    if not references:
        return None, "缺少 3 笔近期独立成交，或仍有效的同规格回收报价；挂牌价不计入"
    exit_cents, basis = min(references)
    result = exit_cents - sum(opp[field] for field in COST_FIELDS)
    return result, basis + "；仅为保守价差，实际出售、验机及到账仍需验证"


def public_profit_estimate(opp, quotes, brief, review, mode='sold'):
    """Public-data estimate, separate from account-verified resale authorization."""
    if freshness_state(opp) != 'current':
        mode = mode if mode in ('sold','listing') else 'sold'
        return dict(mode=mode, mode_label={'sold':'保守成交模式','listing':'挂牌参考模式'}[mode],
                    amount_cents=None, reason='来源报价或原文时效未通过；等待来源刷新后自动重算，不能用滞后买入价计算利润。',
                    market_cents=None, buy_cents=None, costs_cents=0, missing_costs=[],
                    category_large=TOPIC_LABELS.get(opp['topic'],opp['category']),
                    category_small=product_subcategory(brief.get('title') or opp['title'],opp['topic']),
                    category_note='大类/小类用于整理与找同款；利润公式统一，不用类目均值替代商品行情。')
    import json
    detail = {}
    if review and review['detail_json']:
        try:
            detail = json.loads(review['detail_json'])
        except (TypeError, ValueError):
            detail = {}
    total = brief.get('total_cents')
    quantity = brief.get('quantity')
    if total is None and review and review['advertised_cents'] is not None:
        total = review['advertised_cents'] * (quantity or 1)
    spec = (brief.get('selected_spec') or detail.get('specification')
            or (review['specification'] if review else '') or opp['specification'])
    shipping = brief.get('audit', {}).get('plan', {}).get('shipping_cents')
    estimate = public_market_profit(opp, quotes, total, spec, mode, shipping)
    estimate['category_large'] = TOPIC_LABELS.get(opp['topic'], opp['category'])
    estimate['category_small'] = product_subcategory(brief.get('title') or opp['title'], opp['topic'])
    estimate['category_note'] = '大类/小类用于整理与找同款；利润公式统一，不用类目均值替代商品行情。'
    return estimate


def historical_buy_assessment(opp, quotes):
    """Compare cash paid including shipping, never advertised or post-rebate prices."""
    if opp["status"] != "verified" or not opp["specification"] or opp["buy_cents"] is None or opp["buy_shipping_cents"] is None:
        return None, "先核实当前同规格现金实付总额（含运费）", False
    if opp["offer_type"] in ("unknown", "suspected_new_user") or opp["eligibility"] != "eligible":
        return None, "先确认优惠类型和本人账号资格", False
    try:
        checked = datetime.fromisoformat(opp["buy_checked_at"] or "")
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if not timedelta(0) <= datetime.now(timezone.utc) - checked <= CHECKOUT_WINDOW:
            return None, "当前实付价核实时间在未来或超过 15 分钟", False
    except ValueError:
        return None, "缺少当前实付价核实时间", False
    spec = " ".join(opp["specification"].split()).lower()
    samples = {}
    today = date.today()
    for q in quotes:
        if q["kind"] != "historical_buy" or not q["same_spec"] or not q["evidence_url"] or not q["conditions"]:
            continue
        if q["offer_type"] != opp["offer_type"]:
            continue
        if " ".join(q["specification"].split()).lower() != spec:
            continue
        try:
            age = today - date.fromisoformat(q["price_at"] or "")
        except ValueError:
            continue
        if timedelta(0) <= age <= timedelta(days=180):
            key = (q["evidence_url"], q["price_at"])
            samples[key] = min(samples.get(key, q["amount_cents"]), q["amount_cents"])
    if len(samples) < 3:
        return None, f"近 180 天只有 {len(samples)} 个同规格、同优惠资格的现金实付样本；至少需要 3 个", False
    current = opp["buy_cents"] + opp["buy_shipping_cents"]
    middle = int(median(samples.values()))
    lowest = min(samples.values())
    difference = lowest - current
    return difference, f"当前含运费 {money(current)}；近 180 天 {len(samples)} 个历史样本中位数 {money(middle)}、最低 {money(lowest)}。历史条件仍需人工逐条核对。", current < lowest


def freshness_state(opp):
    """Published time proves recency; ingestion time alone never does."""
    def utc(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    now = datetime.now(timezone.utc)
    if opp["source_method"] == "manual":
        checked = utc(opp["buy_checked_at"])
        status = opp["opp_status"] if "opp_status" in opp.keys() else opp["status"]
        return "current" if status == "verified" and checked and timedelta(0) <= now - checked <= CHECKOUT_WINDOW else "unknown"
    if not opp["source_enabled"] or opp["source_status"] != "healthy":
        return "source_stale"
    max_lag = min(timedelta(minutes=2 * int(opp["source_interval"]) + 15), RECENT_WINDOW)
    checked = utc(opp["source_last_success"])
    seen = utc(opp["last_seen_at"])
    if not checked or not seen or not (timedelta(0) <= now - checked <= max_lag and timedelta(0) <= now - seen <= max_lag):
        return "source_stale"
    published = utc(opp["published_at"])
    if not published:
        return "unknown"
    if published > now:
        return "future"
    if now - published > RECENT_WINDOW:
        return "old"
    return "current"


@app.template_filter("freshness")
def freshness_label(opp):
    return {"current": "原文近 2 小时且来源仍在更新", "unknown": "原文时间未知，须核实",
            "future": "原文时间晚于当前，须核实", "old": "原文超过 2 小时",
            "source_stale": "来源或条目检查已超时"}.get(freshness_state(opp), "时效未知")


def resale_assessment(opp, quotes, rules=None):
    """One gate shared by listing, detail and send-time revalidation (D01/D02/D08)."""
    if "auto_state" in opp.keys() and opp["auto_state"] in ('excluded','stale','source_unavailable','retry','conflict'):
        return False, "自动检查已隔离此线索，不进入转售候选"
    if freshness_state(opp) != "current":
        return False, "线索时效或来源状态不满足要求"
    profit, basis = estimated_profit(opp, quotes)
    if profit is None:
        return False, basis
    if profit <= 0:
        return False, "扣除已录入成本后的保守价差不为正"
    if rules is None:
        with connect() as db:
            rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
    matched = [r for r in rules if strategy_matches(r, opp["title"], opp["category"])]
    configured = [r for r in matched if r["min_profit_cents"] is not None and r["min_profit_cents"] > 0
                  and r["max_buy_cents"] is not None and r["max_buy_cents"] > 0]
    if not configured:
        return False, "没有匹配且启用的完整转售策略：需设置正数最低净利及含买入运费预算上限"
    buy_total = opp["buy_cents"] + opp["buy_shipping_cents"]
    for rule in configured:
        if profit >= rule["min_profit_cents"] and buy_total <= rule["max_buy_cents"]:
            return True, (f"满足策略「{rule['name']}」：价差 {money(profit)} ≥ 最低净利 {money(rule['min_profit_cents'])}；"
                          f"买入含运费 {money(buy_total)} ≤ 预算 {money(rule['max_buy_cents'])}。"
                          "历史低价独立展示；回报率、周转、资金占用与退出承接量尚未完整验证，不代表可执行盈利。")
    return False, "未同时满足同一条策略的最低净利和含运费买入预算；不会拼接不同策略的门槛"


def is_evidence_candidate(opp, quotes, rules=None):
    return resale_assessment(opp, quotes, rules)[0]


def is_current_notice(row):
    return (row['opp_status'] not in ('ignored','expired')
            and row['auto_state'] in ('observed','conditional','activity')
            and freshness_state(row) == 'current')


def notice_rows(db, status=None, limit=None):
    sql = """SELECT n.*,o.title,o.status AS opp_status,o.buy_checked_at,
             e.published_at,e.last_seen_at,s.method AS source_method,
             s.status AS source_status,s.enabled AS source_enabled,
             s.interval_minutes AS source_interval,s.last_success AS source_last_success,a.state AS auto_state
             FROM notifications n JOIN opportunities o ON o.id=n.opportunity_id
             JOIN events e ON e.id=n.event_id JOIN sources s ON s.id=o.source_id
             LEFT JOIN auto_reviews a ON a.opportunity_id=o.id"""
    args = []
    if status:
        sql += " WHERE n.status=?"
        args.append(status)
    sql += " ORDER BY n.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return db.execute(sql, args).fetchall()


def dispatch_verified_alerts():
    """Fan out only evidence-backed candidates; failed sends require manual retry."""
    channels = active_channels()
    if not channels:
        return {"eligible": 0, "sent": 0, "failed": 0}
    eligible = 0
    with connect() as db:
        rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
        rows = db.execute("""SELECT o.*,e.id AS source_event_id,e.published_at,e.last_seen_at,
                             s.method AS source_method,s.status AS source_status,
                             s.enabled AS source_enabled,s.interval_minutes AS source_interval,
                             s.last_success AS source_last_success,a.state AS auto_state
                             FROM opportunities o JOIN events e ON e.id=o.event_id
                             JOIN sources s ON s.id=o.source_id
                             LEFT JOIN auto_reviews a ON a.opportunity_id=o.id WHERE o.status='verified'""").fetchall()
        for opp in rows:
            quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (opp["id"],)).fetchall()
            if not is_evidence_candidate(opp, quotes, rules):
                continue
            eligible += 1
            db.execute("""INSERT OR IGNORE INTO notifications (event_id,opportunity_id)
                          VALUES(?,?)""", (opp["source_event_id"], opp["id"]))
            notice_id = db.execute("SELECT id FROM notifications WHERE event_id=?",
                                   (opp["source_event_id"],)).fetchone()[0]
            for channel in channels:
                db.execute("""INSERT OR IGNORE INTO notification_deliveries
                              (notification_id,channel) VALUES(?,?)""", (notice_id, channel))
        tasks = db.execute("""SELECT d.id,d.channel,o.id AS opportunity_id,o.title,o.url
                              FROM notification_deliveries d
                              JOIN notifications n ON n.id=d.notification_id
                              JOIN opportunities o ON o.id=n.opportunity_id
                              WHERE d.status='pending' ORDER BY d.id LIMIT 20""").fetchall()
    sent = failed = 0
    for task in tasks:
        with connect() as db:
            opp = db.execute("""SELECT o.*,e.published_at,e.last_seen_at,
                s.method AS source_method,s.status AS source_status,s.enabled AS source_enabled,
                s.interval_minutes AS source_interval,s.last_success AS source_last_success,a.state AS auto_state
                FROM opportunities o JOIN events e ON e.id=o.event_id
                JOIN sources s ON s.id=o.source_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
                WHERE o.id=?""", (task["opportunity_id"],)).fetchone()
            quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (task["opportunity_id"],)).fetchall()
        profit, _ = estimated_profit(opp, quotes)
        allowed, gate_basis = resale_assessment(opp, quotes)
        if not allowed or task["channel"] not in active_channels():
            with connect() as db:
                db.execute("UPDATE notification_deliveries SET status='skipped',last_error='条件已变化或通道已关闭' WHERE id=?",
                           (task["id"],))
            continue
        title = "转售测算达标线索：" + task["title"][:80]
        body = f"{gate_basis}\n保守价差 {money(profit)}；不代表已成交或已到账。\n原始链接：{task['url'] or '无'}"
        try:
            deliver(task["channel"], title, body)
            with connect() as db:
                db.execute("""UPDATE notification_deliveries SET status='sent',attempts=attempts+1,
                              sent_at=CURRENT_TIMESTAMP,last_error=NULL WHERE id=?""", (task["id"],))
            sent += 1
        except Exception as exc:
            with connect() as db:
                db.execute("""UPDATE notification_deliveries SET status='failed',attempts=attempts+1,
                              last_error=? WHERE id=?""", (str(exc)[:180], task["id"]))
            failed += 1
    return {"eligible": eligible, "sent": sent, "failed": failed}


def go(endpoint, **kwargs):
    return redirect(url_for(endpoint, **kwargs))


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1").fetchone()
    return jsonify({"status": "ok", "database": "ok", "monitor_api_configured": bool(os.environ.get("CHANGED_API_TOKEN"))})


@app.get("/")
def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    platform = request.args.get("platform", "").strip()
    offer_filter = request.args.get("offer", "").strip()
    topic = request.args.get('topic','')
    resource = request.args.get('resource','')
    budget_text = request.args.get("budget", "").strip()
    sort_mode = request.args.get("sort", "comparison")
    if sort_mode not in ("comparison", "latest", "price_low", "price_high", "paper_unit_low"):
        sort_mode = "comparison"
    layout_mode = request.args.get("layout", "list")
    if layout_mode not in ("list", "cards"):
        layout_mode = "list"
    page_size = 100 if layout_mode == "list" else 36
    candidate_only = request.args.get("candidate") == "1"
    # The main product search is the evidence-qualified view. Keep the broad
    # fresh-lead inbox explicitly available so incomplete posts are not destroyed
    # or silently promoted to products.
    view_mode = request.args.get("view") or "ready"
    if view_mode not in ("ready", "current", "all"):
        view_mode = "ready"
    show_archive = view_mode == "all"
    ready_only = view_mode == "ready"
    try:
        budget = cents(budget_text) if budget_text else None
    except ValueError:
        budget = None
    sql = """SELECT o.*,s.platform,s.status AS source_status,s.method AS source_method,
             s.enabled AS source_enabled,s.interval_minutes AS source_interval,s.name AS source_name,
             s.last_success AS source_last_success,
             e.is_baseline,e.published_at,e.last_seen_at,e.snippet,e.metadata_json,
             a.state AS auto_state,a.reason AS auto_reason,a.advertised_cents,
             a.specification AS auto_specification,a.conditions AS auto_conditions,a.detail_json,
             a.checked_at AS review_checked_at,a.detail_checked_at,a.detail_error
             FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id
             LEFT JOIN events e ON e.id=o.event_id
             LEFT JOIN auto_reviews a ON a.opportunity_id=o.id WHERE 1=1"""
    args = []
    if topic in TOPIC_LABELS:
        sql += ' AND o.topic=?'
        args.append(topic)
    if resource in RESOURCE_LABELS:
        sql += ' AND o.resource_kind=?'
        args.append(resource)
    if query:
        # A single character is noisy in RSS copy. Allow source-copy-only hits
        # only when the same captured text contains a price marker; the title
        # remains searchable without that extra requirement.
        if len(query) == 1:
            sql += " AND (o.title LIKE ? OR (COALESCE(e.snippet,'') LIKE ? AND (COALESCE(e.snippet,'') LIKE '%元%' OR COALESCE(e.snippet,'') LIKE '%¥%' OR COALESCE(e.snippet,'') LIKE '%￥%')))"
            args.extend(("%" + query + "%", "%" + query + "%"))
            if query == '纸':
                # Common non-paper compound terms in deal feeds.
                for unrelated_title_term in ('纸皮', '响纸'):
                    sql += " AND o.title NOT LIKE ?"
                    args.append('%' + unrelated_title_term + '%')
        else:
            sql += " AND (o.title LIKE ? OR COALESCE(e.snippet,'') LIKE ?)"
            args.extend(("%" + query + "%", "%" + query + "%"))
    if category:
        sql += " AND o.category=?"
        args.append(category)
    if platform:
        sql += " AND s.platform=?"
        args.append(platform)
    if offer_filter == "new_user":
        sql += " AND o.offer_type IN ('suspected_new_user','new_user')"
    elif offer_filter in ("standard", "other_restricted", "unknown"):
        sql += " AND o.offer_type=?"
        args.append(offer_filter)
    if budget is not None:
        sql += " AND a.advertised_cents IS NOT NULL AND a.advertised_cents<=? AND a.state NOT IN ('conflict','excluded')"
        args.append(budget)
    if status:
        sql += " AND o.status=?"
        args.append(status)
    else:
        sql += " AND o.status!='ignored'"
    if not show_archive:
        if not candidate_only:
            # Product search and the lead inbox start from fresh purchase-like source
            # records. Product search applies the shared evidence gate after enrichment;
            # the current view intentionally keeps incomplete leads.
            sql += " AND o.resource_kind IN ('purchase','unknown')"
        sql += " AND COALESCE(a.state,'queued') NOT IN ('stale','source_unavailable','retry','excluded')"
        sql += """ AND ((s.method='manual' AND o.status='verified'
                         AND datetime(o.buy_checked_at) BETWEEN datetime('now','-15 minutes') AND CURRENT_TIMESTAMP)
                     OR (s.method!='manual' AND s.enabled=1 AND s.status='healthy'
                         AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                         AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                         AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP))"""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    with connect() as db:
        comparisons = load_comparisons(db)
        link_cache={r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')}
        order_by = """ ORDER BY CASE o.category
            WHEN '零售优惠' THEN 1 WHEN '二手与闲置' THEN 2
            WHEN '供货与清仓' THEN 3 WHEN '新品与补货' THEN 4
            WHEN '拍卖与资产' THEN 5 WHEN '服务与合作' THEN 6 ELSE 7 END,
            o.created_at DESC,o.id DESC"""
        if candidate_only:
            rules = db.execute("SELECT * FROM strategies WHERE enabled=1").fetchall()
            candidates = []
            for row in db.execute(sql + " AND o.status='verified'" + order_by, args):
                quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (row["id"],)).fetchall()
                if is_evidence_candidate(row, quotes, rules):
                    candidates.append(row)
            total = len(candidates)
            source_match_counts = {}
            for matched_row in candidates:
                source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            rows = [dict(row) for row in candidates[(page - 1) * page_size:page * page_size]]
            search_assessments={}
            quality_counts=dict(search_ready=0,comparable=0,source_claim_low=0,excluded=0)
            candidate_pool_count=total
            source_offer_count=total
        else:
            all_rows = [enrich_links(dict(row),link_cache) for row in db.execute(sql + order_by,args)]
            source_match_counts = {}
            for matched_row in all_rows:
                source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            if not show_archive and not ready_only and not candidate_only:
                # Do not make parsability a visibility gate. Price, quantity,
                # specifications and promotion certainty are readiness evidence,
                # not reasons to hide a fresh lead from the user's search.
                source_offer_count = len(all_rows)
                grouped = {}
                for row in all_rows:
                    brief = offer_summary(row['title'], row['url'], row.get('snippet'), row.get('auto_conditions'),
                                          row.get('metadata_json'), row.get('detail_json'),
                                          dict(checked_at=row.get('review_checked_at'),
                                               detail_checked_at=row.get('detail_checked_at'),
                                               detail_error=row.get('detail_error')))
                    identity = (''.join(ch for ch in row['title'].casefold() if ch.isalnum()),
                                brief.get('selected_spec'), brief.get('total_cents'), brief.get('quantity'),
                                row.get('auto_state'), row.get('resource_kind'))
                    platform_name = (row.get('platform') or '').strip()
                    feed_name = (row.get('source_name') or '').strip()
                    source_label = f'{platform_name} · {feed_name}' if platform_name and feed_name and feed_name != platform_name else (platform_name or feed_name)
                    names = {source_label} if source_label else {'人工录入'}
                    score = (bool(row.get('detail_checked_at')), len(row.get('auto_conditions') or row.get('snippet') or ''))
                    if identity not in grouped:
                        row['source_channels'] = names
                        row['_evidence_score'] = score
                        grouped[identity] = row
                    else:
                        existing = grouped[identity]
                        existing['source_channels'].update(names)
                        if score > existing['_evidence_score']:
                            row['source_channels'] = existing['source_channels']
                            row['_evidence_score'] = score
                            grouped[identity] = row
                all_rows = list(grouped.values())
                for row in all_rows:
                    row['source_channels'] = sorted(row.pop('source_channels'))
                    row.pop('_evidence_score', None)
                    row['source_count'] = len(row['source_channels'])
            else:
                source_offer_count = len(all_rows)
                # Archive rows are not deduplicated, but still identify the exact
                # configured feed so separate sources on one platform stay visible.
                for row in all_rows:
                    platform_name = (row.get('platform') or '').strip()
                    feed_name = (row.get('source_name') or '').strip()
                    source_label = f'{platform_name} · {feed_name}' if platform_name and feed_name and feed_name != platform_name else (platform_name or feed_name or '人工录入')
                    row['source_channels'] = [source_label]
                    row['source_count'] = 1
            candidate_pool_count=len(all_rows)
            search_assessments={row['id']:assess_readiness(row,comparisons.get(row['id'],{})) for row in all_rows}
            quality_counts=dict(
                search_ready=sum(bool(value['search_ready']) for value in search_assessments.values()),
                comparable=sum(bool(value['comparable']) for value in search_assessments.values()),
                source_claim_low=sum(bool(value['source_claim_low']) for value in search_assessments.values()))
            quality_counts['excluded']=candidate_pool_count-quality_counts['search_ready']
            if ready_only and not show_archive:
                all_rows=[row for row in all_rows if search_assessments[row['id']]['search_ready']]
                source_match_counts = {}
                for matched_row in all_rows:
                    source_match_counts[matched_row['source_id']] = source_match_counts.get(matched_row['source_id'], 0) + 1
            total=len(all_rows)
            if sort_mode == 'comparison':
                all_rows.sort(key=lambda r: comparisons.get(r['id'],{}).get('rank',0),reverse=True)
            elif sort_mode == 'latest':
                all_rows.sort(key=lambda r: (r.get('published_at') or '', r['id']), reverse=True)
            elif sort_mode in ('price_low', 'price_high', 'paper_unit_low'):
                priced, unpriced = [], []
                for row in all_rows:
                    brief = offer_summary(row['title'], row['url'], row.get('snippet'), row.get('auto_conditions'),
                                          row.get('metadata_json'), row.get('detail_json'),
                                          dict(checked_at=row.get('review_checked_at'),
                                               detail_checked_at=row.get('detail_checked_at'),
                                               detail_error=row.get('detail_error')))
                    value = brief.get('total_cents')
                    if value is None:
                        value = row.get('advertised_cents')
                    if sort_mode == 'paper_unit_low':
                        title = brief.get('title') or row['title']
                        paper = paper_package_prices(title, brief.get('total_cents'), brief.get('quantity'))
                        value = paper.get('per_pack_cents') if paper else None
                    if value is None:
                        unpriced.append(row)
                    else:
                        row['_sort_price_cents'] = value
                        priced.append(row)
                priced.sort(key=lambda r: (r['_sort_price_cents'], r.get('published_at') or '', r['id']),
                            reverse=sort_mode == 'price_high')
                for row in priced:
                    row.pop('_sort_price_cents', None)
                all_rows = priced + unpriced
            rows=all_rows[(page-1)*page_size:page*page_size]
        sources = [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        source_coverage = [dict(source, match_count=source_match_counts.get(source['id'], 0)) for source in sources]
        source_coverage.sort(key=lambda source: (-int(bool(source['enabled'])), -source['match_count'], source['platform'], source['name']))
        enabled_source_count = sum(bool(source['enabled']) for source in sources)
        matched_enabled_source_count = sum(bool(source['enabled']) and source['match_count'] > 0 for source in source_coverage)
        alerts = sum(is_current_notice(row) for row in notice_rows(db, "pending"))
        benefit_link_counts=linked_benefit_counts(db,[row['id'] for row in rows])
        merchant_checks={(r['opportunity_id'],r['product_url']):dict(r) for r in db.execute(
            'SELECT * FROM merchant_page_checks WHERE opportunity_id IN (' + ','.join('?' for _ in rows) + ')',
            [row['id'] for row in rows]).fetchall()} if rows else {}
        for row in rows:
            row['merchant_page_check']=summarize_merchant_page(dict(row),merchant_checks)
        merchant_page_counts={}
        for row in rows:
            state=row['merchant_page_check']['state']
            merchant_page_counts[state]=merchant_page_counts.get(state,0)+1
    return render_template("index.html", rows=rows, sources=sources,
                           benefit_link_counts=benefit_link_counts,
                           comparisons=comparisons,sort_mode=sort_mode,
                           search_assessments=search_assessments,quality_counts=quality_counts,
                           merchant_page_counts=merchant_page_counts,
                           candidate_pool_count=candidate_pool_count, source_offer_count=source_offer_count,
                           alerts=alerts, query=query, category=category, status=status,
                           platform=platform, budget_text=budget_text,
                           offer_filter=offer_filter,topic=topic,resource=resource,
                           total=total, page=page, has_next=page * page_size < total,
                           page_size=page_size, layout_mode=layout_mode,
                           source_coverage=source_coverage, enabled_source_count=enabled_source_count,
                           matched_enabled_source_count=matched_enabled_source_count,
                           candidate_only=candidate_only, show_archive=show_archive, view_mode=view_mode, ready_only=ready_only)


@app.get("/benefits")
def benefits_page():
    query=request.args.get('q','').strip()
    benefit_kind=request.args.get('kind','').strip()
    if benefit_kind not in BENEFIT_KINDS:benefit_kind=''
    try:page=max(1,int(request.args.get('page','1')))
    except ValueError:page=1
    page_size=20
    with connect() as db:
        rows=list_benefits(db,query,benefit_kind,limit=page_size+1,offset=(page-1)*page_size)
        summary=benefit_stats(db)
    return render_template('benefits.html',benefits=rows[:page_size],benefits_summary=summary,
                           query=query,benefit_kind=benefit_kind,page=page,
                           has_next=len(rows)>page_size)


@app.get("/verification")
def verification():
    state = request.args.get('state', '')
    with connect() as db:
        counts = dict(db.execute('SELECT state,COUNT(*) FROM auto_reviews GROUP BY state').fetchall())
        run = db.execute('SELECT * FROM review_runs WHERE id=1').fetchone()
        rows = db.execute('''SELECT a.*,o.title,o.url FROM auto_reviews a
            JOIN opportunities o ON o.id=a.opportunity_id WHERE (?='' OR a.state=?)
            ORDER BY o.id DESC LIMIT 100''', (state,state)).fetchall()
    from acceptance import QUERY,evaluate,freeze
    with connect() as db:
        evidence=[dict(r) for r in db.execute(QUERY)]
        cache={r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')}
    cohort=freeze(evidence,current_only=True)
    acceptance=evaluate(evidence,cohort,cache)
    return render_template('verification.html',counts=counts,run=run,rows=rows,state=state,acceptance=acceptance)


@app.post("/manual")
def manual():
    title = request.form.get("title", "").strip()
    url = request.form.get("url", "").strip()
    platform = request.form.get("platform", "").strip()
    category = request.form.get("category", "").strip()
    if not title or not platform or not category:
        flash("标题、平台、类别不能为空", "error")
        return go("index")
    try:
        _public_url(url)
    except ValueError as exc:
        flash(str(exc), "error")
        return go("index")
    with connect() as db:
        db.execute("""INSERT OR IGNORE INTO sources
            (platform,name,category,url,method,parser,status,enabled)
            VALUES(?,?,?,?,'manual','','manual',0)""",
            (platform, "人工线索", category, url))
        source_id = db.execute("SELECT id FROM sources WHERE platform=? AND url=? AND method='manual'",
                               (platform, url)).fetchone()[0]
        key = hashlib.sha256((title + url).encode()).hexdigest()
        event = db.execute("""INSERT OR IGNORE INTO events
            (source_id,external_key,title,url,snippet,fingerprint)
            VALUES(?,?,?,?,?,?)""", (source_id,url,title,url,"人工录入",key))
        if event.rowcount:
            db.execute("""INSERT INTO opportunities
                (event_id,source_id,title,category,url,offer_type) VALUES(?,?,?,?,?,?)""",
                (event.lastrowid,source_id,title,category,url,detected_offer_type(title)))
    from offer import resource_kind
    if resource_kind(title) in BENEFIT_KINDS:add_benefit(title,url,source_type='manual',source_url=url)
    flash("人工线索已保存；价格、规格和利润仍需核实", "ok")
    return go("index")


@app.get("/sources")
def sources():
    with connect() as db:
        rows = db.execute("""SELECT s.*,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id) event_count,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id
                               AND s.enabled=1 AND s.status='healthy'
                               AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                               AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
                               AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')) recent_count,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id AND e.published_at IS NULL) unknown_count,
                             (SELECT COUNT(*) FROM events e WHERE e.source_id=s.id AND e.published_at>CURRENT_TIMESTAMP) future_count
                             FROM sources s ORDER BY s.enabled DESC,s.id""").fetchall()
        candidates = db.execute("""SELECT * FROM source_candidates
            ORDER BY CASE state WHEN 'validated' THEN 0 WHEN 'discovered' THEN 1
                       WHEN 'failed' THEN 2 WHEN 'rejected' THEN 3 ELSE 4 END,
                     updated_at DESC,id DESC""").fetchall()
    return render_template("sources.html", rows=rows, candidates=candidates)


@app.post("/sources/candidates/refresh")
def source_candidates_refresh():
    try:
        result = refresh_channel_candidates()
        flash(f"GitHub 候选目录已刷新：检索 {result['matched_files']} 个路由文件，"
              f"记录 {result['discovered']} 个相关候选，其中 {result['rejected']} 个因账号或浏览器要求被隔离；"
              f"{result['fetch_errors']} 个源码文件本轮读取失败，可下次重试", "ok")
    except Exception as exc:
        flash("GitHub 候选刷新失败：" + str(exc)[:300], "error")
    return go("sources")


@app.post("/sources/candidates/bulk")
def source_candidates_bulk():
    action = request.form.get("action", "")
    raw_ids = request.form.getlist("candidate_ids")
    if action not in ("validate", "promote") or not raw_ids or len(raw_ids) > 50:
        flash("请选择 1 到 50 个候选渠道和有效操作", "error")
        return go("sources")
    try:
        if any(not value.isdecimal() or int(value) <= 0 for value in raw_ids):
            raise ValueError("候选渠道编号无效")
        ids = sorted(set(int(value) for value in raw_ids))
        if action == "validate":
            results = validate_channel_candidates(ids)
            passed = sum(result["state"] == "validated" for result in results)
            items = sum(result["items"] for result in results)
            flash(f"已实测 {len(results)} 个候选；通过 {passed} 个，共解析 {items} 条当前数据",
                  "ok" if passed == len(results) else "error")
        else:
            source_ids = [promote_channel_candidate(candidate_id) for candidate_id in ids]
            outcomes = [scan_source(source_id) for source_id in source_ids]
            healthy = sum(item["status"] == "healthy" for item in outcomes)
            flash(f"已接入 {len(source_ids)} 个渠道并立即采集；正常 {healthy} 个，"
                  f"新增 {sum(item['new'] for item in outcomes)} 条", "ok" if healthy == len(outcomes) else "error")
    except Exception as exc:
        flash(str(exc)[:300], "error")
    return go("sources")


@app.get("/xianyu")
def xianyu():
    accounts, tasks, files, error = [], [], [], None
    try:
        accounts, tasks, files = xianyu_snapshot()
    except XianyuError as exc:
        error = str(exc)
    with connect() as db:
        linked = {row[0] for row in db.execute("SELECT url FROM sources WHERE method='goofish'")}
    return render_template("xianyu.html", accounts=accounts, tasks=tasks,
                           files=files, linked=linked, error=error, result_url=result_url)


@app.post("/xianyu/accounts")
def xianyu_account():
    try:
        if request.content_length and request.content_length > 600_000:
            abort(413)
        import_xianyu_account(request.form.get("name", "").strip(),
                              request.form.get("content", ""))
        flash("闲鱼登录态已保存到本机采集服务；页面不会回显内容", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")


@app.post("/xianyu/tasks")
def xianyu_task_add():
    try:
        create_xianyu_task(request.form)
        flash("闲鱼搜索任务已创建；手动启动后才会访问闲鱼", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")


@app.post("/xianyu/tasks/<int:task_id>/<action>")
def xianyu_task_action(task_id, action):
    if action not in ("start", "stop", "update"):
        abort(404)
    try:
        tasks = xianyu_api("GET", "/api/tasks")
        task = next((item for item in tasks if item.get("id") == task_id), None)
        if not task:
            raise XianyuError("任务不存在")
        if action == "update":
            update_xianyu_task(task_id, task, request.form)
            flash("任务条件已保存", "ok")
            return go("xianyu")
        if action == "start":
            if not task.get("enabled"):
                raise XianyuError("任务已暂停，请先编辑条件并启用")
            account_file = task.get("account_state_file")
            accounts = xianyu_api("GET", "/api/accounts")
            if not account_file or not any(item.get("path") == account_file for item in accounts):
                raise XianyuError("任务没有可用的本机闲鱼账号，请先导入登录态")
        xianyu_api("POST", f"/api/tasks/{action}/{task_id}")
        flash("任务已启动，结果出现后可在本页接入工作台" if action == "start" else "任务已停止", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")


@app.post("/xianyu/results")
def xianyu_result_add():
    filename = request.form.get("filename", "")
    try:
        files = xianyu_api("GET", "/api/results/files").get("files", [])
        if filename not in files:
            raise XianyuError("该闲鱼结果尚未产生")
        url = result_url(filename)
        with connect() as db:
            db.execute("""INSERT OR IGNORE INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                ("闲鱼", "搜索结果 · " + filename.removesuffix("_full_data.jsonl"),
                 "二手与闲置", url, "goofish", "", "pending", 1, 60))
            source_id = db.execute("SELECT id FROM sources WHERE platform='闲鱼' AND url=? AND method='goofish'",
                                   (url,)).fetchone()[0]
        outcome = scan_source(source_id)
        if outcome["status"] != "healthy":
            raise XianyuError("结果来源已登记，但读取失败：" + outcome.get("error", "未知错误"))
        flash(f"闲鱼结果已接入工作台，本次读取 {outcome['seen']} 条；挂牌价仍待核实", "ok")
    except XianyuError as exc:
        flash(str(exc), "error")
    return go("xianyu")


@app.post("/sources")
def source_add():
    platform = request.form.get("platform", "").strip()
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    url = request.form.get("url", "").strip()
    method = request.form.get("method", "manual")
    parser = request.form.get("parser", "") if method == "html" else ""
    parser_hosts = {"smzdm": "www.smzdm.com", "apple": "www.apple.com.cn",
                    "mi": "www.mi.com", "ccgp": "www.ccgp.gov.cn",
                    "yiwugo": "www.yiwugo.com", "kongfz": "book.kongfz.com",
                    "suning": "www.suning.com", "lenovo": "www.lenovo.com.cn",
                    "honor": "www.honor.com"}
    if not all((platform, name, category)) or method not in ("manual", "monitor", "rss", "rsshub", "goofish", "html", "xianbao"):
        flash("来源信息不完整", "error")
        return go("sources")
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        minimum = 1 if method=='xianbao' else 5
        if not minimum <= interval <= 1440:
            raise ValueError(f"检查间隔须在 {minimum} 到 1440 分钟之间")
        if method == 'xianbao':
            validate_xianbao_url(url)
        elif method == "rsshub":
            _local_adapter_url(url, os.environ.get("RSSHUB_BASE", "http://127.0.0.1:1200"), "/")
        elif method == "goofish":
            _local_adapter_url(url, os.environ.get("GOOFISH_BASE", "http://127.0.0.1:8000"), "/api/results/")
            if not urlparse(url).path.endswith(".jsonl"):
                raise ValueError("闲鱼结果地址须以 .jsonl 结尾")
        else:
            _public_url(url)
        if method == "html" and (parser not in parser_hosts or urlparse(url).hostname != parser_hosts[parser]):
            raise ValueError("公开网页解析须选择与入口域名一致的已适配站点")
        with connect() as db:
            if db.execute("SELECT 1 FROM sources WHERE platform=? AND url=? AND method=?",
                          (platform,url,method)).fetchone():
                raise ValueError("该平台和入口已经添加")
        watch_uuid = None
        if method == "monitor":
            token = os.environ.get("CHANGED_API_TOKEN")
            if not token:
                raise ValueError("未配置监控 API 凭据")
            resp = requests.post("http://127.0.0.1:5001/api/v1/watch",
                                 headers={"x-api-key": token},
                                 json={"url": url, "title": name}, timeout=15)
            resp.raise_for_status()
            watch_uuid = resp.json()["uuid"]
        with connect() as db:
            db.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,watch_uuid,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (platform,name,category,url,method,parser,watch_uuid,"pending" if method != "manual" else "manual",
                 int(method != "manual"),interval))
        flash("来源已保存", "ok")
    except Exception as exc:
        flash("来源保存失败：" + str(exc)[:180], "error")
    return go("sources")


@app.post("/sources/<int:source_id>/toggle")
def source_toggle(source_id):
    with connect() as db:
        db.execute("""UPDATE sources SET enabled=1-enabled,
            status=CASE WHEN enabled=1 THEN 'paused' ELSE 'pending' END WHERE id=?""", (source_id,))
    return go("sources")


@app.post("/sources/bulk")
def source_bulk():
    action = request.form.get("action", "")
    raw_ids = request.form.getlist("source_ids")
    if action not in ("enable", "pause", "interval", "scan") or not raw_ids or len(raw_ids) > 50:
        flash("请选择 1 到 50 个渠道和有效的批量操作", "error")
        return go("sources")
    try:
        if any(not value.isdecimal() or int(value) <= 0 for value in raw_ids):
            raise ValueError("渠道编号无效")
        ids = sorted(set(int(value) for value in raw_ids))
        placeholders = ",".join("?" for _ in ids)
        with connect() as db:
            rows = db.execute(f"SELECT id,method,enabled FROM sources WHERE id IN ({placeholders})", ids).fetchall()
            if len(rows) != len(ids) or any(row["method"] == "manual" for row in rows):
                raise ValueError("所选渠道不存在或为人工来源，无法批量修改")
            if action == "scan":
                if any(not row["enabled"] for row in rows):
                    raise ValueError("请先启用所选渠道，再批量检查")
            elif action == "interval":
                interval = int(request.form.get("interval_minutes", ""))
                minimum = 1 if all(row['method']=='xianbao' for row in rows) else 5
                if not minimum <= interval <= 1440:
                    raise ValueError(f"检查间隔须在 {minimum} 到 1440 分钟之间")
                db.execute(f"UPDATE sources SET interval_minutes=? WHERE id IN ({placeholders})", [interval, *ids])
            elif action == "enable":
                db.execute(f"UPDATE sources SET enabled=1,status=CASE WHEN enabled=0 THEN 'pending' ELSE status END WHERE id IN ({placeholders})", ids)
            else:
                db.execute(f"UPDATE sources SET enabled=0,status='paused' WHERE id IN ({placeholders})", ids)
        if action == "scan":
            results = [scan_source(source_id) for source_id in ids]
            healthy = sum(result["status"] == "healthy" for result in results)
            flash(f"已检查 {len(results)} 个渠道；正常 {healthy} 个，新增 {sum(result['new'] for result in results)} 条" ,
                  "ok" if healthy == len(results) else "error")
        else:
            label = {"enable": "启用", "pause": "暂停", "interval": "更新频率"}[action]
            flash(f"已批量{label} {len(ids)} 个渠道", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("sources")


@app.post("/sources/<int:source_id>/interval")
def source_interval(source_id):
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        with connect() as db:
            source = db.execute('SELECT method FROM sources WHERE id=?',(source_id,)).fetchone()
            if source is None:raise ValueError('来源不存在')
            minimum = 1 if source['method']=='xianbao' else 5
            if not minimum <= interval <= 1440:raise ValueError(f'检查间隔须在 {minimum} 到 1440 分钟之间')
            db.execute("UPDATE sources SET interval_minutes=? WHERE id=?", (interval,source_id))
        flash("检查间隔已更新", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("sources")


@app.post("/sources/<int:source_id>/scan")
def scan_one(source_id):
    try:
        outcome = scan_source(source_id)
        flash(f"检查结果：{outcome['status']}；本次新增 {outcome['new']} 条" +
              ("；首次采集为基线" if outcome.get("baseline") else "") +
              ("；" + outcome["error"] if outcome.get("error") else ""),
              "ok" if outcome["status"] == "healthy" else "error")
    except Exception as exc:
        flash(str(exc), "error")
    return go("sources")


@app.post("/sources/scan-all")
def scan_everything():
    results = scan_all()
    flash(f"检查 {len(results)} 个来源；成功 {sum(x['status']=='healthy' for x in results)} 个；新增 {sum(x['new'] for x in results)} 条", "ok")
    return go("sources")


@app.get("/opportunities/<int:opportunity_id>")
def opportunity(opportunity_id):
    with connect() as db:
        opp = db.execute("""SELECT o.*,s.platform,s.name AS source_name,
                             s.method AS source_method,s.status AS source_status,
                             s.enabled AS source_enabled,s.interval_minutes AS source_interval,
                             s.last_success AS source_last_success,
                             e.snippet,e.metadata_json,e.is_baseline,e.observed_at,e.published_at,e.last_seen_at,a.state AS auto_state
                             FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id
                             LEFT JOIN events e ON e.id=o.event_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
                             WHERE o.id=?""", (opportunity_id,)).fetchone()
        if not opp:
            abort(404)
        quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=? ORDER BY observed_at DESC,id DESC",
                            (opportunity_id,)).fetchall()
        buy_checks = db.execute("SELECT * FROM buy_checks WHERE opportunity_id=? ORDER BY checked_at DESC,id DESC LIMIT 30",
                                (opportunity_id,)).fetchall()
        trades = db.execute("SELECT * FROM trades WHERE opportunity_id=? ORDER BY id DESC", (opportunity_id,)).fetchall()
        review = db.execute("SELECT * FROM auto_reviews WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        comparison = load_comparisons(db).get(opportunity_id,{})
        identity_row=dict(opp,detail_json=review['detail_json'] if review else '{}')
        resolved= enrich_links(identity_row,{r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')})
        opp=dict(opp,metadata_json=resolved['metadata_json'])
        brief=offer_summary(opp['title'],opp['url'],opp['snippet'],metadata=opp['metadata_json'],detail_json=review['detail_json'] if review else None,
                            sync_meta=dict(checked_at=review['checked_at'],detail_checked_at=review['detail_checked_at'],detail_error=review['detail_error']) if review else {})
        opp_for_merchant=dict(opp,detail_json=review['detail_json'] if review else '{}')
        product_identity=merchant_identity(opp_for_merchant)
        merchant_checks={(r['opportunity_id'],r['product_url']):dict(r) for r in db.execute(
            'SELECT * FROM merchant_page_checks WHERE opportunity_id=?',(opportunity_id,)).fetchall()}
        merchant_page_check=summarize_merchant_page(opp_for_merchant,merchant_checks)
        public_price_history = assess_public_price_history(db, merchant_page_check.get('product_url'),
                                                           merchant_page_check)
        readiness=assess_readiness(opp,comparison,brief)
        benefit_relations=related_benefits(db,opportunity_id)
        product_benefit_candidates=match_product_benefits(db,opp_for_merchant)
        confirmed_benefits=[item for item in benefit_relations if item['relation_state']=='confirmed']
        source_linked_benefits=[item for item in benefit_relations if item['relation_state']=='source_linked']
    estimate, basis = estimated_profit(opp, quotes)
    profit_mode = request.args.get('profit_mode','sold')
    public_estimate = public_profit_estimate(opp, quotes, brief, review, profit_mode)
    buy_difference, buy_basis, below_observed = historical_buy_assessment(opp, quotes)
    resale_allowed, resale_basis = resale_assessment(opp, quotes)
    return render_template("opportunity.html", opp=opp, quotes=quotes, buy_checks=buy_checks, trades=trades, review=review,
                           public_offer=public_offer(opp['url'],opp['snippet'],opp['title']),comparison=comparison,
                           readiness=readiness,confirmed_benefits=confirmed_benefits,source_linked_benefits=source_linked_benefits,
                           product_benefit_candidates=product_benefit_candidates,
                           product_benefit_searchable=product_identity['key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')) and not product_identity['conflict'],
                           merchant_page_check=merchant_page_check,
                           public_price_history=public_price_history,
                           estimate=estimate, basis=basis, public_estimate=public_estimate,
                           profit_mode=public_estimate['mode'], buy_difference=buy_difference,
                           buy_basis=buy_basis, below_observed=below_observed,
                           resale_allowed=resale_allowed, resale_basis=resale_basis)


@app.post("/opportunities/<int:opportunity_id>/details")
def save_details(opportunity_id):
    status = request.form.get("status", "pending")
    if status not in ("pending", "verified", "ignored", "expired"):
        abort(400)
    try:
        offer_type = request.form.get("offer_type", "unknown")
        eligibility = request.form.get("eligibility", "unknown")
        if offer_type not in ("unknown", "suspected_new_user", "new_user", "standard", "other_restricted") or eligibility not in ("unknown", "eligible", "ineligible"):
            raise ValueError("优惠类型或本人资格无效")
        limit_text = request.form.get("purchase_limit", "").strip()
        purchase_limit = int(limit_text) if limit_text else None
        if purchase_limit is not None and not 1 <= purchase_limit <= 100000:
            raise ValueError("限购数量须为正整数")
        values = [cents(request.form.get(field)) for field in COST_FIELDS]
        specification = request.form.get("specification", "").strip()[:300]
        buy_proof = request.form.get("buy_proof", "").strip()[:500]
        if status == "verified" and (not specification or any(v is None for v in values)
                                     or not buy_proof or request.form.get("buy_confirmed") != "1"):
            raise ValueError("核实买入条件须填写完整规格、到手价及全部成本、核实依据，并确认当前账号可买和有货")
        if status == "verified" and (offer_type in ("unknown", "suspected_new_user") or eligibility != "eligible"):
            raise ValueError("先确认优惠类型及本人账号确实有购买资格；疑似新人价不能直接标为已核实")
        if status == "verified" and offer_type == "new_user" and (purchase_limit is None or request.form.get("new_user_checked") != "1"):
            raise ValueError("新人价须核对本人账号的首单资格、限购数量和当前结算价")
        checked = datetime.now(timezone.utc).isoformat() if status == "verified" else None
        with connect() as db:
            result = db.execute("""UPDATE opportunities SET status=?,notes=?,specification=?,buy_checked_at=?,buy_proof=?,
                offer_type=?,eligibility=?,purchase_limit=?,
                buy_cents=?,buy_shipping_cents=?,sell_shipping_cents=?,platform_fee_cents=?,
                processing_cents=?,other_cents=?,reserve_cents=? WHERE id=?""",
                (status, request.form.get("notes", "")[:2000],
                 specification, checked, buy_proof, offer_type, eligibility, purchase_limit, *values, opportunity_id))
            if not result.rowcount:
                abort(404)
            if status == "verified":
                db.execute("""INSERT INTO buy_checks
                    (opportunity_id,specification,buy_cents,shipping_cents,proof,checked_at)
                    VALUES(?,?,?,?,?,?)""",
                    (opportunity_id,specification,values[0],values[1],buy_proof,checked))
        flash("规格、状态和成本已保存", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("opportunity", opportunity_id=opportunity_id)


@app.post("/opportunities/<int:opportunity_id>/quotes")
def add_quote(opportunity_id):
    kind = request.form.get("kind", "")
    if kind not in ("listing", "sold", "recycler", "estimate", "historical_buy"):
        abort(400)
    try:
        offer_type = request.form.get("offer_type", "unknown")
        if offer_type not in ("unknown", "new_user", "standard", "other_restricted"):
            raise ValueError("历史买价的优惠资格无效")
        amount = cents(request.form.get("amount"), required=True)
        evidence = request.form.get("evidence_url", "").strip()
        specification = request.form.get("specification", "").strip()[:300]
        conditions = request.form.get("conditions", "").strip()[:1000]
        price_at = request.form.get("price_at", "").strip()
        valid_until = request.form.get("valid_until", "").strip()
        same_spec = bool(request.form.get("same_spec"))
        final_quote = kind == "recycler" and request.form.get("final_quote") == "1"
        if kind in ("sold", "recycler", "historical_buy"):
            if not same_spec or not evidence or not conditions or not price_at:
                raise ValueError("历史实付、成交与回收依据须有同规格确认、原始链接、适用条件和实际价格日期")
            observed_date = date.fromisoformat(price_at)
            if observed_date > date.today():
                raise ValueError("价格日期不能晚于今天")
            if kind == "recycler":
                if not final_quote:
                    raise ValueError("回收预估价不能当最终报价；须确认回收方已验机或书面承诺最终价")
                if not valid_until or date.fromisoformat(valid_until) < observed_date:
                    raise ValueError("回收报价须填写不早于报价日的有效期")
            if kind == "historical_buy" and valid_until:
                raise ValueError("历史买入实付价不使用回收报价有效期")
            if kind == "historical_buy" and offer_type == "unknown":
                raise ValueError("历史买价须标明普通价、新人价或其他资格价")
        elif price_at:
            date.fromisoformat(price_at)
        if same_spec:
            with connect() as db:
                opp = db.execute("SELECT specification FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
            if not opp or not opp["specification"] or not specification or " ".join(opp["specification"].split()).lower() != " ".join(specification.split()).lower():
                raise ValueError("要标记同款，机会与报价须填写完全一致的规格和成色")
        if evidence:
            _public_url(evidence)
        with connect() as db:
            db.execute("""INSERT INTO quotes
                (opportunity_id,kind,offer_type,amount_cents,specification,conditions,same_spec,final_quote,evidence_url,price_at,valid_until)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (opportunity_id,kind,offer_type,amount,specification,
                 conditions,int(same_spec),int(final_quote),evidence,price_at or None,valid_until or None))
        flash("行情依据已保存", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("opportunity", opportunity_id=opportunity_id)


@app.post("/opportunities/<int:opportunity_id>/trades")
def add_trade(opportunity_id):
    state = request.form.get("state", "")
    if state not in ("holding", "sold", "refunded"):
        abort(400)
    try:
        quantity = int(request.form.get("quantity", "1"))
        if not 1 <= quantity <= 100000:
            raise ValueError("数量无效")
        buy = cents(request.form.get("buy"), required=True)
        buy_fees = cents(request.form.get("buy_fees"), required=True)
        sale = cents(request.form.get("sale"), required=state == "sold")
        sale_fees = cents(request.form.get("sale_fees"), required=state == "sold")
        refund = cents(request.form.get("refund"), required=state == "refunded") or 0
        deposit = cents(request.form.get("deposit")) or 0
        deposit_lost = cents(request.form.get("deposit_lost"), required=state != "holding" and deposit > 0) or 0
        if deposit_lost > deposit:
            raise ValueError("未退还保证金不能大于缴纳保证金")
        if state == "holding" and (sale is not None or refund):
            raise ValueError("持有库存不能同时记录出售或退款")
        with connect() as db:
            db.execute("""INSERT INTO trades
                (opportunity_id,state,quantity,buy_cents,buy_fees_cents,sale_cents,
                 sale_fees_cents,refund_cents,deposit_cents,deposit_lost_cents,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (opportunity_id,state,quantity,buy,buy_fees,sale,sale_fees,refund,deposit,deposit_lost,
                 request.form.get("notes", "")[:1000]))
        flash("交易记录已保存；保证金只计资金占用", "ok")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return go("opportunity", opportunity_id=opportunity_id)


@app.get("/market")
def market():
    with connect() as db:
        rows = db.execute("""SELECT q.*,o.title AS opportunity_title FROM quotes q
                             JOIN opportunities o ON o.id=q.opportunity_id ORDER BY q.id DESC LIMIT 150""").fetchall()
    return render_template("market.html", rows=rows)


@app.get("/trades")
def trades():
    with connect() as db:
        rows = db.execute("""SELECT t.*,o.title AS opportunity_title FROM trades t
                             JOIN opportunities o ON o.id=t.opportunity_id ORDER BY t.id DESC""").fetchall()
    realized = sum((r["sale_cents"] or 0) + r["refund_cents"] - r["buy_cents"] -
                   r["buy_fees_cents"] - (r["sale_fees_cents"] or 0) - r["deposit_lost_cents"]
                   for r in rows if r["state"] in ("sold", "refunded"))
    tied = sum(r["buy_cents"] + r["buy_fees_cents"] + r["deposit_cents"]
               for r in rows if r["state"] == "holding")
    return render_template("trades.html", rows=rows, realized=realized, tied=tied)


@app.post("/trades/<int:trade_id>/settle")
def settle_trade(trade_id):
    state = request.form.get("state", "")
    if state not in ("sold", "refunded"):
        abort(400)
    try:
        sale = cents(request.form.get("sale"), required=state == "sold")
        sale_fees = cents(request.form.get("sale_fees"), required=state == "sold")
        refund = cents(request.form.get("refund"), required=state == "refunded") or 0
        deposit_lost = cents(request.form.get("deposit_lost"), required=True)
        with connect() as db:
            row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if not row or row["state"] != "holding":
                raise ValueError("只有持有中的记录可以结算")
            if deposit_lost > row["deposit_cents"]:
                raise ValueError("未退还保证金不能大于缴纳保证金")
            db.execute("""UPDATE trades SET state=?,sale_cents=?,sale_fees_cents=?,
                refund_cents=?,deposit_lost_cents=? WHERE id=?""",
                (state,sale,sale_fees,refund,deposit_lost,trade_id))
        flash("库存记录已结算", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("trades")


@app.get("/strategies")
def strategies():
    show_archive = request.args.get("view") == "all"
    with connect() as db:
        rows = db.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
        notices = notice_rows(db, limit=500)
        if not show_archive:
            notices = [row for row in notices if is_current_notice(row)]
        notices = notices[:100]
        deliveries = db.execute("""SELECT d.*,o.title FROM notification_deliveries d
                                   JOIN notifications n ON n.id=d.notification_id
                                   JOIN opportunities o ON o.id=n.opportunity_id
                                   ORDER BY d.id DESC LIMIT 50""").fetchall()
    return render_template("strategies.html", rows=rows, notices=notices,
                           deliveries=deliveries, channels=active_channels(), show_archive=show_archive)


@app.get("/messages")
def messages():
    values = notification_settings()
    chosen = {item.strip() for item in values["NOTIFY_CHANNELS"].split(",")}
    channels = [
        {"key": key, "label": label, "selected": key in chosen,
         "configured": channel_ready(key, values)}
        for key, label in CHANNEL_LABELS.items()
    ]
    with connect() as db:
        deliveries = db.execute("""SELECT channel,status,COUNT(*) AS total
                                   FROM notification_deliveries
                                   GROUP BY channel,status ORDER BY channel,status""").fetchall()
    return render_template("messages.html", enabled=values["NOTIFY_ENABLED"] == "1",
                           channels=channels, deliveries=deliveries,
                           qq_base=values["QQ_ONEBOT_BASE"],
                           qq_group_set=bool(values["QQ_GROUP_ID"]),
                           qq_user_set=bool(values["QQ_USER_ID"]))


@app.post("/messages")
def save_messages():
    values = notification_settings()
    chosen = set(request.form.getlist("channels"))
    if not chosen.issubset(CHANNEL_LABELS):
        abort(400)
    try:
        values["NOTIFY_ENABLED"] = "1" if request.form.get("enabled") == "1" else "0"
        values["NOTIFY_CHANNELS"] = ",".join(key for key in CHANNEL_LABELS if key in chosen)
        for key in ("WECOM_WEBHOOK_URL", "SERVERCHAN_SENDKEY", "QQ_ONEBOT_TOKEN",
                    "QQ_GROUP_ID", "QQ_USER_ID"):
            submitted = request.form.get(key, "").strip()
            if submitted and (len(submitted) > 2000 or any(ord(c) < 32 for c in submitted)):
                raise ValueError("凭据或接收目标格式不正确")
            if request.form.get("clear_" + key) == "1":
                values[key] = ""
            elif submitted:
                values[key] = submitted
        qq_base = request.form.get("QQ_ONEBOT_BASE", "").strip()
        if qq_base:
            values["QQ_ONEBOT_BASE"] = qq_base.rstrip("/")
        if len(values["QQ_ONEBOT_BASE"]) > 100 or any(ord(c) < 32 for c in values["QQ_ONEBOT_BASE"]):
            raise ValueError("QQ 本机网关地址格式不正确")
        if values["QQ_GROUP_ID"] and (not values["QQ_GROUP_ID"].isdigit() or len(values["QQ_GROUP_ID"]) > 20):
            raise ValueError("QQ 群 ID 须为数字")
        if values["QQ_USER_ID"] and (not values["QQ_USER_ID"].isdigit() or len(values["QQ_USER_ID"]) > 20):
            raise ValueError("QQ 用户 ID 须为数字")
        if values["NOTIFY_ENABLED"] == "1":
            if not chosen:
                raise ValueError("启用消息前至少选择一个通道")
            missing = [CHANNEL_LABELS[key] for key in chosen if not channel_ready(key, values)]
            if missing:
                raise ValueError("以下通道配置尚不完整：" + "、".join(missing))
        save_notification_settings(values)
        flash("消息配置已保存；未发送消息。后台下个周期会读取新配置。", "ok")
    except (ValueError, OSError) as exc:
        flash("消息配置未保存：" + (str(exc) if isinstance(exc, ValueError) else "本机配置文件写入失败"), "error")
    return go("messages")


@app.post("/messages/test/<channel>")
def test_message_channel(channel):
    if channel not in CHANNEL_LABELS:
        abort(404)
    if not channel_ready(channel, notification_settings()):
        flash("该通道尚未完成配置，未发送测试消息", "error")
        return go("messages")
    try:
        deliver(channel, "机会工作台接入测试",
                "这是你在本机消息接入页面主动触发的测试消息。请到接收端确认实际到达。",
                manual_test=True)
        flash("通道接口已接受测试请求；请到接收端确认是否收到。", "ok")
    except Exception:
        flash("测试消息发送失败；请检查通道配置和接收端日志。", "error")
    return go("messages")


@app.post("/strategies")
@app.post("/strategies/<int:strategy_id>")
def add_strategy(strategy_id=None):
    name = request.form.get("name", "").strip()
    if not name:
        flash("策略名称不能为空", "error")
        return go("strategies")
    try:
        max_buy = cents(request.form.get("max_buy"))
        min_profit = cents(request.form.get("min_profit"))
        if (max_buy is None) != (min_profit is None):
            raise ValueError("转售策略须同时填写买入含运费预算和最低净利；都留空时仅筛选普通线索")
        if max_buy is not None and (max_buy <= 0 or min_profit <= 0):
            raise ValueError("转售预算和最低净利必须大于 0")
        enabled = 0 if request.form.get("enabled") == "0" else 1
        values = (name,request.form.get("include_words", "")[:300],
                  request.form.get("exclude_words", "")[:300],request.form.get("category", ""),
                  max_buy,min_profit,enabled)
        with connect() as db:
            if strategy_id is None:
                db.execute("""INSERT INTO strategies
                    (name,include_words,exclude_words,category,max_buy_cents,min_profit_cents,enabled)
                    VALUES(?,?,?,?,?,?,?)""", values)
            else:
                updated = db.execute("""UPDATE strategies SET name=?,include_words=?,exclude_words=?,category=?,
                    max_buy_cents=?,min_profit_cents=?,enabled=? WHERE id=?""", (*values,strategy_id))
                if not updated.rowcount:
                    abort(404)
        flash("策略已保存；完整金额门槛用于转售测算候选，发送前也会重新检查。未填金额的策略只筛选普通线索。", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("strategies")


@app.post("/notifications/<int:notification_id>/read")
def read_notice(notification_id):
    with connect() as db:
        db.execute("UPDATE notifications SET status='read',sent_at=CURRENT_TIMESTAMP WHERE id=?", (notification_id,))
    return go("strategies")


@app.post("/deliveries/<int:delivery_id>/retry")
def retry_delivery(delivery_id):
    with connect() as db:
        db.execute("""UPDATE notification_deliveries SET status='pending',last_error=NULL
                      WHERE id=? AND status='failed'""", (delivery_id,))
    flash("已重新排队；后台会再次核对价格证据后发送", "ok")
    return go("strategies")


def main():
    initialize()
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "benefits-import":
        if len(sys.argv)!=3:raise SystemExit("Usage: python app.py benefits-import <normalized-records.jsonl>")
        imported=0
        with open(sys.argv[2],encoding='utf-8') as records:
            for line_number,line in enumerate(records,1):
                if not line.strip():continue
                try:record=json.loads(line);import_authorized_record(record)
                except (json.JSONDecodeError,ValueError) as exc:
                    raise SystemExit(f"优惠记录第 {line_number} 行未导入：{exc}") from exc
                imported+=1
        print({"imported":imported})
        return
    review_all()
    if command == "serve":
        app.run(host="127.0.0.1", port=5002, debug=False)
    elif command == "scan":
        print(scan_all())
        print(run_cycle())
    elif command == "review":
        print(run_cycle())
    elif command == "channels-refresh":
        print(refresh_channel_candidates())
    elif command == "channels-validate":
        print(validate_channel_candidates())
    elif command == "worker":
        while True:
            result = scan_all(due_only=True)
            if result:
                print(result, flush=True)
            print(run_cycle(), flush=True)
            print({'public_links':resolve_links()},flush=True)
            print({'merchant_public_pages':verify_merchant_pages()},flush=True)
            print({'benefit_pages':refresh_benefits()},flush=True)
            deliveries = dispatch_verified_alerts()
            if deliveries["sent"] or deliveries["failed"]:
                print(deliveries, flush=True)
            time.sleep(60)
    else:
        raise SystemExit("Usage: python app.py [serve|scan|review|worker|channels-refresh|channels-validate|benefits-import <normalized-records.jsonl>]")


if __name__ == "__main__":
    main()
