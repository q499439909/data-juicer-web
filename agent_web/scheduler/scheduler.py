"""Durable, resource-aware and user-fair task dispatcher."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from agent_web.config import Settings
from agent_web.db.store import AppStore
from agent_web.execution.runner import TaskRunner
from agent_web.scheduler.ray_runtime import RayRuntime


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TaskScheduler:
    def __init__(self, store: AppStore, runner: TaskRunner, settings: Settings):
        self.store = store
        self.runner = runner
        self.settings = settings
        self.owner = f"scheduler_{uuid4().hex}"
        self.ray = RayRuntime(settings.ray_address, settings.ray_auto_start)
        self.cpu_capacity = settings.scheduler_cpu_capacity or max(1, (os.cpu_count() or 2) - 2)
        self.gpu_capacity = (
            self._detect_gpu_count()
            if settings.scheduler_gpu_capacity < 0
            else settings.scheduler_gpu_capacity
        )
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_leader = False
        self._recovered = False
        self._workers: dict[str, threading.Thread] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="dja-scheduler")
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._is_leader:
            self.store.release_scheduler_lease(self.owner)

    def cancel(self, task_id: str) -> None:
        if not self.store.cancel_queued_task(task_id):
            self.runner.cancel(task_id)
        self.wake()

    def status(self) -> dict[str, Any]:
        active = self.store.active_tasks()
        return {
            "running": len(active),
            "max_running": self.settings.max_running_tasks,
            "cpu_used": sum(float(row.get("requested_cpus") or 0) for row in active),
            "cpu_capacity": self.cpu_capacity,
            "gpu_used": sum(float(row.get("requested_gpus") or 0) for row in active),
            "gpu_capacity": self.gpu_capacity,
            "queued": len(self.store.queued_tasks()),
            "ray_available": self.ray.available,
            "ray_message": self.ray.message,
        }

    def decorate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {task["id"]: task for task in self.store.queued_tasks()}
        counters: dict[str, int] = defaultdict(int)
        for task in self.store.queued_tasks():
            cls = str(task.get("resource_class") or "cpu")
            counters[cls] += 1
            by_id[task["id"]]["queue_position"] = counters[cls]
        for row in rows:
            row["queue_position"] = by_id.get(row["id"], {}).get("queue_position")
            row["queue_reason_text"] = self._reason_text(str(row.get("queue_reason") or ""))
        return rows

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                lease = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
                self._is_leader = self.store.acquire_scheduler_lease(
                    self.owner, lease, _now()
                )
                if not self._is_leader:
                    self._wake.wait(self.settings.scheduler_poll_seconds)
                    self._wake.clear()
                    continue
                if not self._recovered:
                    self.store.recover_dispatching_tasks(_now())
                    self._recover_running_tasks()
                    self.ray.ensure_ready(force=True)
                    self._recovered = True
                else:
                    self.store.recover_dispatching_tasks(_now())
                self.ray.ensure_ready()
                self._dispatch()
            except Exception:
                # A later tick retries; individual task failures are handled by the runner.
                pass
            self._wake.wait(self.settings.scheduler_poll_seconds)
            self._wake.clear()

    def _dispatch(self) -> None:
        while True:
            active = self.store.active_tasks()
            if len(active) >= self.settings.max_running_tasks:
                self._set_all_queue_reasons("global_limit")
                return
            queued = self.store.queued_tasks()
            if not queued:
                return
            users: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for task in queued:
                users[str(task["owner_id"])].append(task)
            owner_ids = sorted(users)
            last_owner = self.store.get_scheduler_state("last_scheduled_user")
            if last_owner in owner_ids:
                pivot = owner_ids.index(last_owner) + 1
                owner_ids = owner_ids[pivot:] + owner_ids[:pivot]
            active_by_user: dict[str, int] = defaultdict(int)
            for task in active:
                active_by_user[str(task["owner_id"])] += 1
            cpu_used = sum(float(task.get("requested_cpus") or 0) for task in active)
            gpu_used = sum(float(task.get("requested_gpus") or 0) for task in active)
            selected = None
            reason_updates: dict[str, str] = {}
            for owner_id in owner_ids:
                task = users[owner_id][0]
                if active_by_user[owner_id] >= self.settings.max_running_per_user:
                    reason_updates[task["id"]] = "user_limit"
                    continue
                cpus = float(task.get("requested_cpus") or 0)
                gpus = float(task.get("requested_gpus") or 0)
                if cpus + cpu_used > self.cpu_capacity:
                    reason_updates[task["id"]] = "waiting_cpu"
                    continue
                if gpus + gpu_used > self.gpu_capacity:
                    reason_updates[task["id"]] = "waiting_gpu"
                    continue
                if task.get("resolved_executor") == "ray" and not self.ray.available:
                    reason_updates[task["id"]] = "ray_unavailable"
                    continue
                selected = task
                break
            for task_id, reason in reason_updates.items():
                self.store.update_task(task_id, queue_reason=reason)
            if selected is None:
                return
            assigned = self._assign_gpus(active, float(selected.get("requested_gpus") or 0))
            lease = (datetime.now(timezone.utc) + timedelta(seconds=self.settings.dispatch_lease_seconds)).isoformat()
            if not self.store.claim_task(selected["id"], self.owner, lease, assigned):
                continue
            self.store.set_scheduler_state("last_scheduled_user", str(selected["owner_id"]))
            self._start_worker(selected["id"], self._run_task)

    def _run_task(self, task_id: str) -> None:
        try:
            self.runner.execute(task_id)
        finally:
            self.wake()

    def _recover_running_tasks(self) -> None:
        for task in self.store.active_tasks():
            if task["status"] == "running" and task.get("ray_job_id"):
                self._start_worker(task["id"], self.runner.resume_ray)
            elif task["status"] == "running":
                self._start_worker(task["id"], self.runner.resume_local)

    def _start_worker(self, task_id: str, target: Any) -> None:
        current = self._workers.get(task_id)
        if current and current.is_alive():
            return
        thread = threading.Thread(
            target=target,
            args=(task_id,),
            daemon=True,
            name=f"dja-task-{task_id[-8:]}",
        )
        self._workers[task_id] = thread
        thread.start()

    def _set_all_queue_reasons(self, reason: str) -> None:
        for task in self.store.queued_tasks():
            self.store.update_task(task["id"], queue_reason=reason)

    @staticmethod
    def _assign_gpus(active: list[dict[str, Any]], requested: float) -> list[int]:
        if requested <= 0:
            return []
        occupied = set()
        for task in active:
            try:
                occupied.update(json.loads(task.get("assigned_gpu_ids") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        needed = max(1, int(requested))
        result, candidate = [], 0
        while len(result) < needed:
            if candidate not in occupied:
                result.append(candidate)
            candidate += 1
        return result

    @staticmethod
    def _detect_gpu_count() -> float:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return float(len([line for line in result.stdout.splitlines() if line.strip()]))
        except (OSError, subprocess.SubprocessError):
            pass
        return 0.0

    @staticmethod
    def _reason_text(reason: str) -> str:
        return {
            "waiting_dispatch": "等待系统调度",
            "global_limit": "全局并发已满",
            "user_limit": "当前用户已有任务运行",
            "waiting_cpu": "等待 CPU 资源",
            "waiting_gpu": "等待 GPU 资源",
            "ray_unavailable": "等待 Ray 就绪",
            "recovering": "Web 重启后正在恢复",
        }.get(reason, "")
