"""Official Taobao activity-hall configuration routes."""
import os

from flask import flash, render_template, request

from db import connect
from routes.common import go
from services.taobao_campaigns import (create_campaign, list_campaigns,
                                       refresh_campaign)


def taobao_campaigns():
    with connect() as db:
        campaigns = list_campaigns(db)
    return render_template("taobao_campaigns.html", campaigns=campaigns)


def taobao_campaign_add():
    try:
        with connect() as db:
            campaign_id = create_campaign(
                db, name=request.form.get("name", ""),
                activity_material_id=request.form.get("activity_material_id", ""),
                material_id=request.form.get("material_id", ""),
                interval_minutes=request.form.get("interval_minutes", "60"), env=os.environ,
            )
        result = refresh_campaign(campaign_id, env=os.environ)
        if result["status"] == "verified":
            flash(f"官方活动 #{campaign_id} 已保存并完成会场核验。", "ok")
        else:
            flash(f"官方活动 #{campaign_id} 已保存，但会场核验失败：{result['error'][:180]}", "error")
    except (TypeError, ValueError) as exc:
        flash("官方活动保存失败：" + str(exc)[:240], "error")
    return go("taobao_campaigns")


def taobao_campaign_refresh(campaign_id):
    result = refresh_campaign(campaign_id, env=os.environ)
    if result["status"] == "verified":
        flash(f"官方活动 #{campaign_id} 已按最新官方响应更新。", "ok")
    else:
        flash(f"官方活动 #{campaign_id} 核验失败：{result['error'][:180]}", "error")
    return go("taobao_campaigns")


def register(app):
    app.add_url_rule("/taobao-campaigns", endpoint="taobao_campaigns", view_func=taobao_campaigns, methods=["GET"])
    app.add_url_rule("/taobao-campaigns", endpoint="taobao_campaign_add", view_func=taobao_campaign_add, methods=["POST"])
    app.add_url_rule("/taobao-campaigns/<int:campaign_id>/refresh", endpoint="taobao_campaign_refresh", view_func=taobao_campaign_refresh, methods=["POST"])
