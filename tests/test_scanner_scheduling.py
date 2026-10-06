import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import scanner


class ScannerSchedulingTests(unittest.TestCase):
    def test_failed_sources_retry_after_bounded_interval_but_healthy_keep_configured_interval(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(db, "DB_PATH", Path(temp) / "sources.db"):
            db.initialize()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            source_ids = {}
            with db.connect() as conn:
                # db.initialize() seeds the application's real source catalog;
                # keep this scheduling test isolated to the four rows below.
                conn.execute("UPDATE sources SET enabled=0")
                cases = (
                    ("failed_due", "failed", 60, now - timedelta(minutes=16)),
                    ("failed_not_due", "failed", 60, now - timedelta(minutes=14)),
                    ("healthy_not_due", "healthy", 60, now - timedelta(minutes=16)),
                    ("healthy_due", "healthy", 10, now - timedelta(minutes=11)),
                )
                for name, status, interval, checked in cases:
                    source_ids[name] = conn.execute(
                        """INSERT INTO sources(platform,name,category,url,method,status,enabled,interval_minutes,last_checked)
                        VALUES('test',?,'test',?,'rss',?,1,?,?)""",
                        (name, f"https://example.com/{name}", status, interval,
                         checked.strftime("%Y-%m-%d %H:%M:%S")),
                    ).lastrowid

            with patch("scanner.scan_source", side_effect=lambda source_id, review=False: {"source_id": source_id}), \
                 patch("autoreview.review_all"):
                results = scanner.scan_all(due_only=True)

            scanned = {result["source_id"] for result in results}
            self.assertEqual(scanned, {source_ids["failed_due"], source_ids["healthy_due"]})
