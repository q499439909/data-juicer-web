"""Durable, conversation-scoped Agent runs with replayable events."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator

import yaml
from agentscope.message import Msg

from agent_web.db.store import AppStore
from agent_web.workspaces import WorkspaceManager
from data_juicer_agents.capabilities.session.orchestrator import DJSessionAgent


class RunAlreadyActive(RuntimeError):
    pass


class StreamDelta:
    """Normalize either cumulative snapshots or native deltas into true deltas."""

    def __init__(self) -> None:
        self._mode: str | None = None
        self._assembled = ""

    def push(self, text: str) -> str:
        value = str(text or "")
        if not value:
            return ""
        if not self._assembled:
            self._assembled = value
            return value
        if self._mode is None:
            if value.startswith(self._assembled):
                self._mode = "cumulative"
                delta = value[len(self._assembled):]
                self._assembled = value
                return delta
            self._mode = "delta"
            self._assembled += value
            return value
        if self._mode == "delta":
            self._assembled += value
            return value
        if value.startswith(self._assembled):
            delta = value[len(self._assembled):]
            self._assembled = value
            return delta
        if self._assembled.startswith(value):
            return ""
        self._assembled = value
        return value


class AgentRuntime:
    """Own Agent execution; callers only start a run and replay its events."""

    def __init__(self, store: AppStore, workspaces: WorkspaceManager):
        self.store = store
        self.workspaces = workspaces
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def recover_interrupted(self) -> int:
        return self.store.fail_running_runs("Web 服务重启，本轮 Agent 处理已中断，请重新发送。")

    async def start_turn(
        self,
        owner_id: str,
        conversation_id: str,
        message: str,
        *,
        agent_message: str | None = None,
    ) -> dict[str, Any]:
        self.store.require_conversation(owner_id, conversation_id)
        if self.store.active_run(owner_id, conversation_id):
            raise RunAlreadyActive("当前会话已有正在运行的 Agent")
        self.store.add_message(owner_id, conversation_id, "user", message)
        try:
            run = self.store.create_run(owner_id, conversation_id, message)
        except sqlite3.IntegrityError as exc:
            raise RunAlreadyActive("当前会话已有正在运行的 Agent") from exc
        self._publish(run["id"], "status", {"stage": "thinking", "message": "Agent 正在分析需求"})
        task = asyncio.create_task(
            self._execute_run(
                run["id"], owner_id, conversation_id, message,
                agent_message=agent_message or message,
            ),
            name=f"agent-run-{run['id']}",
        )
        self._tasks[run["id"]] = task
        task.add_done_callback(lambda _task, run_id=run["id"]: self._tasks.pop(run_id, None))
        return run

    async def stream_events(
        self, owner_id: str, run_id: str, after: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        cursor = max(0, int(after))
        while True:
            events = self.store.list_run_events(owner_id, run_id, cursor)
            for event in events:
                cursor = int(event["sequence"])
                yield {
                    "sequence": cursor,
                    "type": event["event_type"],
                    **(event.get("payload") if isinstance(event.get("payload"), dict) else {}),
                }
            run = self.store.require_run(owner_id, run_id)
            if run["status"] != "running" and not events:
                break
            await asyncio.sleep(0.25)

    async def _execute_run(
        self,
        run_id: str,
        owner_id: str,
        conversation_id: str,
        message: str,
        *,
        agent_message: str,
    ) -> None:
        started = time.perf_counter()
        try:
            async with self._locks[conversation_id]:
                conversation = self.store.require_conversation(owner_id, conversation_id)
                workdir = self.workspaces.resolve_user_path(
                    owner_id, f"conversations/{conversation_id}"
                )
                workdir.mkdir(parents=True, exist_ok=True)

                def on_event(event: dict[str, Any]) -> None:
                    payload = dict(event)
                    event_type = str(payload.get("type", "activity"))
                    if event_type in {"tool_start", "tool_end"}:
                        self.store.add_invocation(
                            run_id,
                            "tool",
                            str(payload.get("tool", "tool")),
                            "success" if payload.get("ok", True) else "failed",
                            payload.get("args"),
                            payload.get("result_preview") or payload.get("summary"),
                        )
                    self._publish(run_id, "activity", {"event": payload})

                agent = DJSessionAgent(
                    use_llm_router=True,
                    working_dir=str(workdir),
                    verbose=False,
                    event_callback=on_event,
                    enable_streaming=True,
                )
                if conversation.get("snapshot_json"):
                    agent.restore_snapshot(json.loads(conversation["snapshot_json"]))

                stream_delta = StreamDelta()

                async def emit_chunk(msg: Any, last: bool) -> None:
                    text = str(msg.get_text_content() or "")
                    delta = stream_delta.push(text)
                    if delta:
                        self._publish(run_id, "chunk", {"text": delta, "last": bool(last)})

                reply = await agent.handle_as_studio_turn_async(
                    Msg(name="user", role="user", content=agent_message), emit_chunk
                )
                response = str(reply.msg.get_text_content() or "")
                self.store.add_message(owner_id, conversation_id, "assistant", response)
                plan = self._discover_plan(workdir) or agent.state.draft_plan
                self.store.save_conversation_state(
                    owner_id, conversation_id, agent.export_snapshot(), plan
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                self.store.finish_run(
                    run_id,
                    response,
                    "success",
                    duration_ms,
                    input_tokens=self._estimated_tokens(agent_message),
                    output_tokens=self._estimated_tokens(response),
                )
                self._publish(
                    run_id,
                    "final",
                    {"text": response, "run_id": run_id, "plan": self.plan_summary(plan)},
                )
        except asyncio.CancelledError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.store.finish_run(
                run_id, "", "failed", duration_ms, "Agent 运行被服务关闭中断"
            )
            self._publish(run_id, "error", {"message": "Agent 运行被服务关闭中断"})
            raise
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.store.finish_run(run_id, "", "failed", duration_ms, str(exc))
            self._publish(run_id, "error", {"message": str(exc)})

    def _publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.store.add_run_event(run_id, event_type, payload)

    @staticmethod
    def _estimated_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    @staticmethod
    def _discover_plan(workdir: Path) -> dict[str, Any] | None:
        candidates = sorted(
            [*workdir.rglob("*.yaml"), *workdir.rglob("*.yml")],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("recipe"), dict):
                return payload
        return None

    @staticmethod
    def plan_summary(plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(plan, dict):
            return None
        recipe = plan.get("recipe") if isinstance(plan.get("recipe"), dict) else {}
        process = recipe.get("process", []) if isinstance(recipe, dict) else []
        names = []
        if isinstance(process, list):
            for item in process:
                if isinstance(item, dict):
                    names.extend(str(key) for key in item.keys())
        return {
            "plan_id": str(plan.get("plan_id", "")),
            "operators": names,
            "operator_count": len(names),
            "dataset_path": str(recipe.get("dataset_path", "")),
            "export_path": str(recipe.get("export_path", "")),
        }
