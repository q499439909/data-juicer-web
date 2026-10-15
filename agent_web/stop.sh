#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
if [ ! -f "$PID_FILE" ]; then
  echo "No background process recorded; stop a foreground process with Ctrl+C."
  exit 0
fi
pid="$(cat "$PID_FILE")"
if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
  echo "Invalid PID file: $PID_FILE" >&2
  exit 1
fi
if kill -0 "$pid" 2>/dev/null; then
  if ! ps -p "$pid" -o args= | grep -q 'python.*-m agent_web.app'; then
    echo "PID $pid does not belong to this Web service; refusing to stop it." >&2
    exit 1
  fi
  kill "$pid"
  for attempt in {1..30}; do
    if ! kill -0 "$pid" 2>/dev/null || [ "$(ps -p "$pid" -o stat= | cut -c1)" = Z ]; then
      rm -f "$PID_FILE"
      echo "Stopped Web service. Ray and submitted jobs are managed separately."
      exit 0
    fi
    sleep 1
  done
  echo "Still stopping; PID file retained. Check $LOG_FILE." >&2
  exit 1
fi
rm -f "$PID_FILE"
echo "Web service was already stopped."
