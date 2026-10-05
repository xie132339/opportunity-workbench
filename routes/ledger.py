"""Public market comparison routes."""
from flask import render_template
from db import connect

def market():
    with connect() as db:
        rows = db.execute("""SELECT q.*,o.title AS opportunity_title FROM quotes q
                             JOIN opportunities o ON o.id=q.opportunity_id
                             WHERE q.kind!='historical_buy' ORDER BY q.id DESC LIMIT 150""").fetchall()
    return render_template("market.html", rows=rows)

def register(app):
    app.add_url_rule('/market', endpoint='market', view_func=market, methods=['GET'])
