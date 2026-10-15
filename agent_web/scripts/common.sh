#!/usr/bin/env bash
# Always run the repository's virtual environment.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
if [ ! -x .venv/bin/python ]; then
  echo "Missing Linux virtual environment. Run: bash agent_web/install.sh" >&2
  exit 1
fi
export PATH="$PWD/.venv/bin:$PATH"
PYTHON="$PWD/.venv/bin/python"
DATA_DIR="$("$PYTHON" -c 'from agent_web.config import Settings; print(Settings.from_env().database_path.parent)')"
mkdir -p "$DATA_DIR"
PID_FILE="$DATA_DIR/agent_web.pid"
LOG_FILE="$DATA_DIR/agent_web.log"
