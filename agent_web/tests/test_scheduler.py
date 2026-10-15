import threading
import time
import json
import os
from pathlib import Path

from agent_web.config import Settings
from agent_web.db.store import AppStore
from agent_web.scheduler.resource_detector import detect_resources
from agent_web.scheduler.scheduler import TaskScheduler
from agent_web.execution.runner import TaskRunner
from agent_web.workspaces import WorkspaceManager


class _RayReady:
    available = True
    message = "正常"

    def ensure_ready(self, force=False):
        return True


class _RecordingRunner:
    def __init__(self, store: AppStore):
        self.store = store
        self.started: list[str] = []
        self.lock = threading.Lock()

    def execute(self, task_id: str):
        task = self.store.require_task_admin(task_id)
        self.store.update_task(task_id, status="running")
        with self.lock:
            self.started.append(task["owner_id"])
        time.sleep(0.02)
        self.store.update_task(task_id, status="succeeded", finished_at="done")

    def resume_ray(self, task_id: str):
        self.execute(task_id)

    def cancel(self, task_id: str):
        self.store.update_task(task_id, status="cancelled", finished_at="done")


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        root=tmp_path,
        database_path=tmp_path / "app.db",
        workspace_root=tmp_path / "workspaces",
        host="127.0.0.1",
        port=8090,
        cookie_secure=False,
        ray_address="http://127.0.0.1:1",
        task_timeout_seconds=30,
        ray_bytes_threshold=1024,
        ray_file_threshold=10,
        max_running_tasks=1,
        max_running_per_user=1,
        scheduler_cpu_capacity=4,
        scheduler_gpu_capacity=1,
        scheduler_poll_seconds=0.01,
        dispatch_lease_seconds=2,
        ray_auto_start=False,
    )
    values.update(overrides)
    return Settings(**values)


def _task(store: AppStore, owner: dict, conversation: dict, suffix: str,
          *, cpus=1, gpus=0):
    plan = {"plan_id": suffix, "recipe": {"dataset_path": "/tmp/in", "process": []}}
    return store.create_task(
        owner["id"], conversation["id"], suffix, "auto", "local", "test", plan,
        resource_class="gpu" if gpus else "cpu", requested_cpus=cpus,
        requested_gpus=gpus,
    )


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("scheduler condition was not reached")


def test_resource_detector_reads_explicit_gpu_and_cpu_requests():
    request = detect_resources({
        "recipe": {"process": [{"image_caption_mapper": {"num_cpus": 2, "num_gpus": 1}}]}
    })

    assert request.resource_class == "gpu"
    assert request.cpus == 4
    assert request.gpus == 1


def test_scheduler_round_robins_users_while_preserving_user_fifo(tmp_path: Path):
    settings = _settings(tmp_path)
    store = AppStore(settings.database_path)
    store.initialize()
    alice = store.create_user("alice", "Alice", "hash")
    bob = store.create_user("bob", "Bob", "hash")
    alice_conversation = store.create_conversation(alice["id"], "Alice")
    bob_conversation = store.create_conversation(bob["id"], "Bob")
    _task(store, alice, alice_conversation, "a1")
    _task(store, alice, alice_conversation, "a2")
    _task(store, bob, bob_conversation, "b1")
    store.set_scheduler_state("last_scheduled_user", bob["id"])
    runner = _RecordingRunner(store)
    scheduler = TaskScheduler(store, runner, settings)
    scheduler.ray = _RayReady()

    scheduler.start()
    _wait_for(lambda: len(runner.started) == 3)
    scheduler.stop()

    assert runner.started[0] != runner.started[1]
    assert runner.started[0] == runner.started[2]


def test_scheduler_persists_gpu_wait_reason_and_position(tmp_path: Path):
    settings = _settings(tmp_path, scheduler_gpu_capacity=0)
    store = AppStore(settings.database_path)
    store.initialize()
    user = store.create_user("alice", "Alice", "hash")
    conversation = store.create_conversation(user["id"], "GPU")
    task = _task(store, user, conversation, "gpu", gpus=1)
    scheduler = TaskScheduler(store, _RecordingRunner(store), settings)
    scheduler.ray = _RayReady()

    scheduler.start()
    _wait_for(lambda: store.require_task_admin(task["id"])["queue_reason"] == "waiting_gpu")
    decorated = scheduler.decorate(store.list_tasks(user["id"]))[0]
    scheduler.stop()

    assert decorated["status"] == "queued"
    assert decorated["queue_position"] == 1
    assert decorated["queue_reason_text"] == "等待 GPU 资源"


def test_queued_task_can_be_cancelled_without_starting_runner(tmp_path: Path):
    settings = _settings(tmp_path, scheduler_gpu_capacity=0)
    store = AppStore(settings.database_path)
    store.initialize()
    user = store.create_user("alice", "Alice", "hash")
    conversation = store.create_conversation(user["id"], "GPU")
    task = _task(store, user, conversation, "gpu", gpus=1)
    runner = _RecordingRunner(store)
    scheduler = TaskScheduler(store, runner, settings)
    scheduler.ray = _RayReady()

    scheduler.cancel(task["id"])

    assert store.require_task_admin(task["id"])["status"] == "cancelled"
    assert runner.started == []


def test_expired_dispatch_is_requeued_and_executed_after_restart(tmp_path: Path):
    settings = _settings(tmp_path)
    store = AppStore(settings.database_path)
    store.initialize()
    user = store.create_user("alice", "Alice", "hash")
    conversation = store.create_conversation(user["id"], "Recovery")
    task = _task(store, user, conversation, "recover")
    assert store.claim_task(task["id"], "dead-scheduler", "2000-01-01T00:00:00+00:00", [])
    runner = _RecordingRunner(store)

    restarted_store = AppStore(settings.database_path)
    restarted_store.initialize()
    scheduler = TaskScheduler(restarted_store, runner, settings)
    scheduler.ray = _RayReady()
    scheduler.start()
    _wait_for(lambda: restarted_store.require_task_admin(task["id"])["status"] == "succeeded")
    scheduler.stop()

    assert runner.started == [user["id"]]


def test_local_task_monitor_recovers_from_durable_exit_record(tmp_path: Path):
    settings = _settings(tmp_path)
    store = AppStore(settings.database_path)
    store.initialize()
    user = store.create_user("alice", "Alice", "hash")
    conversation = store.create_conversation(user["id"], "Local recovery")
    task = _task(store, user, conversation, "local")
    workspaces = WorkspaceManager(settings.workspace_root)
    runtime_dir = workspaces.resolve_user_path(user["id"], f"tasks/{task['id']}")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "exit.json").write_text(
        json.dumps({"returncode": 0, "result_path": str(runtime_dir / "result.jsonl")}),
        encoding="utf-8",
    )
    store.update_task(
        task["id"], status="running",
        runtime_metadata=json.dumps({"wrapper_pid": os.getpid()}),
    )

    TaskRunner(store, workspaces, settings.ray_address, 30).resume_local(task["id"])

    recovered = store.require_task_admin(task["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["result_relative_path"].endswith("result.jsonl")
