"""Local, source-first Chinese opportunity workbench."""
import hashlib
import os
from pathlib import Path
import secrets
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from urllib.parse import urlparse

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
import requests

from db import connect, initialize
from scanner import _local_adapter_url, _public_url, scan_all, scan_source
from notifier import active_channels, deliver

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
    if opp["status"] != "verified" or not opp["specification"]:
        return None, "买入条件与完整规格尚未核实"
    try:
        checked = datetime.fromisoformat(opp["buy_checked_at"] or "")
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except ValueError:
        return None, "缺少实际到手价的核实时间"
    if datetime.now(timezone.utc) - checked > timedelta(hours=24):
        return None, "买入价格已超过 24 小时，请重新核对库存、资格与结算价"
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


def historical_buy_assessment(opp, quotes):
    """Compare cash paid including shipping, never advertised or post-rebate prices."""
    if opp["status"] != "verified" or not opp["specification"] or opp["buy_cents"] is None or opp["buy_shipping_cents"] is None:
        return None, "先核实当前同规格现金实付总额（含运费）", False
    try:
        checked = datetime.fromisoformat(opp["buy_checked_at"] or "")
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - checked > timedelta(hours=24):
            return None, "当前实付价已超过 24 小时，请重新核对", False
    except ValueError:
        return None, "缺少当前实付价核实时间", False
    spec = " ".join(opp["specification"].split()).lower()
    samples = {}
    today = date.today()
    for q in quotes:
        if q["kind"] != "historical_buy" or not q["same_spec"] or not q["evidence_url"] or not q["conditions"]:
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
        return None, f"近 180 天只有 {len(samples)} 个不同日期或来源的同规格现金实付样本；至少需要 3 个", False
    current = opp["buy_cents"] + opp["buy_shipping_cents"]
    middle = int(median(samples.values()))
    lowest = min(samples.values())
    difference = lowest - current
    return difference, f"当前含运费 {money(current)}；近 180 天 {len(samples)} 个历史样本中位数 {money(middle)}、最低 {money(lowest)}。历史条件仍需人工逐条核对。", current < lowest


def is_evidence_candidate(opp, quotes):
    profit, _ = estimated_profit(opp, quotes)
    _, _, historical_low = historical_buy_assessment(opp, quotes)
    return profit is not None and profit > 0 and historical_low


def dispatch_verified_alerts():
    """Fan out only evidence-backed candidates; failed sends require manual retry."""
    channels = active_channels()
    if not channels:
        return {"eligible": 0, "sent": 0, "failed": 0}
    eligible = 0
    with connect() as db:
        rows = db.execute("""SELECT o.*,e.id AS source_event_id FROM opportunities o
                             JOIN events e ON e.id=o.event_id WHERE o.status='verified'""").fetchall()
        for opp in rows:
            quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (opp["id"],)).fetchall()
            if not is_evidence_candidate(opp, quotes):
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
            opp = db.execute("SELECT * FROM opportunities WHERE id=?", (task["opportunity_id"],)).fetchone()
            quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (task["opportunity_id"],)).fetchall()
        profit, _ = estimated_profit(opp, quotes)
        _, buy_basis, historical_low = historical_buy_assessment(opp, quotes)
        if profit is None or profit <= 0 or not historical_low or task["channel"] not in channels:
            with connect() as db:
                db.execute("UPDATE notification_deliveries SET status='skipped',last_error='条件已变化或通道已关闭' WHERE id=?",
                           (task["id"],))
            continue
        title = "待人工复核的价差线索：" + task["title"][:80]
        body = f"{buy_basis}\n保守价差 {money(profit)}。仍须核实资格、库存、成交与到账。\n原始链接：{task['url'] or '无'}"
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
    budget_text = request.args.get("budget", "").strip()
    candidate_only = request.args.get("candidate") == "1"
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
        order_by = """ ORDER BY CASE o.category
            WHEN '零售优惠' THEN 1 WHEN '二手与闲置' THEN 2
            WHEN '供货与清仓' THEN 3 WHEN '新品与补货' THEN 4
            WHEN '拍卖与资产' THEN 5 WHEN '服务与合作' THEN 6 ELSE 7 END,
            o.created_at DESC,o.id DESC"""
        if candidate_only:
            candidates = []
            for row in db.execute(sql + " AND o.status='verified'" + order_by, args):
                quotes = db.execute("SELECT * FROM quotes WHERE opportunity_id=?", (row["id"],)).fetchall()
                if is_evidence_candidate(row, quotes):
                    candidates.append(row)
            total = len(candidates)
            rows = candidates[(page - 1) * 36:page * 36]
        else:
            total = db.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
            rows = db.execute(sql + order_by + " LIMIT 36 OFFSET ?",
                              [*args, (page - 1) * 36]).fetchall()
        sources = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
        alerts = db.execute("SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0]
    return render_template("index.html", rows=rows, sources=sources,
                           alerts=alerts, query=query, category=category, status=status,
                           platform=platform, budget_text=budget_text,
                           total=total, page=page, has_next=page * 36 < total,
                           candidate_only=candidate_only)


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
    parser = request.form.get("parser", "") if method == "html" else ""
    parser_hosts = {"smzdm": "www.smzdm.com", "apple": "www.apple.com.cn",
                    "mi": "www.mi.com", "ccgp": "www.ccgp.gov.cn",
                    "yiwugo": "www.yiwugo.com", "kongfz": "book.kongfz.com",
                    "suning": "www.suning.com", "lenovo": "www.lenovo.com.cn",
                    "honor": "www.honor.com"}
    if not all((platform, name, category)) or method not in ("manual", "monitor", "rss", "rsshub", "goofish", "html"):
        flash("来源信息不完整", "error")
        return go("sources")
    try:
        interval = int(request.form.get("interval_minutes", "60"))
        if not 5 <= interval <= 1440:
            raise ValueError("检查间隔须在 5 到 1440 分钟之间")
        if method == "rsshub":
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
    buy_difference, buy_basis, below_observed = historical_buy_assessment(opp, quotes)
    return render_template("opportunity.html", opp=opp, quotes=quotes, trades=trades,
                           estimate=estimate, basis=basis, buy_difference=buy_difference,
                           buy_basis=buy_basis, below_observed=below_observed)


@app.post("/opportunities/<int:opportunity_id>/details")
def save_details(opportunity_id):
    status = request.form.get("status", "pending")
    if status not in ("pending", "verified", "ignored", "expired"):
        abort(400)
    try:
        values = [cents(request.form.get(field)) for field in COST_FIELDS]
        specification = request.form.get("specification", "").strip()[:300]
        buy_proof = request.form.get("buy_proof", "").strip()[:500]
        if status == "verified" and (not specification or any(v is None for v in values)
                                     or not buy_proof or request.form.get("buy_confirmed") != "1"):
            raise ValueError("核实买入条件须填写完整规格、到手价及全部成本、核实依据，并确认当前账号可买和有货")
        checked = datetime.now(timezone.utc).isoformat() if status == "verified" else None
        with connect() as db:
            db.execute("""UPDATE opportunities SET status=?,notes=?,specification=?,buy_checked_at=?,buy_proof=?,
                buy_cents=?,buy_shipping_cents=?,sell_shipping_cents=?,platform_fee_cents=?,
                processing_cents=?,other_cents=?,reserve_cents=? WHERE id=?""",
                (status, request.form.get("notes", "")[:2000],
                 specification, checked, buy_proof, *values, opportunity_id))
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
                (opportunity_id,kind,amount_cents,specification,conditions,same_spec,final_quote,evidence_url,price_at,valid_until)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (opportunity_id,kind,amount,specification,
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
    with connect() as db:
        rows = db.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
        notices = db.execute("""SELECT n.*,o.title FROM notifications n
                                JOIN opportunities o ON o.id=n.opportunity_id
                                ORDER BY n.id DESC LIMIT 100""").fetchall()
        deliveries = db.execute("""SELECT d.*,o.title FROM notification_deliveries d
                                   JOIN notifications n ON n.id=d.notification_id
                                   JOIN opportunities o ON o.id=n.opportunity_id
                                   ORDER BY d.id DESC LIMIT 50""").fetchall()
    return render_template("strategies.html", rows=rows, notices=notices,
                           deliveries=deliveries, channels=active_channels())


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
    if command == "serve":
        app.run(host="127.0.0.1", port=5002, debug=False)
    elif command == "scan":
        print(scan_all())
    elif command == "worker":
        while True:
            result = scan_all(due_only=True)
            if result:
                print(result, flush=True)
            deliveries = dispatch_verified_alerts()
            if deliveries["sent"] or deliveries["failed"]:
                print(deliveries, flush=True)
            time.sleep(60)
    else:
        raise SystemExit("Usage: python app.py [serve|scan|worker]")


if __name__ == "__main__":
    main()
