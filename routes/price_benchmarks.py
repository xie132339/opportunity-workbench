"""Operator-maintained product categories and fixed price-band rules."""
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from flask import abort, flash, redirect, render_template, request, url_for

from db import connect
from services.category_policy import encode_policy, validate_policy, decode_policy, match_category
from services.category_comparison_rules import default_rule, encode_rule, decode_rule
from services.maintained_price_baselines import (decode_terms, parse_terms, unit_label,
                                                  validate_baseline)
from services.maintained_price_candidates import load_maintained_price_candidates
from services.money import cents
from offer import TOPIC_LABELS


def _policy_from_form(form, prior=None):
    prior = prior or decode_policy(None)[0]
    max_age_raw = form.get("max_source_age_minutes", "").strip()
    offers_raw = form.get("minimum_comparable_offers", "").strip()
    policy_fields_present = any(key in form for key in ("max_source_age_minutes", "minimum_comparable_offers", "reference_enabled"))
    reference_enabled = form.get("reference_enabled") == "1" if policy_fields_present else prior["reference"]["enabled"]

    def optional_int(name, old):
        value = form.get(name, "").strip()
        return int(value) if value else old

    def optional_float(name, old):
        value = form.get(name, "").strip()
        return float(value) if value else old

    old_ref = prior["reference"]
    reference = dict(
        enabled=reference_enabled,
        price_basis=form.get("price_basis", old_ref["price_basis"]),
        window_days=optional_int("window_days", old_ref["window_days"]) if reference_enabled else None,
        minimum_independent_sources=optional_int("minimum_independent_sources", old_ref["minimum_independent_sources"]) if reference_enabled else None,
        below_reference_percent=optional_float("below_reference_percent", old_ref["below_reference_percent"]) if reference_enabled else None,
    )
    policy = dict(schema_version=1,
                  max_source_age_minutes=int(max_age_raw) if max_age_raw else prior["max_source_age_minutes"],
                  minimum_comparable_offers=int(offers_raw) if offers_raw else prior["minimum_comparable_offers"],
                  reference=reference)
    return validate_policy(policy)


def price_benchmarks():
    with connect() as db:
        db.execute("BEGIN")
        categories = db.execute("""SELECT c.*,p.name AS parent_name,0 AS matched_opportunity_count
            FROM benchmark_categories c LEFT JOIN benchmark_categories p ON p.id=c.parent_id
            ORDER BY COALESCE(p.name,c.name), c.parent_id IS NOT NULL, c.name""").fetchall()
        categories = [dict(row) for row in categories]
        category_counts = Counter()
        for opportunity in db.execute("SELECT title,topic FROM opportunities"):
            matched = match_category(opportunity["title"], opportunity["topic"], categories)
            if matched:
                category_counts[matched["id"]] += 1
        for row in categories:
            policy, error = decode_policy(row["policy_json"])
            comparison_rule, comparison_rule_error = decode_rule(row["comparison_rule_json"])
            row.update(matched_opportunity_count=category_counts[row["id"]], policy=policy, policy_error=error,
                       comparison_rule=comparison_rule, comparison_rule_error=comparison_rule_error)
            try:
                terms=json.loads(row["match_terms_json"] or "[]")
                row["match_terms"]=terms if isinstance(terms,list) and all(isinstance(term,str) for term in terms) else []
            except (TypeError,ValueError):
                row["match_terms"]=[]
        baselines = [dict(row) for row in db.execute("""SELECT b.*,c.name AS category_name,c.topic_key
            FROM maintained_price_baselines b JOIN benchmark_categories c ON c.id=b.category_id
            ORDER BY c.name,b.name,b.id""")]
        for row in baselines:
            row["match_terms"] = decode_terms(row["match_terms_json"])
            row["unit_label"] = unit_label(row)
        baseline_summary = dict(total=len(baselines), enabled=sum(bool(row["enabled"]) for row in baselines),
                                ready=sum(bool(row["enabled"]) for row in baselines))
        maintained_candidates = load_maintained_price_candidates(db, categories)
    return render_template("price_benchmarks.html", categories=categories, baselines=baselines,
                           baseline_summary=baseline_summary, maintained_candidates=maintained_candidates)


def _baseline_payload(form):
    return validate_baseline(dict(
        category_id=int(form.get("category_id", "")),
        name=form.get("name", ""),
        match_terms=parse_terms(form.get("match_terms", "")),
        unit_mode=form.get("unit_mode", ""),
        count_unit=form.get("count_unit", "").strip(),
        base_quantity=int(form.get("base_quantity", "")),
        exact_specification=form.get("exact_specification", ""),
        bargain_cents=cents(form.get("bargain_amount"), required=True),
        low_cents=cents(form.get("low_amount"), required=True),
        typical_cents=cents(form.get("typical_amount"), required=True),
        high_cents=cents(form.get("high_amount"), required=True),
        effective_from=form.get("effective_from", ""),
        enabled=form.get("enabled") == "1",
        notes=form.get("notes", ""),
    ))


def save_maintained_price_baseline():
    baseline_raw = request.form.get("baseline_id", "").strip()
    try:
        payload = _baseline_payload(request.form)
        with connect() as db:
            category = db.execute("SELECT id FROM benchmark_categories WHERE id=?", (payload["category_id"],)).fetchone()
            if category is None:
                raise ValueError("所选品类不存在")
            encoded_terms = json.dumps(payload["match_terms"], ensure_ascii=False)
            current = dict(payload, match_terms_json=encoded_terms)
            if baseline_raw:
                baseline_id = int(baseline_raw)
                old = db.execute("SELECT * FROM maintained_price_baselines WHERE id=?", (baseline_id,)).fetchone()
                if old is None:
                    raise ValueError("要编辑的维护基准不存在")
                previous = dict(old)
                changed = any(previous.get(key) != current.get(key) for key in current if key != "match_terms")
                version = previous["version"] + int(changed)
                db.execute("""UPDATE maintained_price_baselines SET category_id=?,name=?,match_terms_json=?,unit_mode=?,
                    count_unit=?,base_quantity=?,exact_specification=?,bargain_cents=?,low_cents=?,typical_cents=?,
                    high_cents=?,effective_from=?,enabled=?,notes=?,version=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (payload["category_id"],payload["name"],encoded_terms,payload["unit_mode"],payload["count_unit"],
                     payload["base_quantity"],payload["exact_specification"],payload["bargain_cents"],payload["low_cents"],
                     payload["typical_cents"],payload["high_cents"],payload["effective_from"],int(payload["enabled"]),
                     payload["notes"],version,baseline_id))
                if changed:
                    db.execute("""INSERT INTO maintained_price_baseline_audit
                        (baseline_id,version,previous_json,baseline_json) VALUES(?,?,?,?)""",
                        (baseline_id,version,json.dumps(previous,ensure_ascii=False,sort_keys=True),
                         json.dumps(current,ensure_ascii=False,sort_keys=True)))
            else:
                cursor = db.execute("""INSERT INTO maintained_price_baselines
                    (category_id,name,match_terms_json,unit_mode,count_unit,base_quantity,exact_specification,
                     bargain_cents,low_cents,typical_cents,high_cents,effective_from,enabled,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (payload["category_id"],payload["name"],encoded_terms,payload["unit_mode"],payload["count_unit"],
                     payload["base_quantity"],payload["exact_specification"],payload["bargain_cents"],payload["low_cents"],
                     payload["typical_cents"],payload["high_cents"],payload["effective_from"],int(payload["enabled"]),
                     payload["notes"]))
                db.execute("""INSERT INTO maintained_price_baseline_audit
                    (baseline_id,version,previous_json,baseline_json) VALUES(?,1,NULL,?)""",
                    (cursor.lastrowid,json.dumps(current,ensure_ascii=False,sort_keys=True)))
        flash("维护价格基准已保存；匹配到的官方优惠会按此版本计算", "ok")
    except (ValueError, TypeError, OverflowError) as exc:
        flash(str(exc) or "维护价格基准格式无效", "error")
    except sqlite3.IntegrityError:
        flash("保存失败：同一品类下的基准名称须唯一", "error")
    return redirect(url_for("price_benchmarks"))


def save_benchmark_category():
    category_id = request.form.get("category_id", "").strip()
    name = request.form.get("name", "").strip()
    unit = request.form.get("pricing_unit", "").strip()
    identity = request.form.get("identity_rule", "").strip()
    parent_raw = request.form.get("parent_id", "").strip()
    topic_key = request.form.get("topic_key", "other").strip()
    terms_raw = request.form.get("match_terms", "")
    try:
        comparison_rule = encode_rule(dict(schema_version=1,
            identity_mode=request.form.get("identity_mode", "merchant_or_exact_title"),
            unit_mode=request.form.get("unit_mode", "exact_spec"),
            count_unit=request.form.get("count_unit", "").strip()))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("price_benchmarks"))
    terms = list(dict.fromkeys(unicodedata.normalize("NFKC", term).strip()
                               for term in re.split(r"[,，\n]+", terms_raw)
                               if term.strip()))
    if (not name or len(name) > 80 or not unit or len(unit) > 240
            or not identity or len(identity) > 500 or not terms):
        flash("品类名称、计价口径、商品匹配规则和至少一个归类关键词必填；请检查长度", "error")
        return redirect(url_for("price_benchmarks"))
    if topic_key not in TOPIC_LABELS or len(terms) > 40 or any(len(term) > 50 for term in terms):
        flash("所属大类无效，或匹配词超过数量/长度限制", "error")
        return redirect(url_for("price_benchmarks"))
    try:
        parent_id = int(parent_raw) if parent_raw else None
        if category_id:
            category_id = int(category_id)
        with connect() as db:
            if parent_id is not None:
                parent = db.execute("SELECT id FROM benchmark_categories WHERE id=?", (parent_id,)).fetchone()
                if not parent or parent_id == category_id:
                    raise ValueError("上级品类无效")
                if category_id:
                    descendants = db.execute("""WITH RECURSIVE tree(id) AS (
                        SELECT id FROM benchmark_categories WHERE parent_id=?
                        UNION ALL SELECT c.id FROM benchmark_categories c JOIN tree t ON c.parent_id=t.id
                    ) SELECT 1 FROM tree WHERE id=? LIMIT 1""", (category_id, parent_id)).fetchone()
                    if descendants:
                        raise ValueError("不能将品类移动到自己的下级")
            if category_id:
                old = db.execute("SELECT * FROM benchmark_categories WHERE id=?", (category_id,)).fetchone()
                if old is None:
                    raise ValueError("要编辑的品类不存在")
                old_policy, _ = decode_policy(old["policy_json"])
                policy = _policy_from_form(request.form, old_policy)
                encoded = encode_policy(policy)
                previous_config = json.dumps(dict(name=old["name"], parent_id=old["parent_id"],
                    pricing_unit=old["pricing_unit"], identity_rule=old["identity_rule"],
                    topic_key=old["topic_key"], match_terms_json=old["match_terms_json"],
                    policy_json=old["policy_json"], comparison_rule_json=old["comparison_rule_json"]),
                    ensure_ascii=False, sort_keys=True)
                current_config = json.dumps(dict(name=name, parent_id=parent_id, pricing_unit=unit,
                    identity_rule=identity, topic_key=topic_key,
                    match_terms_json=json.dumps(terms,ensure_ascii=False), policy_json=encoded,
                    comparison_rule_json=comparison_rule),
                    ensure_ascii=False, sort_keys=True)
                version = old["version"] + int(previous_config != current_config)
                cursor = db.execute("""UPDATE benchmark_categories SET parent_id=?,name=?,pricing_unit=?,
                    identity_rule=?,topic_key=?,match_terms_json=?,policy_json=?,comparison_rule_json=?,version=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?""",
                    (parent_id, name, unit, identity, topic_key, json.dumps(terms,ensure_ascii=False),
                     encoded, comparison_rule, version, category_id))
                if cursor.rowcount != 1:
                    raise ValueError("要编辑的品类不存在")
                if previous_config != current_config:
                    db.execute("""INSERT INTO benchmark_category_rule_audit
                        (category_id,version,previous_policy_json,policy_json) VALUES(?,?,?,?)""",
                        (category_id,version,previous_config,current_config))
            else:
                policy = _policy_from_form(request.form)
                cursor = db.execute("""INSERT INTO benchmark_categories
                    (parent_id,name,pricing_unit,identity_rule,topic_key,match_terms_json,policy_json,comparison_rule_json)
                    VALUES(?,?,?,?,?,?,?,?)""", (parent_id,name,unit,identity,topic_key,
                    json.dumps(terms,ensure_ascii=False),encode_policy(policy),comparison_rule))
                db.execute("""INSERT INTO benchmark_category_rule_audit
                    (category_id,version,previous_policy_json,policy_json) VALUES(?,1,NULL,?)""",
                    (cursor.lastrowid,json.dumps(dict(topic_key=topic_key,match_terms=terms,policy=policy,
                        comparison_rule=json.loads(comparison_rule)),ensure_ascii=False,sort_keys=True)))
        flash("品类匹配词与判断规则已保存；证据仍须满足来源、同款和行情要求", "ok")
    except (ValueError, TypeError) as exc:
        flash(str(exc) or "品类编号无效", "error")
    except Exception as exc:
        # Keep uniqueness/foreign-key errors user-readable without leaking SQL details.
        flash("保存失败：品类名称须唯一，且上级品类必须存在", "error")
    return redirect(url_for("price_benchmarks"))


def save_topic_rule(topic_key):
    if topic_key not in TOPIC_LABELS:
        abort(404)
    try:
        max_age = int(request.form.get("max_source_age_minutes", ""))
        min_offers = int(request.form.get("minimum_comparable_offers", ""))
        enabled = request.form.get("reference_enabled") == "1"

        def optional_int(name):
            value = request.form.get(name, "").strip()
            return int(value) if value else None

        def optional_float(name):
            value = request.form.get(name, "").strip()
            return float(value) if value else None

        reference = dict(
            enabled=enabled, price_basis=request.form.get("price_basis", "merchant_public"),
            window_days=optional_int("window_days") if enabled else None,
            minimum_independent_sources=optional_int("minimum_independent_sources") if enabled else None,
            below_reference_percent=optional_float("below_reference_percent") if enabled else None,
        )
        policy = validate_policy(dict(schema_version=1, max_source_age_minutes=max_age,
                                      minimum_comparable_offers=min_offers, reference=reference))
        encoded = encode_policy(policy)
        with connect() as db:
            old = db.execute("SELECT policy_json,version FROM benchmark_topic_rules WHERE topic_key=?",
                             (topic_key,)).fetchone()
            if old is None:
                raise ValueError("该大类规则不存在")
            version = old["version"] + 1
            db.execute("UPDATE benchmark_topic_rules SET policy_json=?,version=?,updated_at=CURRENT_TIMESTAMP WHERE topic_key=?",
                       (encoded, version, topic_key))
            db.execute("""INSERT INTO benchmark_topic_rule_audit
                (topic_key,version,previous_policy_json,policy_json) VALUES(?,?,?,?)""",
                       (topic_key, version, old["policy_json"], encoded))
        flash(f"{TOPIC_LABELS[topic_key]}规则已保存（版本 {version}）；这不会生成行情样本", "ok")
    except (ValueError, TypeError, OverflowError) as exc:
        flash(str(exc) or "规则格式无效", "error")
    return redirect(url_for("price_benchmarks"))


def toggle_benchmark_provider(provider_id):
    watched = request.form.get("watched")
    if watched not in ("0", "1"):
        abort(400)
    with connect() as db:
        cursor = db.execute("""UPDATE benchmark_providers SET watched=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?""", (int(watched), provider_id))
        if cursor.rowcount != 1:
            abort(404)
    flash("已更新关注清单；提供方仍为未接入状态", "ok")
    return redirect(url_for("price_benchmarks"))


def register(app):
    app.add_url_rule("/price-benchmarks", endpoint="price_benchmarks",
                     view_func=price_benchmarks, methods=["GET"])
    app.add_url_rule("/price-benchmarks/maintained", endpoint="save_maintained_price_baseline",
                     view_func=save_maintained_price_baseline, methods=["POST"])
    app.add_url_rule("/price-benchmarks/categories", endpoint="save_benchmark_category",
                     view_func=save_benchmark_category, methods=["POST"])
    app.add_url_rule("/price-benchmarks/rules/<topic_key>", endpoint="save_topic_rule",
                     view_func=save_topic_rule, methods=["POST"])
    app.add_url_rule("/price-benchmarks/providers/<int:provider_id>/watch",
                     endpoint="toggle_benchmark_provider", view_func=toggle_benchmark_provider,
                     methods=["POST"])
