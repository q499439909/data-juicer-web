from __future__ import annotations

import copy
import json
import mimetypes
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_web.auth.dependencies import app_state, current_user
from agent_web.db.store import ResourceNotFound
from agent_web.routes.conversations import _plan_dataset_stats
from agent_web.scheduler.resource_detector import detect_resources
from agent_web.state import AppState

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class CreateTaskBody(BaseModel):
    conversation_id: str
    executor: str = "auto"


class RetryBody(BaseModel):
    executor: str | None = None


def _operators(plan: dict) -> list[str]:
    recipe = plan.get("recipe") if isinstance(plan.get("recipe"), dict) else {}
    process = recipe.get("process", []) if isinstance(recipe, dict) else []
    names = []
    for item in process if isinstance(process, list) else []:
        if isinstance(item, dict):
            names.extend(str(key) for key in item.keys())
    return names


def _prepare_plan(state: AppState, owner_id: str, plan: dict) -> dict:
    prepared = copy.deepcopy(plan)
    recipe = prepared.get("recipe")
    if not isinstance(recipe, dict):
        raise HTTPException(400, "方案缺少 Data-Juicer recipe")
    dataset_path = str(recipe.get("dataset_path", "")).strip()
    if not dataset_path:
        raise HTTPException(400, "方案没有数据集路径，请先在工作台选择数据")
    workspace = state.workspaces.ensure_user_workspace(owner_id)
    dataset = Path(dataset_path).resolve()
    if not dataset.is_relative_to(workspace) or not dataset.exists():
        raise HTTPException(400, "数据集必须位于当前用户工作区")
    output = state.workspaces.resolve_user_path(owner_id, f"outputs/{uuid4().hex}/result.jsonl")
    output.parent.mkdir(parents=True, exist_ok=True)
    recipe["export_path"] = str(output)
    return prepared


@router.get("")
def list_tasks(user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    return state.scheduler.decorate(
        state.store.list_tasks(None if user["role"] == "admin" else user["id"])
    )


@router.post("")
def create_task(body: CreateTaskBody, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        conversation = state.store.require_conversation(user["id"], body.conversation_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    if not conversation.get("plan_json"):
        raise HTTPException(409, "当前会话还没有可执行方案")
    plan = _prepare_plan(state, user["id"], json.loads(conversation["plan_json"]))
    total_bytes, file_count = _plan_dataset_stats(state, user["id"], plan)
    recommendation = state.policy.recommend(
        operator_names=_operators(plan), total_bytes=total_bytes, file_count=file_count
    )
    resources = detect_resources(plan)
    requested = body.executor.lower()
    if requested not in {"auto", "local", "ray"}:
        raise HTTPException(400, "执行方式必须是 auto、local 或 ray")
    resolved = recommendation.recommended if requested == "auto" else requested
    if requested == "auto" and resources.gpus > 0:
        resolved = "ray"
    if resolved not in recommendation.allowed:
        raise HTTPException(409, "该方案不能使用所选执行方式")
    task = state.store.create_task(
        user["id"], body.conversation_id, conversation["title"], requested,
        resolved, recommendation.reason, plan,
        resource_class=resources.resource_class,
        requested_cpus=resources.cpus,
        requested_gpus=resources.gpus,
    )
    state.scheduler.wake()
    return task


@router.get("/{task_id}")
def task_detail(task_id: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
        if user["role"] == "admin" and task["owner_id"] != user["id"]:
            state.store.audit(user["id"], "view_task", "task", task_id)
        return state.scheduler.decorate([task])[0]
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    if task["status"] not in {"queued", "dispatching", "running", "cancelling"}:
        raise HTTPException(409, "当前任务不能取消")
    state.scheduler.cancel(task_id)
    if user["role"] == "admin" and task["owner_id"] != user["id"]:
        state.store.audit(user["id"], "cancel_task", "task", task_id)
    return {"ok": True}


@router.post("/{task_id}/retry")
def retry_task(task_id: str, body: RetryBody, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        original = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    if original["status"] not in {"failed", "cancelled", "succeeded"}:
        raise HTTPException(409, "当前任务不能重试")
    executor = body.executor or original["resolved_executor"]
    if executor not in {"local", "ray"}:
        raise HTTPException(400, "重试执行方式无效")
    task = state.store.create_task(
        original["owner_id"], original["conversation_id"], original["name"], executor, executor,
        "按历史任务配置重试。", json.loads(original["plan_json"]), retry_of=task_id,
        resource_class=original.get("resource_class") or "cpu",
        requested_cpus=float(original.get("requested_cpus") or 1),
        requested_gpus=float(original.get("requested_gpus") or 0),
    )
    state.scheduler.wake()
    if user["role"] == "admin" and original["owner_id"] != user["id"]:
        state.store.audit(user["id"], "retry_task", "task", task_id, {"new_task_id": task["id"]})
    return task


@router.delete("/{task_id}/result")
def delete_result(task_id: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    raw_path = str(task.get("result_relative_path") or "")
    if not raw_path:
        raise HTTPException(404, "任务没有结果文件")
    path = Path(raw_path).resolve()
    workspace = state.workspaces.ensure_user_workspace(task["owner_id"])
    if not path.is_relative_to(workspace):
        raise HTTPException(409, "结果路径不在用户工作区，已拒绝删除")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    state.store.update_task(task_id, result_relative_path=None)
    if user["role"] == "admin" and task["owner_id"] != user["id"]:
        state.store.audit(user["id"], "delete_task_result", "task", task_id)
    return {"ok": True}


@router.get("/{task_id}/result/download")
def download_result(task_id: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    raw_path = str(task.get("result_relative_path") or "")
    if not raw_path:
        raise HTTPException(404, "结果不存在或已删除")
    path = Path(raw_path).resolve()
    workspace = state.workspaces.ensure_user_workspace(task["owner_id"])
    if not path.is_relative_to(workspace) or not path.exists():
        raise HTTPException(404, "结果不存在或已删除")
    if user["role"] == "admin" and task["owner_id"] != user["id"]:
        state.store.audit(user["id"], "download_task_result", "task", task_id)
    if path.is_file():
        return FileResponse(path, filename=path.name)
    archive_base = state.workspaces.resolve_user_path(task["owner_id"], f"tasks/{task_id}/result_download")
    archive = Path(shutil.make_archive(str(archive_base), "zip", path))
    return FileResponse(archive, filename=f"{task_id}_result.zip")


@router.get("/{task_id}/result/manifest")
def result_manifest(task_id: str, user: dict = Depends(current_user),
                    state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    if task["status"] != "succeeded":
        raise HTTPException(409, "任务尚未成功完成")
    return state.results.build(task["owner_id"], task)


@router.get("/{task_id}/result/file")
def result_file(task_id: str, path: str, user: dict = Depends(current_user),
                state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id) if user["role"] == "admin" else state.store.require_task(user["id"], task_id)
        target = state.results.resolve_file(task["owner_id"], path)
    except (ResourceNotFound, ValueError) as exc:
        raise HTTPException(404, "结果文件不存在") from exc
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    safe_inline = target.suffix.lower() in {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
        ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".wav", ".ogg", ".flac", ".m4a",
    }
    return FileResponse(target, media_type=mime) if safe_inline else FileResponse(
        target, media_type=mime, filename=target.name
    )
