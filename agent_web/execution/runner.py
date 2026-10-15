"""Blocking execution adapter used exclusively by the persistent scheduler."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
import psutil

from agent_web.db.store import AppStore
from agent_web.workspaces import WorkspaceManager
from data_juicer_agents.tools.apply.submit_ray_job.logic import submit_ray_job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TaskRunner:
    def __init__(self, store: AppStore, workspaces: WorkspaceManager,
                 ray_address: str, timeout_seconds: int):
        self.store = store
        self.workspaces = workspaces
        self.ray_address = ray_address
        self.timeout_seconds = timeout_seconds

    def execute(self, task_id: str) -> None:
        task = self.store.require_task_admin(task_id)
        if task["cancel_requested"]:
            self.store.update_task(task_id, status="cancelled", finished_at=_now())
            return
        self.store.update_task(
            task_id,
            status="running",
            started_at=task.get("started_at") or _now(),
            heartbeat_at=_now(),
            lease_expires_at=None,
        )
        runtime_dir = self.workspaces.resolve_user_path(task["owner_id"], f"tasks/{task_id}")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        plan = json.loads(task["plan_json"])
        try:
            if task["resolved_executor"] == "ray":
                result = self._execute_ray(task_id, plan, runtime_dir)
            else:
                result = self._execute_local(task_id, plan, runtime_dir)
            self._finish(task_id, result)
        except Exception as exc:
            self.store.update_task(
                task_id, status="failed", finished_at=_now(), error_text=str(exc),
                heartbeat_at=_now(),
            )

    def resume_ray(self, task_id: str) -> None:
        task = self.store.require_task_admin(task_id)
        job_id = str(task.get("ray_job_id") or "")
        if not job_id:
            self.store.update_task(
                task_id, status="failed", finished_at=_now(),
                error_text="Web 重启后缺少 Ray Job ID，无法恢复监控。",
            )
            return
        result = self._monitor_ray(task_id, job_id, str(task.get("result_relative_path") or ""))
        self._finish(task_id, result)

    def resume_local(self, task_id: str) -> None:
        task = self.store.require_task_admin(task_id)
        try:
            metadata = json.loads(task.get("runtime_metadata") or "{}")
            wrapper_pid = int(metadata["wrapper_pid"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.store.update_task(
                task_id, status="failed", finished_at=_now(),
                error_text="Web 重启后缺少本地任务运行信息，无法恢复监控。",
            )
            return
        runtime_dir = self.workspaces.resolve_user_path(task["owner_id"], f"tasks/{task_id}")
        self._finish(task_id, self._monitor_local(task_id, wrapper_pid, runtime_dir))

    def cancel(self, task_id: str) -> None:
        task = self.store.require_task_admin(task_id)
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            return
        self.store.update_task(task_id, cancel_requested=1, status="cancelling")
        if task.get("ray_job_id"):
            subprocess.run(
                ["ray", "job", "stop", "--address", self.ray_address, task["ray_job_id"]],
                capture_output=True, text=True, timeout=30,
            )
        elif task.get("runtime_metadata"):
            try:
                metadata = json.loads(task["runtime_metadata"])
                self._terminate_process_group(int(metadata["wrapper_pid"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                pass

    def _finish(self, task_id: str, result: dict[str, Any]) -> None:
        self.store.update_task(
            task_id,
            status=result["status"],
            finished_at=_now(),
            heartbeat_at=_now(),
            scheduler_owner=None,
            queue_reason=None,
            log_text=str(result.get("output", ""))[-100_000:],
            error_text=result.get("error"),
            result_relative_path=str(result.get("result_path", "")) or None,
            ray_job_id=result.get("ray_job_id"),
            external_id=result.get("ray_job_id"),
        )

    def _execute_local(self, task_id: str, plan: dict[str, Any], runtime_dir: Path) -> dict[str, Any]:
        plan_path = runtime_dir / "submitted_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        for stale in (runtime_dir / "exit.json", runtime_dir / "stdout.log", runtime_dir / "stderr.log"):
            if stale.exists():
                stale.unlink()
        wrapper = subprocess.Popen(
            [
                sys.executable, "-m", "agent_web.execution.task_wrapper",
                "--plan", str(plan_path), "--runtime-dir", str(runtime_dir),
                "--timeout", str(self.timeout_seconds),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=self._local_environment(task_id),
        )
        metadata = {"wrapper_pid": wrapper.pid, "runtime_dir": str(runtime_dir)}
        self.store.update_task(
            task_id, external_id=f"pid:{wrapper.pid}",
            runtime_metadata=json.dumps(metadata), heartbeat_at=_now(),
        )
        return self._monitor_local(task_id, wrapper.pid, runtime_dir)

    def _monitor_local(self, task_id: str, wrapper_pid: int, runtime_dir: Path) -> dict[str, Any]:
        exit_path = runtime_dir / "exit.json"
        deadline = time.monotonic() + self.timeout_seconds + 30
        cancelled = False
        while time.monotonic() < deadline:
            task = self.store.require_task_admin(task_id)
            self.store.update_task(task_id, heartbeat_at=_now())
            if task["cancel_requested"]:
                cancelled = True
                self._terminate_process_group(wrapper_pid)
                break
            if exit_path.exists():
                break
            if not self._pid_exists(wrapper_pid):
                break
            time.sleep(0.5)
        if not exit_path.exists() and self._pid_exists(wrapper_pid):
            self._terminate_process_group(wrapper_pid)
        stdout = self._read_log(runtime_dir / "stdout.log")
        stderr = self._read_log(runtime_dir / "stderr.log")
        if cancelled:
            return {"status": "cancelled", "output": (stdout + "\n" + stderr).strip(), "error": None}
        if not exit_path.exists():
            return {
                "status": "failed", "output": (stdout + "\n" + stderr).strip(),
                "error": "本地任务进程异常退出，未生成退出记录。",
            }
        payload = json.loads(exit_path.read_text(encoding="utf-8"))
        returncode = int(payload.get("returncode", 1))
        return {
            "status": "succeeded" if returncode == 0 else "failed",
            "output": (stdout + "\n" + stderr).strip(),
            "error": None if returncode == 0 else str(payload.get("error") or stderr),
            "result_path": str(payload.get("result_path", "")),
        }

    def _execute_ray(self, task_id: str, plan: dict[str, Any], runtime_dir: Path) -> dict[str, Any]:
        plan_path = runtime_dir / "plan.yaml"
        plan_path.write_text(
            yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        submitted = submit_ray_job(
            plan_path=str(plan_path),
            ray_address=self.ray_address,
            timeout_seconds=self.timeout_seconds,
            no_wait=True,
        )
        job_id = str(submitted.get("job_id", ""))
        if not submitted.get("ok") or not job_id:
            return {
                "status": "failed",
                "output": (str(submitted.get("stdout", "")) + "\n" + str(submitted.get("stderr", ""))).strip(),
                "error": str(submitted.get("message", "Ray task submission failed")),
                "result_path": str(submitted.get("export_path", "")),
                "ray_job_id": job_id or None,
            }
        result_path = str(submitted.get("export_path", ""))
        self.store.update_task(
            task_id, ray_job_id=job_id, external_id=job_id,
            result_relative_path=result_path or None, heartbeat_at=_now(),
        )
        return self._monitor_ray(task_id, job_id, result_path)

    def _monitor_ray(self, task_id: str, job_id: str, result_path: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        final_status, status_output = "", ""
        while time.monotonic() < deadline:
            task = self.store.require_task_admin(task_id)
            self.store.update_task(task_id, heartbeat_at=_now())
            if task["cancel_requested"]:
                subprocess.run(
                    ["ray", "job", "stop", "--address", self.ray_address, job_id],
                    capture_output=True, text=True, timeout=30,
                )
                return {
                    "status": "cancelled", "output": "Ray job cancelled by user.",
                    "error": None, "result_path": result_path, "ray_job_id": job_id,
                }
            checked = subprocess.run(
                ["ray", "job", "status", "--address", self.ray_address, job_id],
                capture_output=True, text=True, timeout=30,
            )
            status_output = (checked.stdout + "\n" + checked.stderr).strip()
            upper = status_output.upper()
            if "SUCCEEDED" in upper:
                final_status = "succeeded"
                break
            if "FAILED" in upper:
                final_status = "failed"
                break
            if "STOPPED" in upper:
                final_status = "cancelled"
                break
            time.sleep(2)
        if not final_status:
            subprocess.run(
                ["ray", "job", "stop", "--address", self.ray_address, job_id],
                capture_output=True, text=True, timeout=30,
            )
            final_status, status_output = "failed", status_output + "\nTask timeout"
        logs = subprocess.run(
            ["ray", "job", "logs", "--address", self.ray_address, job_id],
            capture_output=True, text=True, timeout=30,
        )
        return {
            "status": final_status,
            "output": (logs.stdout + "\n" + logs.stderr + "\n" + status_output).strip(),
            "error": None if final_status in {"succeeded", "cancelled"} else "Ray task failed",
            "result_path": result_path,
            "ray_job_id": job_id,
        }

    @staticmethod
    def _read_log(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _local_environment(self, task_id: str) -> dict[str, str]:
        environment = dict(os.environ)
        task = self.store.require_task_admin(task_id)
        try:
            assigned = json.loads(task.get("assigned_gpu_ids") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            assigned = []
        if assigned:
            environment["CUDA_VISIBLE_DEVICES"] = ",".join(str(item) for item in assigned)
        return environment

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            # os.kill(pid, 0) can emit CTRL_C_EVENT on Windows rather than probe.
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    @staticmethod
    def _terminate_process_group(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=15,
            )
        else:
            os.killpg(pid, signal.SIGTERM)
