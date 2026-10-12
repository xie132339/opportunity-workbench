#!/usr/bin/env python3
"""Create an online SQLite backup without copying a live database file."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source = Path(os.environ["WORKBENCH_DB"])
backup_dir = Path(os.environ["WORKBENCH_BACKUP_DIR"])
if not source.exists():
    raise SystemExit(f"database not found: {source}")

backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target = backup_dir / f"workbench-{stamp}.sqlite3"
temporary = target.with_suffix(".sqlite3.partial")
try:
    with sqlite3.connect(source) as source_db, sqlite3.connect(temporary) as backup_db:
        source_db.backup(backup_db)
        check = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        raise SystemExit(f"backup integrity check failed: {check}")
    temporary.replace(target)
finally:
    for path in (temporary, Path(f"{temporary}-shm"), Path(f"{temporary}-wal")):
        path.unlink(missing_ok=True)

digest = hashlib.sha256(target.read_bytes()).hexdigest()
print(f"backup={target} bytes={target.stat().st_size} sha256={digest}")
