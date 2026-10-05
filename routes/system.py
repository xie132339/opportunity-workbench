"""Health and process-level HTTP routes."""
import os
from flask import jsonify
from db import connect

def health():
    with connect() as db:
        db.execute("SELECT 1").fetchone()
    return jsonify({"status": "ok", "database": "ok", "monitor_api_configured": bool(os.environ.get("CHANGED_API_TOKEN"))})

def register(app):
    app.add_url_rule('/health', endpoint='health', view_func=health, methods=['GET'])
