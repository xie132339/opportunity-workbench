#!/usr/bin/env bash
# Manage only this checkout's local web server and Taobao worker.
set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${WORKBENCH_PYTHON:-$ROOT/.venv/bin/python}"
PORT="${WORKBENCH_PORT:-5002}"
RUNTIME_DIR="${WORKBENCH_RUNTIME_DIR:-$ROOT/state/runtime}"
WEB_PID_FILE="$RUNTIME_DIR/web.pid"
WORKER_PID_FILE="$RUNTIME_DIR/taobao-worker.pid"
WEB_LOG="$RUNTIME_DIR/web.log"
WORKER_LOG="$RUNTIME_DIR/taobao-worker.log"

die() { printf '%s\n' "$*" >&2; exit 1; }

process_cwd() {
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

is_own_process() {
  local pid="$1" role="$2" command cwd
  kill -0 "$pid" 2>/dev/null || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  cwd="$(process_cwd "$pid")"
  [[ "$cwd" == "$ROOT" ]] || return 1
  case "$role" in
    web) [[ "$command" == *"app.py serve"* ]] ;;
    worker) [[ "$command" == *"app.py taobao-worker"* ]] ;;
    *) return 1 ;;
  esac
}

stop_pid() {
  local pid="$1" role="$2" pid_file="$3" attempt
  [[ -n "$pid" ]] || return 0
  if ! is_own_process "$pid" "$role"; then
    [[ -f "$pid_file" ]] && rm -f "$pid_file"
    return 0
  fi
  printf 'Stopping %s (pid %s)\n' "$role" "$pid"
  kill -TERM "$pid"
  for attempt in {1..50}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    printf 'Force stopping unresponsive %s (pid %s)\n' "$role" "$pid" >&2
    kill -KILL "$pid"
  fi
  rm -f "$pid_file"
}

stop_tracked() {
  local role="$1" pid_file="$2" pid
  [[ -f "$pid_file" ]] || return 0
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  stop_pid "$pid" "$role" "$pid_file"
}

stop_listeners_on_own_port() {
  local pid
  command -v lsof >/dev/null || return 0
  while IFS= read -r pid; do
    stop_pid "$pid" web "$WEB_PID_FILE"
  done < <(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
}

stop_untracked_workers() {
  local pid command
  while read -r pid command; do
    [[ "$command" == *"app.py taobao-worker"* ]] || continue
    stop_pid "$pid" worker "$WORKER_PID_FILE"
  done < <(ps -Ao pid=,command=)
}

stop_all() {
  stop_tracked web "$WEB_PID_FILE"
  stop_tracked worker "$WORKER_PID_FILE"
  stop_listeners_on_own_port
  stop_untracked_workers
}

wait_for_web() {
  local attempt
  for attempt in {1..50}; do
    if curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

start_all() {
  [[ -x "$PYTHON" ]] || die "Python 虚拟环境不存在：$PYTHON；请先运行 bash setup.sh"
  mkdir -p "$RUNTIME_DIR"
  stop_all
  (
    cd "$ROOT"
    nohup "$PYTHON" -B app.py serve >"$WEB_LOG" 2>&1 &
    printf '%s\n' "$!" >"$WEB_PID_FILE"
  )
  if ! wait_for_web; then
    printf '%s\n' "网页服务未能启动，最近日志：" >&2
    tail -n 30 "$WEB_LOG" >&2 || true
    stop_tracked web "$WEB_PID_FILE"
    exit 1
  fi
  (
    cd "$ROOT"
    nohup "$PYTHON" -B app.py taobao-worker >"$WORKER_LOG" 2>&1 &
    printf '%s\n' "$!" >"$WORKER_PID_FILE"
  )
  sleep 0.2
  is_own_process "$(cat "$WORKER_PID_FILE")" worker || die "淘宝 worker 未能启动；请检查 $WORKER_LOG"
  printf 'Running current source: http://127.0.0.1:%s/\n' "$PORT"
}

status() {
  local web_pid="" worker_pid=""
  [[ -f "$WEB_PID_FILE" ]] && web_pid="$(cat "$WEB_PID_FILE" 2>/dev/null || true)"
  [[ -f "$WORKER_PID_FILE" ]] && worker_pid="$(cat "$WORKER_PID_FILE" 2>/dev/null || true)"
  if is_own_process "$web_pid" web; then
    printf 'web: running (pid %s)\n' "$web_pid"
  else
    printf 'web: stopped\n'
  fi
  if is_own_process "$worker_pid" worker; then
    printf 'taobao-worker: running (pid %s)\n' "$worker_pid"
  else
    printf 'taobao-worker: stopped\n'
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    printf 'health: ok\n'
  else
    printf 'health: unavailable\n'
  fi
}

case "${1:-restart}" in
  start|restart) start_all ;;
  stop) stop_all ;;
  status) status ;;
  *) die "Usage: bash scripts/workbench.sh [start|stop|restart|status]" ;;
esac
