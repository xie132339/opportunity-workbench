"""Opportunity detail and manual evidence/ledger-entry routes."""
import hashlib
from datetime import date, datetime, timezone
from flask import abort, flash, render_template, request
from db import connect
from comparison import load_comparisons, assess_readiness, merchant_identity
from benefits import (KINDS as BENEFIT_KINDS, add as add_benefit, product_match_candidates as match_product_benefits, related as related_benefits)
from autoreview import offer_summary, public_offer
from offer import detected_offer_type
from price_history import for_product as assess_public_price_history
from scanner import _public_url
from link_resolution import enrich as enrich_links
from merchant_verification import summarize as summarize_merchant_page
from services.money import COST_FIELDS, cents
from services.opportunity_analysis import (estimated_profit, historical_buy_assessment, public_profit_estimate, resale_assessment)
from routes.common import go

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

def register(app):
    app.add_url_rule('/opportunities/<int:opportunity_id>', endpoint='opportunity', view_func=opportunity, methods=['GET'])
    app.add_url_rule('/opportunities/<int:opportunity_id>/details', endpoint='save_details', view_func=save_details, methods=['POST'])
    app.add_url_rule('/opportunities/<int:opportunity_id>/quotes', endpoint='add_quote', view_func=add_quote, methods=['POST'])
    app.add_url_rule('/opportunities/<int:opportunity_id>/trades', endpoint='add_trade', view_func=add_trade, methods=['POST'])
    app.add_url_rule('/manual', endpoint='manual', view_func=manual, methods=['POST'])
