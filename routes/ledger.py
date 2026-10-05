"""Market comparison and purchase/sale ledger routes."""
from flask import abort, flash, render_template, request
from db import connect
from services.money import cents
from routes.common import go

def market():
    with connect() as db:
        rows = db.execute("""SELECT q.*,o.title AS opportunity_title FROM quotes q
                             JOIN opportunities o ON o.id=q.opportunity_id ORDER BY q.id DESC LIMIT 150""").fetchall()
    return render_template("market.html", rows=rows)

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

def register(app):
    app.add_url_rule('/market', endpoint='market', view_func=market, methods=['GET'])
    app.add_url_rule('/trades', endpoint='trades', view_func=trades, methods=['GET'])
    app.add_url_rule('/trades/<int:trade_id>/settle', endpoint='settle_trade', view_func=settle_trade, methods=['POST'])
