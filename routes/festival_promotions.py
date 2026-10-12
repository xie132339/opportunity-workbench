"""Dedicated holiday-promotion entry for Double 11, 618 and similar events."""
import os
import json
from datetime import datetime

from flask import flash, render_template, request

from db import connect
from services.double11_sync import snapshot as double11_snapshot, RULE_URL as DOUBLE11_RULE_URL
from routes.common import go
from services.festival_promotions import (festival_options,
                                          create_festival_material_feed,
                                          list_festivals, recommended_shop_offers)
from services.festival_optimizer import calculate_basket, example_basket, import_product
from services.taobao_alliance import (TaobaoAllianceAPIError,
                                      TaobaoAllianceConfigurationError,
                                      fetch_promoter_material_ids)


def festival_promotions():
    with connect() as db:
        festivals = list_festivals(db)
        shop_offers = recommended_shop_offers(db)
        double11 = double11_snapshot(db)
    result, error, imported, payload = None, "", None, None
    try:
        if request.method == "POST":
            raw = request.form.get("basket_json", "")
            if len(raw) > 40000:
                raise ValueError("清单过大，请保留最多 8 种商品、12 条优惠")
            payload = json.loads(raw)
            result = calculate_basket(payload)
        elif request.args.get("example") == "1":
            payload = example_basket()
            result = calculate_basket(payload)
        elif request.args.get("product_id"):
            product_id = request.args["product_id"]
            if not product_id.isdigit() or len(product_id) > 12:
                raise ValueError("请填写商品详情页中的数字编号")
            with connect() as db:
                imported = import_product(db, int(product_id))
    except (ValueError, TypeError, OverflowError) as exc:
        error = str(exc)[:240]
    return render_template("festival_promotions.html", festivals=festivals,
                           festival_options=festival_options(), shop_offers=shop_offers,
                           double11=double11, double11_rule_url=DOUBLE11_RULE_URL, basket_result=result,
                           basket_error=error, basket_payload=payload, basket_import=imported)


def festival_material_discover():
    report = {}
    try:
        materials = fetch_promoter_material_ids(env=os.environ, report=report)
        error = ""
    except (TaobaoAllianceAPIError, TaobaoAllianceConfigurationError, ValueError) as exc:
        materials = []
        error = str(exc)[:240]
    return render_template("festival_material_discovery.html", materials=materials,
                           error=error, report=report, festival_year=datetime.now().year,
                           festival_options=festival_options())


def festival_material_add():
    material_id = str(request.form.get("material_id") or "").strip()
    try:
        materials = fetch_promoter_material_ids(env=os.environ)
        material = next((item for item in materials if item["material_id"] == material_id), None)
        if not material:
            raise ValueError("该物料已不在淘宝联盟当前可发现列表中，请重新发现")
        material["festival_name"] = request.form.get("festival_name", "")
        with connect() as db:
            feed_id = create_festival_material_feed(
                db, festival_code=request.form.get("festival_code", ""),
                festival_year=request.form.get("festival_year", ""), material=material,
                interval_minutes=request.form.get("interval_minutes", "60"),
                minimum_discount=request.form.get("minimum_discount", "5"), env=os.environ,
            )
        flash(f"官方物料已加入节日监控 #{feed_id}；后续由淘宝 worker 定时采集。", "ok")
        return go("festival_promotions")
    except (TaobaoAllianceAPIError, TaobaoAllianceConfigurationError, TypeError, ValueError) as exc:
        flash("节日物料添加失败：" + str(exc)[:240], "error")
        return go("festival_promotions")


def register(app):
    app.add_url_rule("/festival-promotions", endpoint="festival_promotions", view_func=festival_promotions, methods=["GET", "POST"])
    app.add_url_rule("/festival-promotions/discover", endpoint="festival_material_discover", view_func=festival_material_discover, methods=["POST"])
    app.add_url_rule("/festival-promotions/materials", endpoint="festival_material_add", view_func=festival_material_add, methods=["POST"])
