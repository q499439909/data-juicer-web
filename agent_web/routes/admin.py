from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_web.auth.dependencies import admin_user, app_state
from agent_web.auth.passwords import hash_password
from agent_web.db.store import ResourceNotFound
from agent_web.state import AppState

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserPatch(BaseModel):
    status: str | None = None
    role: str | None = None
    temporary_password: str | None = Field(default=None, min_length=8, max_length=128)


class AdminRetryBody(BaseModel):
    executor: str | None = None


class ConversationAdminPatch(BaseModel):
    hidden: bool


@router.get("/overview")
def overview(admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    return {**state.store.overview(), "scheduler": state.scheduler.status()}


@router.get("/runs")
def runs(admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    return state.store.list_runs()


@router.get("/runs/{run_id}")
def run_detail(run_id: str, admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    try:
        detail = state.store.run_detail(run_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "运行不存在") from exc
    state.store.audit(admin["id"], "view_run_content", "run", run_id)
    return detail


@router.get("/tasks")
def tasks(admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    return state.scheduler.decorate(state.store.list_tasks())


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    try:
        state.store.require_task_admin(task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    state.scheduler.cancel(task_id)
    state.store.audit(admin["id"], "cancel_task", "task", task_id)
    return {"ok": True}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, body: AdminRetryBody, admin: dict = Depends(admin_user),
               state: AppState = Depends(app_state)):
    try:
        original = state.store.require_task_admin(task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    executor = body.executor or original["resolved_executor"]
    if executor not in {"local", "ray"}:
        raise HTTPException(400, "执行方式无效")
    task = state.store.create_task(
        original["owner_id"], original["conversation_id"], original["name"], executor,
        executor, "管理员按历史配置重试。", json.loads(original["plan_json"]), retry_of=task_id,
        resource_class=original.get("resource_class") or "cpu",
        requested_cpus=float(original.get("requested_cpus") or 1),
        requested_gpus=float(original.get("requested_gpus") or 0),
    )
    state.store.audit(admin["id"], "retry_task", "task", task_id, {"new_task_id": task["id"]})
    state.scheduler.wake()
    return task


@router.delete("/tasks/{task_id}/result")
def delete_result(task_id: str, admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    try:
        task = state.store.require_task_admin(task_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "任务不存在") from exc
    raw_path = str(task.get("result_relative_path") or "")
    if raw_path:
        path = Path(raw_path).resolve()
        workspace = state.workspaces.ensure_user_workspace(task["owner_id"])
        if not path.is_relative_to(workspace):
            raise HTTPException(409, "结果路径不在用户工作区")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        state.store.update_task(task_id, result_relative_path=None)
    state.store.audit(admin["id"], "delete_task_result", "task", task_id)
    return {"ok": True}


@router.get("/users")
def users(admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    return state.store.list_users()


@router.patch("/conversations/{conversation_id}")
def patch_conversation(conversation_id: str, body: ConversationAdminPatch,
                       admin: dict = Depends(admin_user), state: AppState = Depends(app_state)):
    try:
        conversation = state.store.require_conversation_admin(conversation_id)
        state.store.set_conversation_hidden(
            conversation["owner_id"], conversation_id, body.hidden
        )
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    state.store.audit(
        admin["id"], "hide_conversation" if body.hidden else "restore_conversation",
        "conversation", conversation_id,
    )
    return {"ok": True}


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, admin: dict = Depends(admin_user),
               state: AppState = Depends(app_state)):
    try:
        target = state.store.require_user(user_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "用户不存在") from exc
    if body.status not in {None, "active", "disabled"} or body.role not in {None, "user", "admin"}:
        raise HTTPException(400, "用户状态或角色无效")
    removing_last_admin = (
        target["role"] == "admin"
        and target["status"] == "active"
        and (body.role == "user" or body.status == "disabled")
        and state.store.count_active_admins() <= 1
    )
    if removing_last_admin:
        raise HTTPException(409, "不能移除最后一个可用管理员")
    password_hash = hash_password(body.temporary_password) if body.temporary_password else None
    state.store.update_user_admin(
        user_id, status=body.status, role=body.role, password_hash=password_hash,
        must_change_password=True if password_hash else None,
    )
    state.store.audit(admin["id"], "update_user", "user", user_id,
                      {"status": body.status, "role": body.role, "password_reset": bool(password_hash)})
    return {"ok": True}
