"""Discount discovery and automated verification routes."""
from flask import render_template, request
from db import connect
from benefits import KINDS as BENEFIT_KINDS, listing as list_benefits, stats as benefit_stats
from acceptance import QUERY, evaluate, freeze

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

def register(app):
    app.add_url_rule('/benefits', endpoint='benefits_page', view_func=benefits_page, methods=['GET'])
    app.add_url_rule('/verification', endpoint='verification', view_func=verification, methods=['GET'])
