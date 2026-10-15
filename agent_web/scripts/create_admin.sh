#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
exec "$PYTHON" -m agent_web.scripts.create_admin "$@"
