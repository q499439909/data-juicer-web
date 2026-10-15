#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. See https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
uv sync --locked --extra web --no-dev --python "${AGENT_WEB_PYTHON_VERSION:-3.12}"
mkdir -p agent_web/data
if [ ! -f agent_web/.env ]; then
  cp agent_web/.env.example agent_web/.env
  chmod 600 agent_web/.env
  echo "Created agent_web/.env. Set your model API key before using Agent chat."
fi
echo "Installed. Next: bash agent_web/scripts/create_admin.sh admin"
echo "Then: bash agent_web/start.sh"
