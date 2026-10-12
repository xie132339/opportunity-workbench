"""Dedicated holiday-promotion entry for Double 11, 618 and similar events."""
import os

from flask import flash, render_template, request

from db import connect
from routes.common import go
from services.festival_promotions import (create_festival, festival_options,
                                          list_festivals, refresh_festival)


def festival_promotions():
    with connect() as db:
        festivals = list_festivals(db)
    return render_template("festival_promotions.html", festivals=festivals,
                           festival_options=festival_options())


def festival_promotion_add():
    try:
        with connect() as db:
            festival_id = create_festival(
                db, festival_code=request.form.get("festival_code", ""),
                festival_year=request.form.get("festival_year", ""),
                festival_name=request.form.get("festival_name", ""),
                campaign_name=request.form.get("campaign_name", ""),
                activity_material_id=request.form.get("activity_material_id", ""),
                material_id=request.form.get("material_id", ""),
                interval_minutes=request.form.get("interval_minutes", "60"),
                minimum_discount=request.form.get("minimum_discount", "5"), env=os.environ,
            )
        result = refresh_festival(festival_id, env=os.environ)
        if result["status"] == "verified":
            flash(f"节日大促 #{festival_id} 已保存并完成官方会场核验。", "ok")
        else:
            flash(f"节日大促 #{festival_id} 已保存，但会场核验失败：{result['error'][:180]}", "error")
    except (TypeError, ValueError) as exc:
        flash("节日大促保存失败：" + str(exc)[:240], "error")
    return go("festival_promotions")


def festival_promotion_refresh(festival_id):
    result = refresh_festival(festival_id, env=os.environ)
    if result["status"] == "verified":
        flash(f"节日大促 #{festival_id} 已按最新官方响应更新。", "ok")
    else:
        flash(f"节日大促 #{festival_id} 核验失败：{result['error'][:180]}", "error")
    return go("festival_promotions")


def register(app):
    app.add_url_rule("/festival-promotions", endpoint="festival_promotions", view_func=festival_promotions, methods=["GET"])
    app.add_url_rule("/festival-promotions", endpoint="festival_promotion_add", view_func=festival_promotion_add, methods=["POST"])
    app.add_url_rule("/festival-promotions/<int:festival_id>/refresh", endpoint="festival_promotion_refresh", view_func=festival_promotion_refresh, methods=["POST"])
