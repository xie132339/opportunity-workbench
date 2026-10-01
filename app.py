"""Local, source-first Chinese opportunity workbench."""
import hashlib
import os
from pathlib import Path
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
import requests

from db import connect, initialize
from scanner import _public_url, scan_all, scan_source

ROOT = Path(__file__).resolve().parent


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()
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
    return {"csrf": token, "money": money, "profit": estimated_profit}


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


def estimated_profit(opp, quotes):
    if opp["category"] == "服务与合作":
        return None, "服务商机需先确认资格与合同条件"
    missing = [field for field in COST_FIELDS if opp[field] is None]
    if missing:
        return None, "缺少成本：" + "、".join(missing)
    acceptable = [q for q in quotes if q["kind"] in ("sold", "recycler") and q["same_spec"]]
    if not acceptable:
        return None, "缺少已确认同规格的成交或回收报价"
    quote = acceptable[0]
    result = quote["amount_cents"] - sum(opp[field] for field in COST_FIELDS)
    return result, "依据：" + ("成交" if quote["kind"] == "sold" else "回收") + "报价 #" + str(quote["id"])


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
    budget_text = request.args.get("budget", "").strip()
    try:
        budget = cents(budget_text) if budget_text else None
    except ValueError:
        budget = None
    sql = """SELECT o.*,s.platform,s.status AS source_status,e.is_baseline
             FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id
             LEFT JOIN events e ON e.id=o.event_id WHERE 1=1"""
    args = []
    if query:
        sql += " AND o.title LIKE ?"
        args.append("%" + query + "%")
    if category:
        sql += " AND o.category=?"
        args.append(category)
    if platform:
        sql += " AND s.platform=?"
        args.append(platform)
    if budget is not None:
        sql += " AND o.buy_cents IS NOT NULL AND o.buy_cents<=?"
        args.append(budget)
    if status:
        sql += " AND o.status=?"
        args.append(status)
    else:
        sql += " AND o.status!='ignored'"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
        rows = db.execute(sql + """ ORDER BY CASE o.category
            WHEN '零售优惠' THEN 1 WHEN '二手与闲置' THEN 2
            WHEN '供货与清仓' THEN 3 WHEN '新品与补货' THEN 4
            WHEN '拍卖与资产' THEN 5 WHEN '服务与合作' THEN 6 ELSE 7 END,
            o.created_at DESC,o.id DESC LIMIT 36 OFFSET ?""",
                          [*args, (page - 1) * 36]).fetchall()
        sources = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
        alerts = db.execute("SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0]
    return render_template("index.html", rows=rows, sources=sources,
                           alerts=alerts, query=query, category=category, status=status,
                           platform=platform, budget_text=budget_text,
                           total=total, page=page, has_next=page * 36 < total)


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
                (event_id,source_id,title,category,url) VALUES(?,?,?,?,?)""",
                (event.lastrowid,source_id,title,category,url))
    flash("人工线索已保存；价格、规格和利润仍需核实", "ok")
    return go("index")


@app.get("/sources")
def sources():
    with connect() as db:
        rows = db.execute("""SELECT s.*,(SELECT COUNT(*) FROM events e WHERE e.source_id=s.id) event_count
                             FROM sources s ORDER BY s.enabled DESC,s.id""").fetchall()
    return render_template("sources.html", rows=rows)


@app.post("/sources")
def source_add():
    platform = request.form.get("platform", "").strip()
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    url = request.form.get("url", "").strip()
    method = request.form.get("method", "manual")
    if not all((platform, name, category)) or method not in ("manual", "monitor", "rss"):
        flash("来源信息不完整", "error")
        return go("sources")
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        if not 5 <= interval <= 1440:
            raise ValueError("检查间隔须在 5 到 1440 分钟之间")
        _public_url(url)
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
                (platform,name,category,url,method,watch_uuid,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (platform,name,category,url,method,watch_uuid,"pending" if method != "manual" else "manual",
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


@app.post("/sources/<int:source_id>/interval")
def source_interval(source_id):
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        if not 5 <= interval <= 1440:
            raise ValueError("检查间隔须在 5 到 1440 分钟之间")
        with connect() as db:
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
        opp = db.execute("""SELECT o.*,s.platform,s.name AS source_name,e.snippet,e.is_baseline,e.observed_at
                             FROM opportunities o LEFT JOIN sources s ON s.id=o.source_id
                             LEFT JOIN events e ON e.id=o.event_id WHERE o.id=?""", (opportunity_id,)).fetchone()
        if not opp:
            abort(404)
        quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=? ORDER BY observed_at DESC,id DESC",
                            (opportunity_id,)).fetchall()
        trades = db.execute("SELECT * FROM trades WHERE opportunity_id=? ORDER BY id DESC", (opportunity_id,)).fetchall()
    estimate, basis = estimated_profit(opp, quotes)
    return render_template("opportunity.html", opp=opp, quotes=quotes, trades=trades,
                           estimate=estimate, basis=basis)


@app.post("/opportunities/<int:opportunity_id>/details")
def save_details(opportunity_id):
    status = request.form.get("status", "pending")
    if status not in ("pending", "verified", "ignored", "expired"):
        abort(400)
    try:
        values = [cents(request.form.get(field)) for field in COST_FIELDS]
        with connect() as db:
            db.execute("""UPDATE opportunities SET status=?,notes=?,specification=?,
                buy_cents=?,buy_shipping_cents=?,sell_shipping_cents=?,platform_fee_cents=?,
                processing_cents=?,other_cents=?,reserve_cents=? WHERE id=?""",
                (status, request.form.get("notes", "")[:2000],
                 request.form.get("specification", "")[:300], *values, opportunity_id))
        flash("规格、状态和成本已保存", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("opportunity", opportunity_id=opportunity_id)


@app.post("/opportunities/<int:opportunity_id>/quotes")
def add_quote(opportunity_id):
    kind = request.form.get("kind", "")
    if kind not in ("listing", "sold", "recycler", "estimate"):
        abort(400)
    try:
        amount = cents(request.form.get("amount"), required=True)
        evidence = request.form.get("evidence_url", "").strip()
        specification = request.form.get("specification", "").strip()[:300]
        same_spec = bool(request.form.get("same_spec"))
        if same_spec:
            with connect() as db:
                opp = db.execute("SELECT specification FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
            if not opp or not opp["specification"] or not specification or " ".join(opp["specification"].split()).lower() != " ".join(specification.split()).lower():
                raise ValueError("要标记同款，机会与报价须填写完全一致的规格和成色")
        if evidence:
            _public_url(evidence)
        with connect() as db:
            db.execute("""INSERT INTO quotes
                (opportunity_id,kind,amount_cents,specification,conditions,same_spec,evidence_url)
                VALUES(?,?,?,?,?,?,?)""",
                (opportunity_id,kind,amount,specification,
                 request.form.get("conditions", "")[:1000],int(same_spec),evidence))
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
    with connect() as db:
        rows = db.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
        notices = db.execute("""SELECT n.*,o.title FROM notifications n
                                JOIN opportunities o ON o.id=n.opportunity_id
                                ORDER BY n.id DESC LIMIT 100""").fetchall()
    return render_template("strategies.html", rows=rows, notices=notices)


@app.post("/strategies")
def add_strategy():
    name = request.form.get("name", "").strip()
    if not name:
        flash("策略名称不能为空", "error")
        return go("strategies")
    try:
        with connect() as db:
            db.execute("""INSERT INTO strategies
                (name,include_words,exclude_words,category,max_buy_cents,min_profit_cents)
                VALUES(?,?,?,?,?,?)""",
                (name,request.form.get("include_words", "")[:300],
                 request.form.get("exclude_words", "")[:300],request.form.get("category", ""),
                 cents(request.form.get("max_buy")),cents(request.form.get("min_profit"))))
        flash("策略已保存；目前关键词与类别用于站内新线索提醒", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return go("strategies")


@app.post("/notifications/<int:notification_id>/read")
def read_notice(notification_id):
    with connect() as db:
        db.execute("UPDATE notifications SET status='read',sent_at=CURRENT_TIMESTAMP WHERE id=?", (notification_id,))
    return go("strategies")


def main():
    initialize()
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve":
        app.run(host="127.0.0.1", port=5002, debug=False)
    elif command == "scan":
        print(scan_all())
    elif command == "worker":
        while True:
            result = scan_all(due_only=True)
            if result:
                print(result, flush=True)
            time.sleep(60)
    else:
        raise SystemExit("Usage: python app.py [serve|scan|worker]")


if __name__ == "__main__":
    main()
