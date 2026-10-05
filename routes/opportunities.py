"""Opportunity detail and manual public-evidence routes."""
import hashlib
from datetime import date
from flask import abort, flash, render_template, request
from db import connect
from comparison import load_comparisons, assess_readiness, merchant_identity
from benefits import (KINDS as BENEFIT_KINDS, add as add_benefit, product_match_candidates as match_product_benefits, related as related_benefits)
from autoreview import offer_summary, public_offer
from offer import detected_offer_type
from scanner import _public_url
from link_resolution import enrich as enrich_links
from services.money import cents
from services.opportunity_analysis import public_profit_estimate
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
        quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=? AND kind!='historical_buy' ORDER BY observed_at DESC,id DESC",
                            (opportunity_id,)).fetchall()
        review = db.execute("SELECT * FROM auto_reviews WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        comparison = load_comparisons(db).get(opportunity_id,{})
        identity_row=dict(opp,detail_json=review['detail_json'] if review else '{}')
        resolved= enrich_links(identity_row,{r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')})
        opp=dict(opp,metadata_json=resolved['metadata_json'])
        brief=offer_summary(opp['title'],opp['url'],opp['snippet'],metadata=opp['metadata_json'],detail_json=review['detail_json'] if review else None,
                            sync_meta=dict(checked_at=review['checked_at'],detail_checked_at=review['detail_checked_at'],detail_error=review['detail_error']) if review else {})
        opp_for_merchant=dict(opp,detail_json=review['detail_json'] if review else '{}')
        product_identity=merchant_identity(opp_for_merchant)
        readiness=assess_readiness(opp,comparison,brief)
        benefit_relations=related_benefits(db,opportunity_id)
        product_benefit_candidates=match_product_benefits(db,opp_for_merchant)
        confirmed_benefits=[item for item in benefit_relations if item['relation_state']=='confirmed']
        source_linked_benefits=[item for item in benefit_relations if item['relation_state']=='source_linked']
    profit_mode = request.args.get('profit_mode','sold')
    public_estimate = public_profit_estimate(opp, quotes, brief, review, profit_mode)
    return render_template("opportunity.html", opp=opp, quotes=quotes, review=review,
                           public_offer=public_offer(opp['url'],opp['snippet'],opp['title']),comparison=comparison,
                           readiness=readiness,confirmed_benefits=confirmed_benefits,source_linked_benefits=source_linked_benefits,
                           product_benefit_candidates=product_benefit_candidates,
                           product_benefit_searchable=product_identity['key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')) and not product_identity['conflict'],
                           public_estimate=public_estimate,
                           profit_mode=public_estimate['mode'])

def add_quote(opportunity_id):
    kind = request.form.get("kind", "")
    if kind not in ("listing", "sold", "recycler", "estimate"):
        abort(400)
    try:
        amount = cents(request.form.get("amount"), required=True)
        evidence = request.form.get("evidence_url", "").strip()
        specification = request.form.get("specification", "").strip()[:300]
        conditions = request.form.get("conditions", "").strip()[:1000]
        price_at = request.form.get("price_at", "").strip()
        valid_until = request.form.get("valid_until", "").strip()
        same_spec = bool(request.form.get("same_spec"))
        final_quote = kind == "recycler" and request.form.get("final_quote") == "1"
        if kind in ("sold", "recycler"):
            if not same_spec or not evidence or not conditions or not price_at:
                raise ValueError("成交与回收依据须有同规格确认、原始链接、适用条件和实际价格日期")
            observed_date = date.fromisoformat(price_at)
            if observed_date > date.today():
                raise ValueError("价格日期不能晚于今天")
            if kind == "recycler":
                if not final_quote:
                    raise ValueError("回收预估价不能当最终报价；须确认回收方已验机或书面承诺最终价")
                if not valid_until or date.fromisoformat(valid_until) < observed_date:
                    raise ValueError("回收报价须填写不早于报价日的有效期")
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
                (opportunity_id,kind,"unknown",amount,specification,
                 conditions,int(same_spec),int(final_quote),evidence,price_at or None,valid_until or None))
        flash("行情依据已保存", "ok")
    except ValueError as exc:
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
    app.add_url_rule('/opportunities/<int:opportunity_id>/quotes', endpoint='add_quote', view_func=add_quote, methods=['POST'])
    app.add_url_rule('/manual', endpoint='manual', view_func=manual, methods=['POST'])
