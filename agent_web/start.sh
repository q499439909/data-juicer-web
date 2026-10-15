#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--background" ]; }; then
  echo "Usage: bash agent_web/start.sh [--background]" >&2
  exit 2
fi
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Data-Juicer Agent Web is already running (PID $(cat "$PID_FILE"))."
  exit 0
fi
if [ "${1:-}" = "--background" ]; then
  nohup "$PYTHON" -m agent_web.app > "$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"
  for attempt in {1..30}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Web startup failed. See $LOG_FILE" >&2
      tail -n 30 "$LOG_FILE" >&2
      exit 1
    fi
    if "$PYTHON" -c 'from agent_web.config import Settings; from urllib.request import urlopen; s=Settings.from_env(); host="127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host; urlopen(f"http://{host}:{s.port}/health", timeout=1)' >/dev/null 2>&1; then
      echo "Started Data-Juicer Agent Web (PID $pid). Log: $LOG_FILE"
      exit 0
    fi
    sleep 1
  done
  echo "Process is running but readiness timed out. Check $LOG_FILE and run status.sh." >&2
  exit 1
fi
exec "$PYTHON" -m agent_web.app
