"""Detached local Data-Juicer wrapper that leaves a durable exit record."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from data_juicer_agents.tools.apply.apply_recipe.logic import ApplyUseCase


def _write_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    args = parser.parse_args()
    runtime_dir = Path(args.runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    cancelled = threading.Event()

    def request_cancel(_signum, _frame):
        cancelled.set()

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    result, returncode, stdout, stderr = ApplyUseCase().execute(
        plan, runtime_dir, timeout_seconds=args.timeout,
        cancel_check=cancelled.is_set,
    )
    (runtime_dir / "stdout.log").write_text(stdout, encoding="utf-8", errors="replace")
    (runtime_dir / "stderr.log").write_text(stderr, encoding="utf-8", errors="replace")
    _write_atomic(runtime_dir / "exit.json", {
        "returncode": returncode,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "error": result.error_message,
        "result_path": str(result.artifacts.get("export_path", "")),
    })
    return int(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
