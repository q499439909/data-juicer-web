"""Health and optional lifecycle management for a same-host Ray head."""

from __future__ import annotations

import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import urlopen


class RayRuntime:
    def __init__(self, address: str, auto_start: bool):
        self.address = address.rstrip("/")
        self.auto_start = auto_start
        self.available = False
        self.message = "尚未检查"
        self._last_check = 0.0

    def ensure_ready(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_check < 10:
            return self.available
        self._last_check = now
        if self._healthy():
            self.available, self.message = True, "正常"
            return True
        if not self.auto_start or not self._is_local_linux_address():
            self.available, self.message = False, "未连接"
            return False
        parsed = urlparse(self.address)
        port = parsed.port or 8265
        try:
            result = subprocess.run(
                [
                    "ray", "start", "--head", "--dashboard-host=127.0.0.1",
                    f"--dashboard-port={port}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.available, self.message = False, f"Ray 启动失败：{exc}"
            return False
        if result.returncode != 0:
            self.available, self.message = False, (result.stderr or result.stdout)[-500:]
            return False
        for _ in range(20):
            if self._healthy():
                self.available, self.message = True, "由 Web 启动并连接"
                return True
            time.sleep(0.5)
        self.available, self.message = False, "Ray 已启动但健康检查未通过"
        return False

    def _healthy(self) -> bool:
        try:
            with urlopen(f"{self.address}/api/version", timeout=1.5) as response:  # nosec B310 - configured localhost
                return 200 <= response.status < 500
        except Exception:
            return False

    def _is_local_linux_address(self) -> bool:
        parsed = urlparse(self.address)
        return sys.platform.startswith("linux") and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
