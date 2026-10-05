"""Local, source-first Chinese opportunity workbench application bootstrap."""
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from flask import Flask, abort, request, session

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
from autoreview import LABELS as REVIEW_LABELS, review_all, run_cycle, offer_summary
from benefits import KINDS as BENEFIT_KINDS, run_cycle as refresh_benefits, import_authorized_record
from channel_discovery import refresh as refresh_channel_candidates, validate_many as validate_channel_candidates
from link_resolution import run_cycle as resolve_links
from notifier import active_channels, deliver
from scanner import scan_all, scan_source, strategy_matches
from xianyu import api as xianyu_api, snapshot as xianyu_snapshot
from offer import RESOURCE_LABELS, TOPIC_LABELS, product_subcategory, paper_package_prices
from services.money import COST_FIELDS, cents, money
from services.opportunity_analysis import (CHECKOUT_WINDOW, RECENT_WINDOW, estimated_profit, public_profit_estimate,
    freshness_state, freshness_label, resale_assessment, is_evidence_candidate,
    is_current_notice, notice_rows)
from services.alert_dispatch import dispatch_verified_alerts


def create_app():
    application = Flask(__name__)
    application.secret_key = os.environ.get("WORKBENCH_SECRET") or secrets.token_hex(32)

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

    @application.before_request
    def csrf_protection():
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            abort(400, "Invalid form token")

    @application.context_processor
    def common():
        from autoreview import offer_summary as summarize_offer
        token = session.setdefault("csrf", secrets.token_urlsafe(24))
        return {"csrf": token, "money": money, "profit": estimated_profit, "review_labels": REVIEW_LABELS,
                "offer_summary": summarize_offer, "resource_labels": RESOURCE_LABELS, "topic_labels": TOPIC_LABELS,
                "product_subcategory": product_subcategory, "paper_package_prices": paper_package_prices}

    from routes import register_all
    register_all(application)
    return application


app = create_app()


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
    review_all()
    if command == "serve":
        app.run(host="127.0.0.1", port=5002, debug=False)
    elif command == "scan":
        print(scan_all())
        print(run_cycle())
    elif command == "review":
        print(run_cycle())
    elif command == "channels-refresh":
        print(refresh_channel_candidates())
    elif command == "channels-validate":
        print(validate_channel_candidates())
    elif command == "worker":
        while True:
            result = scan_all(due_only=True)
            if result:
                print(result, flush=True)
            print(run_cycle(), flush=True)
            print({"public_links": resolve_links()}, flush=True)
            print({"benefit_pages": refresh_benefits()}, flush=True)
            deliveries = dispatch_verified_alerts()
            if deliveries["sent"] or deliveries["failed"]:
                print(deliveries, flush=True)
            time.sleep(60)
    else:
        raise SystemExit("Usage: python app.py [serve|scan|review|worker|channels-refresh|channels-validate|benefits-import <normalized-records.jsonl>]")


if __name__ == "__main__":
    main()
