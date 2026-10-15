"""Application dependency container."""

from __future__ import annotations

from dataclasses import dataclass

from agent_web.agent_runtime.runtime import AgentRuntime
from agent_web.auth.provider import BuiltInCookieIdentityProvider, IdentityProvider
from agent_web.config import Settings
from agent_web.db.store import AppStore
from agent_web.execution.policy import ExecutionPolicy
from agent_web.execution.runner import TaskRunner
from agent_web.scheduler import TaskScheduler
from agent_web.datasets.provider import DatasetProvider, UnavailableDatasetProvider
from agent_web.results import ResultPresenter
from agent_web.workspaces import WorkspaceManager


@dataclass
class AppState:
    settings: Settings
    store: AppStore
    workspaces: WorkspaceManager
    policy: ExecutionPolicy
    agents: AgentRuntime
    tasks: TaskRunner
    scheduler: TaskScheduler
    results: ResultPresenter
    identity_provider: IdentityProvider
    dataset_provider: DatasetProvider


def build_state(settings: Settings | None = None) -> AppState:
    settings = settings or Settings.from_env()
    store = AppStore(settings.database_path)
    store.initialize()
    workspaces = WorkspaceManager(settings.workspace_root)
    policy = ExecutionPolicy(settings.ray_bytes_threshold, settings.ray_file_threshold)
    runner = TaskRunner(store, workspaces, settings.ray_address, settings.task_timeout_seconds)
    scheduler = TaskScheduler(store, runner, settings)
    agents = AgentRuntime(store, workspaces)
    agents.recover_interrupted()
    return AppState(
        settings=settings,
        store=store,
        workspaces=workspaces,
        policy=policy,
        agents=agents,
        tasks=runner,
        scheduler=scheduler,
        results=ResultPresenter(workspaces),
        identity_provider=BuiltInCookieIdentityProvider(),
        dataset_provider=UnavailableDatasetProvider(),
    )
