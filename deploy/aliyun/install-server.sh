#!/usr/bin/env bash
# Prepare this checkout for Ubuntu 24.04. Run as root from the checked-out app root.
set -euo pipefail

APP_ROOT="${APP_ROOT:-/application}"
APP_USER="workbench"
SERVICE_DIR="${APP_ROOT}/deploy/aliyun/systemd"
NGINX_SOURCE="${APP_ROOT}/deploy/aliyun/nginx/opportunity-workbench.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ ! -f "${APP_ROOT}/app.py" || ! -d "${SERVICE_DIR}" ]]; then
  echo "APP_ROOT must contain a checked-out opportunity-workbench revision: ${APP_ROOT}" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx python3.12-venv

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --home /var/lib/opportunity-workbench --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 /var/lib/opportunity-workbench
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 /var/lib/opportunity-workbench/backups
install -d -o root -g "${APP_USER}" -m 0750 /etc/opportunity-workbench

python3.12 -m venv "${APP_ROOT}/.venv"
"${APP_ROOT}/.venv/bin/python" -m pip install --upgrade pip
"${APP_ROOT}/.venv/bin/python" -m pip install -r "${APP_ROOT}/requirements.txt"
chown -R root:"${APP_USER}" "${APP_ROOT}"
chmod -R g-w "${APP_ROOT}"

for unit in opportunity-workbench-web.service opportunity-workbench-worker.service opportunity-workbench-backup.service; do
  sed "s|__APP_ROOT__|${APP_ROOT}|g" "${SERVICE_DIR}/${unit}" > "/etc/systemd/system/${unit}"
done
install -m 0644 "${SERVICE_DIR}/opportunity-workbench-backup.timer" /etc/systemd/system/opportunity-workbench-backup.timer
install -m 0644 "${NGINX_SOURCE}" /etc/nginx/sites-available/opportunity-workbench

if [[ ! -f /etc/opportunity-workbench/workbench.env ]]; then
  install -m 0640 -o root -g "${APP_USER}" "${APP_ROOT}/deploy/aliyun/workbench.env.example" /etc/opportunity-workbench/workbench.env
  echo "Created /etc/opportunity-workbench/workbench.env. Set a private WORKBENCH_ADMIN_PASSWORD before public activation." >&2
fi

systemctl daemon-reload
runuser -u "${APP_USER}" -- env WORKBENCH_DB=/var/lib/opportunity-workbench/workbench.sqlite3 "${APP_ROOT}/.venv/bin/gunicorn" --check-config wsgi:app
nginx -t

echo "Preparation complete. Services and Nginx are deliberately not enabled by this script."
