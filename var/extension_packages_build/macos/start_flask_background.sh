#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

PORT="5000"

is_port_open() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1
    return $?
  fi
  return 1
}

kill_port_listeners() {
  local port="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  local pids
  pids="$(lsof -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ' | xargs)"
  if [[ -n "${pids:-}" ]]; then
    # shellcheck disable=SC2086
    kill -9 $pids >/dev/null 2>&1 || true
    sleep 0.4
  fi
}

PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "ERROR: No Python interpreter found (.venv/bin/python or python3)." >&2
    exit 1
  fi
fi

kill_port_listeners "$PORT"

nohup "$PYTHON_BIN" app.py --port "$PORT" >/dev/null 2>&1 &

echo "Started Flask app in background on 127.0.0.1:${PORT}"
