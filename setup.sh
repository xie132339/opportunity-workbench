#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v python3.12 >/dev/null 2>&1; then
  printf '%s\n' '需要 Python 3.12。请先安装 Python 3.12，然后重新运行 bash setup.sh。' >&2
  exit 1
fi

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  .venv/bin/python -c 'from pathlib import Path; import re, secrets; p=Path(".env"); s=p.read_text(encoding="utf-8"); p.write_text(re.sub(r"(?m)^WORKBENCH_SECRET=.*$", "WORKBENCH_SECRET=" + secrets.token_urlsafe(32), s), encoding="utf-8")'
  chmod 600 .env
fi

printf '\n%s\n' '安装完成。运行：.venv/bin/python app.py serve'
