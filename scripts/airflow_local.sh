#!/usr/bin/env bash
# Run a local Airflow stack against airflow/dags without using gunicorn.
#
# Why no gunicorn?  On macOS / Apple Silicon, gunicorn's `sync` worker calls
# fork() and the child segfaults at C-extension import time (numpy / cryptography
# / grpc arm64 wheels, dyld interposers, etc.).  `airflow standalone` uses
# gunicorn under the hood, so we avoid it entirely and use Flask's built-in dev
# server (`airflow webserver --debug`), which stays single-process and never
# fork()s.  scheduler + triggerer are normal subprocesses; no fork issues there.
#
# Layout:
#   - scheduler runs in the background
#   - triggerer runs in the background
#   - webserver --debug runs in the foreground
#   - on SIGINT/SIGTERM/EXIT we tear down the background processes
#
# Configurable via env vars:
#   AIRFLOW_HOME_LOCAL  default: $REPO_ROOT/.airflow
#   AIRFLOW_PORT        default: 8088
#   ADMIN_USER          default: admin
#   ADMIN_PASS          default: admin

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AIRFLOW_HOME_LOCAL="${AIRFLOW_HOME_LOCAL:-$REPO_ROOT/.airflow}"
AIRFLOW_PORT="${AIRFLOW_PORT:-8088}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin}"

mkdir -p "$AIRFLOW_HOME_LOCAL/logs"

# Refuse to start if the port is already taken; otherwise the webserver hangs
# silently retrying the bind.
if lsof -nP -iTCP:"$AIRFLOW_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port $AIRFLOW_PORT is already in use." >&2
  echo "       Run 'make airflow-down' or pick another port:" >&2
  echo "       make airflow-up AIRFLOW_PORT=8081" >&2
  exit 1
fi

export AIRFLOW_HOME="$AIRFLOW_HOME_LOCAL"
export AIRFLOW__CORE__DAGS_FOLDER="$REPO_ROOT/airflow/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__EXECUTOR=SequentialExecutor
export AIRFLOW__WEBSERVER__WEB_SERVER_PORT="$AIRFLOW_PORT"
# Belt-and-suspenders for macOS even though --debug avoids gunicorn entirely.
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export NO_PROXY='*'

run() { poetry run "$@"; }

echo "==> Initializing Airflow metadata DB (idempotent)..."
run airflow db migrate >/dev/null

echo "==> Ensuring admin user exists ($ADMIN_USER / $ADMIN_PASS)..."
# `users create` exits non-zero if the user already exists; that's fine.
run airflow users create \
  --username "$ADMIN_USER" \
  --password "$ADMIN_PASS" \
  --firstname Admin \
  --lastname User \
  --role Admin \
  --email "${ADMIN_USER}@example.com" >/dev/null 2>&1 || true

PIDS=()
cleanup() {
  echo
  echo "==> Stopping background Airflow processes..."
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  # Give them a moment, then SIGKILL anything that didn't exit.
  sleep 1
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

echo "==> Starting scheduler (logs: $AIRFLOW_HOME_LOCAL/logs/scheduler.out)"
run airflow scheduler >"$AIRFLOW_HOME_LOCAL/logs/scheduler.out" 2>&1 &
PIDS+=($!)

echo "==> Starting triggerer (logs: $AIRFLOW_HOME_LOCAL/logs/triggerer.out)"
run airflow triggerer >"$AIRFLOW_HOME_LOCAL/logs/triggerer.out" 2>&1 &
PIDS+=($!)

echo
echo "================================================================"
echo "  Airflow UI:  http://localhost:$AIRFLOW_PORT"
echo "  Login:       $ADMIN_USER / $ADMIN_PASS"
echo "  DAG:         reactivation_campaign"
echo "  Press Ctrl-C to stop all components."
echo "================================================================"
echo

# Foreground: Flask dev server (no gunicorn => no fork => no SIGSEGV).
run airflow webserver --debug --port "$AIRFLOW_PORT"
