"""Discount discovery and automated verification routes."""
from flask import render_template, request
from db import connect
from benefits import KINDS as BENEFIT_KINDS, listing as list_benefits, stats as benefit_stats
from acceptance import QUERY, evaluate, freeze
from autoreview import offer_summary
from services.source_freshness import CATALOG_LISTING_PARSERS
from services.product_search import listing_quote

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

def verification():
    state = request.args.get('state', '')
    with connect() as db:
        counts = dict(db.execute('SELECT state,COUNT(*) FROM auto_reviews GROUP BY state').fetchall())
        run = db.execute('SELECT * FROM review_runs WHERE id=1').fetchone()
        rows = db.execute('''SELECT a.*,o.title,o.url,e.snippet,e.metadata_json,s.parser AS source_parser
            FROM auto_reviews a
            JOIN opportunities o ON o.id=a.opportunity_id LEFT JOIN events e ON e.id=o.event_id
            LEFT JOIN sources s ON s.id=o.source_id
            WHERE (?='' OR a.state=?)
            ORDER BY o.id DESC LIMIT 100''', (state,state)).fetchall()
    display_rows=[]
    for row in rows:
        item=dict(row)
        current_summary=offer_summary(item['title'],item['url'],item.get('snippet') or '',
            conditions=item.get('conditions') or '',metadata=item.get('metadata_json') or '{}',
            detail_json=item.get('detail_json'),sync_meta=dict(checked_at=item.get('checked_at'),
                detail_checked_at=item.get('detail_checked_at'),detail_error=item.get('detail_error')))
        item['display_quote']=listing_quote(dict(item,
            catalog_listing=item.get('source_parser') in CATALOG_LISTING_PARSERS),current_summary)
        price_status=current_summary.get('price_status') or {}
        item['page_amount_cents']=price_status.get('page_amount_cents')
        item['page_amount_label']=price_status.get('page_amount_label')
        item['page_amount_evidence']=price_status.get('page_amount_evidence')
        selected=current_summary.get('selected_spec')
        source_spec=current_summary.get('source_spec')
        item['display_specification']=(f'原文明示报价规格：{selected}' if selected else
            f'标题规格线索：{source_spec}（未确认报价对应变体）' if source_spec else
            '未能从来源原文确定')
        display_rows.append(item)
    from acceptance import QUERY,evaluate,freeze
    with connect() as db:
        evidence=[dict(r) for r in db.execute(QUERY)]
        cache={r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')}
    cohort=freeze(evidence,current_only=True)
    acceptance=evaluate(evidence,cohort,cache)
    return render_template('verification.html',counts=counts,run=run,rows=display_rows,state=state,acceptance=acceptance)

def register(app):
    app.add_url_rule('/benefits', endpoint='benefits_page', view_func=benefits_page, methods=['GET'])
    app.add_url_rule('/verification', endpoint='verification', view_func=verification, methods=['GET'])
