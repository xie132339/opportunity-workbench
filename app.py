"""Local, source-first Chinese opportunity workbench application bootstrap."""
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from flask import Flask, flash, redirect, request, session, url_for
from flask.testing import FlaskClient

ROOT = Path(__file__).resolve().parent

def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

# Load local settings before importing modules that read settings during import.
load_env()

from db import connect, initialize
from autoreview import LABELS as REVIEW_LABELS, run_cycle, offer_summary
from benefits import KINDS as BENEFIT_KINDS, run_cycle as refresh_benefits, import_authorized_record
from channel_discovery import refresh as refresh_channel_candidates, validate_many as validate_channel_candidates
from notifier import active_channels, deliver
from scanner import review_taobao_backlog, scan_all, scan_source, strategy_matches
from services.link_resolution_worker import run_periodically as run_link_resolution_worker
from services.taobao_sync_validation import detail_batch_limit, validate_taobao_syncs
from services.taobao_campaigns import refresh_due_campaigns
from xianyu import api as xianyu_api, snapshot as xianyu_snapshot
from offer import RESOURCE_LABELS, TOPIC_LABELS, product_subcategory, paper_package_prices
from services.money import COST_FIELDS, cents, money
from services.opportunity_analysis import (CHECKOUT_WINDOW, RECENT_WINDOW, estimated_profit, public_profit_estimate,
    freshness_state, freshness_label, resale_assessment, is_evidence_candidate,
    is_current_notice, notice_rows)
from services.alert_dispatch import dispatch_verified_alerts
from services.local_auth import sign_in, signed_in


class WorkbenchTestClient(FlaskClient):
    """Keep existing business-route tests focused while production stays gated."""

    def __init__(self, *args, authenticated=True, **kwargs):
        self._authenticated = authenticated
        super().__init__(*args, **kwargs)

    def open(self, *args, **kwargs):
        if self._authenticated:
            with self.session_transaction() as stored_session:
                sign_in(stored_session, "test-user")
        return super().open(*args, **kwargs)


def create_app():
    application = Flask(__name__)
    application.secret_key = os.environ.get("WORKBENCH_SECRET") or secrets.token_hex(32)
    # "保持登录"复选框开启后，会话 cookie 在 PERMANENT_SESSION_LIFETIME 内保持；
    # 未勾选则维持浏览器会话 cookie（关闭浏览器即失效）。
    application.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    application.test_client_class = WorkbenchTestClient

    @application.template_filter("cn_time")
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

    application.add_template_filter(freshness_label, "freshness")

    # Web 进程启动即确保校准数据新鲜：worker 每小时重算落盘，但 Gunicorn
    # (wsgi:app) 不经过 app.py main()，此处兜底让 web 进程在表空/过期时
    # 自行校准一次，避免生产环境停留在固定种子兜底。
    try:
        from services import discount_calibration, shop_reputation
        with connect() as connection:
            discount_calibration.refresh_if_stale(connection)
            shop_reputation.refresh_stats_if_stale(connection)
    except Exception:
        application.logger.exception("discount calibration refresh failed at startup")

    @application.before_request
    def access_control_and_csrf_protection():
        if request.endpoint not in {"login", "health", "static"} and not signed_in(session):
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            # 登录页被浏览器缓存或会话 cookie 过期时，旧页面携带的 csrf 会失效；
            # 不再裸 400（浏览器显示 Bad Request 用户无从下手），改为提示并回到登录页重试。
            flash("页面会话已过期，请刷新后重新提交。", "error")
            next_path = request.form.get("next") or "/"
            return redirect(url_for("login", next=next_path))

    @application.after_request
    def no_cache_login_page(response):
        # 登录页禁止缓存：否则浏览器复用旧 HTML 里的 csrf token，会话 cookie 失效后必现 400
        if request.endpoint == "login":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    @application.context_processor
    def common():
        from autoreview import offer_summary as summarize_offer
        token = session.setdefault("csrf", secrets.token_urlsafe(24))
        return {"csrf": token, "current_user": session.get("workbench_user"), "money": money, "profit": estimated_profit, "review_labels": REVIEW_LABELS,
                "offer_summary": summarize_offer, "resource_labels": RESOURCE_LABELS, "topic_labels": TOPIC_LABELS,
                "product_subcategory": product_subcategory, "paper_package_prices": paper_package_prices}

    from routes import register_all
    register_all(application)
    return application


app = create_app()


def taobao_detail_batch_limit():
    """Compatibility wrapper for configuration shown in current documentation."""
    return detail_batch_limit(os.environ)


def complete_taobao_syncs(source_results):
    """Attach an exact per-run verification receipt after official collection."""
    return validate_taobao_syncs(source_results, run_detail_cycle=run_cycle,
                                 batch_size=taobao_detail_batch_limit())


def run_taobao_cycle():
    """Collect official Taobao material and audit every synchronized record."""
    from services.double11_sync import refresh as refresh_double11
    double11 = refresh_double11(due_only=True)
    campaigns = refresh_due_campaigns(env=os.environ)
    sources = scan_all(due_only=True, method="taobao_alliance")
    backlog = review_taobao_backlog()
    if not campaigns and not sources and not backlog["selected"] and not double11:
        return {}
    result = {"sources": sources, "review_backlog": backlog}
    if double11:
        result["double11"] = double11
    if campaigns:
        result["campaigns"] = campaigns
    if sources:
        result["post_sync_validation"] = complete_taobao_syncs(sources)
    return result


def main():
    initialize()
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "benefits-import":
        if len(sys.argv) != 3:
            raise SystemExit("Usage: python app.py benefits-import <normalized-records.jsonl>")
        imported = 0
        with open(sys.argv[2], encoding="utf-8") as records:
            for line_number, line in enumerate(records, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    import_authorized_record(record)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise SystemExit(f"优惠记录第 {line_number} 行未导入：{exc}") from exc
                imported += 1
        print({"imported": imported})
        return
    if command == "serve":
        app.run(host="127.0.0.1", port=5002, debug=False)
    elif command == "scan":
        results = scan_all()
        print(results)
        print({"taobao_post_sync_validation": complete_taobao_syncs(results)})
        print(run_cycle())
    elif command == "taobao-worker":
        while True:
            result = run_taobao_cycle()
            if result:
                print(result, flush=True)
            from services import discount_calibration, shop_reputation
            with connect() as connection:
                discount_calibration.refresh_if_stale(connection)
                shop_reputation.refresh_stats_if_stale(connection)
            time.sleep(60)
    elif command == "review":
        print(run_cycle())
    elif command == "channels-refresh":
        print(refresh_channel_candidates())
    elif command == "channels-validate":
        print(validate_channel_candidates())
    elif command == "worker":
        link_stop = threading.Event()
        link_worker = threading.Thread(
            target=run_link_resolution_worker,
            args=(link_stop,),
            name="public-link-resolution",
            daemon=True,
        )
        link_worker.start()
        try:
            while True:
                result = scan_all(due_only=True)
                if result:
                    print(result, flush=True)
                print(run_cycle(), flush=True)
                print({"benefit_pages": refresh_benefits()}, flush=True)
                deliveries = dispatch_verified_alerts()
                if deliveries["sent"] or deliveries["failed"]:
                    print(deliveries, flush=True)
                from services import discount_calibration, shop_reputation
                with connect() as connection:
                    discount_calibration.refresh_if_stale(connection)
                    shop_reputation.refresh_stats_if_stale(connection)
                time.sleep(60)
        finally:
            link_stop.set()
            link_worker.join(timeout=2)
    else:
        raise SystemExit("Usage: python app.py [serve|scan|review|worker|taobao-worker|channels-refresh|channels-validate|benefits-import <normalized-records.jsonl>]")


if __name__ == "__main__":
    main()
