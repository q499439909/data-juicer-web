#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
"$PYTHON" -c '
import json
from urllib.request import urlopen
from agent_web.config import Settings
s = Settings.from_env()
host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
with urlopen(f"http://{host}:{s.port}/health", timeout=5) as response:
    print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
'
