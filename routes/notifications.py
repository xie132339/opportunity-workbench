"""Strategies, notification channel settings and delivery routes."""
from flask import abort, flash, render_template, request
from db import connect
from notifier import CHANNEL_LABELS, active_channels, channel_ready, deliver, notification_settings, save_notification_settings
from services.money import cents
from services.opportunity_analysis import is_current_notice, notice_rows
from routes.common import go

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

def read_notice(notification_id):
    with connect() as db:
        db.execute("UPDATE notifications SET status='read',sent_at=CURRENT_TIMESTAMP WHERE id=?", (notification_id,))
    return go("strategies")

def retry_delivery(delivery_id):
    with connect() as db:
        db.execute("""UPDATE notification_deliveries SET status='pending',last_error=NULL
                      WHERE id=? AND status='failed'""", (delivery_id,))
    flash("已重新排队；后台会再次核对价格证据后发送", "ok")
    return go("strategies")

def register(app):
    app.add_url_rule('/strategies', endpoint='strategies', view_func=strategies, methods=['GET'])
    app.add_url_rule('/messages', endpoint='messages', view_func=messages, methods=['GET'])
    app.add_url_rule('/messages', endpoint='save_messages', view_func=save_messages, methods=['POST'])
    app.add_url_rule('/messages/test/<channel>', endpoint='test_message_channel', view_func=test_message_channel, methods=['POST'])
    app.add_url_rule('/strategies/<int:strategy_id>', endpoint='add_strategy', view_func=add_strategy, methods=['POST'])
    app.add_url_rule('/strategies', endpoint='add_strategy', view_func=add_strategy, methods=['POST'])
    app.add_url_rule('/notifications/<int:notification_id>/read', endpoint='read_notice', view_func=read_notice, methods=['POST'])
    app.add_url_rule('/deliveries/<int:delivery_id>/retry', endpoint='retry_delivery', view_func=retry_delivery, methods=['POST'])
