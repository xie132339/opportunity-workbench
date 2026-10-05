"""Freshness, eligibility, comparable-price and profit assessment rules."""
import json
from datetime import date, datetime, timedelta, timezone

from db import connect
from offer import TOPIC_LABELS, product_subcategory
from pricing import public_market_profit
from scanner import strategy_matches
from services.money import COST_FIELDS, money

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
    detail = {}
    if review and review['detail_json']:
        try:
            detail = json.loads(review['detail_json'])
        except (TypeError, ValueError):
            detail = {}
    total = brief.get('total_cents')
    spec = (brief.get('selected_spec') or detail.get('specification')
            or (review['specification'] if review else '') or opp['specification'])
    shipping = brief.get('audit', {}).get('plan', {}).get('shipping_cents')
    estimate = public_market_profit(opp, quotes, total, spec, mode, shipping)
    estimate['category_large'] = TOPIC_LABELS.get(opp['topic'], opp['category'])
    estimate['category_small'] = product_subcategory(brief.get('title') or opp['title'], opp['topic'])
    estimate['category_note'] = '大类/小类用于整理与找同款；利润公式统一，不用类目均值替代商品行情。'
    return estimate

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
                          "本规则只核算公开来源价差；回报率、周转、资金占用与退出承接量尚未完整验证，不代表可执行盈利。")
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
