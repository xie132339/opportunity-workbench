"""Price benchmark category rules and external provider registry."""
import json
import re
import unicodedata
from collections import Counter
from flask import abort, flash, redirect, render_template, request, url_for

from db import connect
from services.price_benchmark_evidence import price_claim_evidence
from services.source_provenance import source_provenance_evidence
from services.category_policy import encode_policy, validate_policy, decode_policy, match_category
from services.category_comparison_rules import (default_rule, encode_rule, decode_rule,
                                                IDENTITY_MODES, UNIT_MODES, COUNT_UNITS)
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
        evidence = price_claim_evidence(db)
        provenance = source_provenance_evidence(db)
        topic_rules = []
        for row in db.execute("SELECT * FROM benchmark_topic_rules ORDER BY topic_key"):
            policy, error = decode_policy(row["policy_json"])
            topic_rules.append(dict(row, label=TOPIC_LABELS.get(row["topic_key"], row["topic_key"]),
                                    policy=policy, policy_error=error))
    return render_template("price_benchmarks.html", categories=categories, evidence=evidence,
                           provenance=provenance, topic_rules=topic_rules)


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
    if not name or len(name) > 80 or not unit or len(unit) > 240 or not identity or len(identity) > 500:
        flash("品类名称、计价口径、商品匹配规则必填；请检查长度", "error")
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
    app.add_url_rule("/price-benchmarks/categories", endpoint="save_benchmark_category",
                     view_func=save_benchmark_category, methods=["POST"])
    app.add_url_rule("/price-benchmarks/rules/<topic_key>", endpoint="save_topic_rule",
                     view_func=save_topic_rule, methods=["POST"])
    app.add_url_rule("/price-benchmarks/providers/<int:provider_id>/watch",
                     endpoint="toggle_benchmark_provider", view_func=toggle_benchmark_provider,
                     methods=["POST"])
