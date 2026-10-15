"""Environment configuration with Linux-friendly defaults."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    workspace_root: Path
    host: str
    port: int
    cookie_secure: bool
    ray_address: str
    task_timeout_seconds: int
    ray_bytes_threshold: int
    ray_file_threshold: int
    max_running_tasks: int = 3
    max_running_per_user: int = 1
    scheduler_cpu_capacity: float = 0
    scheduler_gpu_capacity: float = -1
    scheduler_poll_seconds: float = 1.0
    dispatch_lease_seconds: int = 30
    ray_auto_start: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parent
        data_root = Path(os.environ.get("AGENT_WEB_DATA_DIR", root / "data")).resolve()
        return cls(
            root=root,
            database_path=Path(os.environ.get("AGENT_WEB_DATABASE", data_root / "agent_web.db")).resolve(),
            workspace_root=Path(os.environ.get("AGENT_WEB_WORKSPACES", data_root / "workspaces")).resolve(),
            host=os.environ.get("AGENT_WEB_HOST", "0.0.0.0"),
            port=int(os.environ.get("AGENT_WEB_PORT", "8090")),
            cookie_secure=os.environ.get("AGENT_WEB_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"},
            ray_address=os.environ.get("RAY_ADDRESS", "http://127.0.0.1:8265"),
            task_timeout_seconds=int(os.environ.get("AGENT_WEB_TASK_TIMEOUT", "3600")),
            ray_bytes_threshold=int(os.environ.get("AGENT_WEB_RAY_BYTES_THRESHOLD", str(2 * 1024**3))),
            ray_file_threshold=int(os.environ.get("AGENT_WEB_RAY_FILE_THRESHOLD", "1000")),
            max_running_tasks=int(os.environ.get("AGENT_WEB_MAX_RUNNING_TASKS", "3")),
            max_running_per_user=int(os.environ.get("AGENT_WEB_MAX_RUNNING_PER_USER", "1")),
            scheduler_cpu_capacity=float(os.environ.get("AGENT_WEB_SCHEDULER_CPU_CAPACITY", "0")),
            scheduler_gpu_capacity=float(os.environ.get("AGENT_WEB_SCHEDULER_GPU_CAPACITY", "-1")),
            scheduler_poll_seconds=float(os.environ.get("AGENT_WEB_SCHEDULER_POLL_SECONDS", "1")),
            dispatch_lease_seconds=int(os.environ.get("AGENT_WEB_DISPATCH_LEASE_SECONDS", "30")),
            ray_auto_start=os.environ.get(
                "AGENT_WEB_RAY_AUTO_START", "true" if sys.platform.startswith("linux") else "false"
            ).lower() in {"1", "true", "yes"},
        )
