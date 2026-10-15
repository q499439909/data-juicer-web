"""Web application package; load configuration before importing the Agent runtime."""

import os
from pathlib import Path

from dotenv import load_dotenv

__version__ = "0.1.0"

load_dotenv(
    Path(os.environ.get("AGENT_WEB_ENV_FILE", Path(__file__).parent / ".env")),
    override=False,
)
