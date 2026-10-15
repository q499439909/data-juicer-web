from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agent_web.auth.dependencies import app_state, current_user
from agent_web.agent_runtime.runtime import RunAlreadyActive
from agent_web.db.store import ResourceNotFound
from agent_web.state import AppState

router = APIRouter(prefix="/api", tags=["workspace"])


class ConversationBody(BaseModel):
    title: str = "新会话"


class ConversationPatch(BaseModel):
    title: str | None = None
    hidden: bool | None = None


class MessageBody(BaseModel):
    message: str
    workspace_path: str | None = None


@router.get("/conversations")
def list_conversations(hidden: bool = False, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    return state.store.list_conversations(user["id"], include_hidden=hidden)


@router.post("/conversations")
def create_conversation(body: ConversationBody, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    return state.store.create_conversation(user["id"], body.title)


@router.patch("/conversations/{conversation_id}")
def patch_conversation(conversation_id: str, body: ConversationPatch,
                       user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        if body.title is not None:
            state.store.rename_conversation(user["id"], conversation_id, body.title)
        if body.hidden is not None:
            state.store.set_conversation_hidden(user["id"], conversation_id, body.hidden)
        return state.store.require_conversation(user["id"], conversation_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc


@router.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        return state.store.list_messages(user["id"], conversation_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc


@router.get("/conversations/{conversation_id}/plan")
def conversation_plan(conversation_id: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        conversation = state.store.require_conversation(user["id"], conversation_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    plan = json.loads(conversation["plan_json"]) if conversation.get("plan_json") else None
    if not plan:
        return {"plan": None, "recommendation": None}
    summary = state.agents.plan_summary(plan) or {}
    total_bytes, file_count = _plan_dataset_stats(state, user["id"], plan)
    recommendation = state.policy.recommend(
        operator_names=summary.get("operators", []), total_bytes=total_bytes, file_count=file_count
    )
    return {
        "plan": plan,
        "summary": summary,
        "data": {"total_bytes": total_bytes, "file_count": file_count},
        "recommendation": {
            "recommended": recommendation.recommended,
            "allowed": recommendation.allowed,
            "reason": recommendation.reason,
        },
    }


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, body: MessageBody,
                       user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    if not body.message.strip():
        raise HTTPException(400, "消息不能为空")
    try:
        state.store.require_conversation(user["id"], conversation_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc

    agent_message = body.message.strip()
    if body.workspace_path:
        try:
            dataset_path = state.workspaces.resolve_user_path(
                user["id"], body.workspace_path
            )
        except ValueError as exc:
            raise HTTPException(400, "所选数据路径不安全") from exc
        if not dataset_path.exists():
            raise HTTPException(400, "所选数据不存在")
        agent_message += (
            "\n\n当前用户明确选择的数据路径（必须保持用户隔离）："
            f"{dataset_path}"
        )

    try:
        run = await state.agents.start_turn(
            user["id"],
            conversation_id,
            body.message.strip(),
            agent_message=agent_message,
        )
    except RunAlreadyActive as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run_id": run["id"], "status": run["status"], "started_at": run["started_at"]}


@router.get("/conversations/{conversation_id}/active-run")
def active_run(conversation_id: str, user: dict = Depends(current_user),
               state: AppState = Depends(app_state)):
    try:
        state.store.require_conversation(user["id"], conversation_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    run = state.store.active_run(user["id"], conversation_id)
    if not run:
        return {"run": None}
    return {"run": {key: run.get(key) for key in ("id", "status", "started_at")}}


@router.get("/agent-runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0,
                     user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        state.store.require_run(user["id"], run_id)
    except ResourceNotFound as exc:
        raise HTTPException(404, "Agent 运行不存在") from exc

    async def stream():
        async for event in state.agents.stream_events(user["id"], run_id, after):
            if await request.is_disconnected():
                break
            yield (
                f"id: {event['sequence']}\n"
                "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
            )

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/workspace/files")
def workspace_files(path: str = "", user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        return {"path": path, "files": state.workspaces.list_files(user["id"], path)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/datasets/provider")
def dataset_provider(user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    if not state.dataset_provider.available:
        return {"available": False, "datasets": []}
    return {"available": True, "datasets": state.dataset_provider.list_for_user(user)}


@router.post("/workspace/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
    extract_archives: bool = Form(default=False),
    user: dict = Depends(current_user),
    state: AppState = Depends(app_state),
):
    saved = []
    for index, upload in enumerate(files):
        supplied = relative_paths[index] if index < len(relative_paths) else upload.filename
        relative = Path("uploads") / Path(str(supplied or upload.filename).replace("\\", "/"))
        try:
            target = state.workspaces.resolve_user_path(user["id"], relative)
        except ValueError as exc:
            raise HTTPException(400, "上传路径不安全") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                handle.write(chunk)
        workspace = state.workspaces.ensure_user_workspace(user["id"])
        rel = target.relative_to(workspace).as_posix()
        record = state.store.add_file(user["id"], "upload", rel, upload.filename or target.name, size)
        record["extracted_to"] = None
        if extract_archives:
            try:
                extracted = state.workspaces.extract_archive(user["id"], rel)
                record["extracted_to"] = extracted.relative_to(workspace).as_posix()
            except ValueError:
                pass
        saved.append(record)
    return saved


@router.get("/workspace/download")
def download_workspace_file(path: str, user: dict = Depends(current_user), state: AppState = Depends(app_state)):
    try:
        target = state.workspaces.resolve_user_path(user["id"], path)
    except ValueError as exc:
        raise HTTPException(404, "文件不存在") from exc
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=target.name)


def _plan_dataset_stats(state: AppState, owner_id: str, plan: dict) -> tuple[int, int]:
    recipe = plan.get("recipe") if isinstance(plan.get("recipe"), dict) else {}
    raw_path = str(recipe.get("dataset_path", ""))
    if not raw_path:
        return 0, 0
    try:
        path = Path(raw_path).resolve()
        workspace = state.workspaces.ensure_user_workspace(owner_id)
        if not path.is_relative_to(workspace) or not path.exists():
            return 0, 0
        if path.is_file():
            return path.stat().st_size, 1
        files = [item for item in path.rglob("*") if item.is_file()]
        return sum(item.stat().st_size for item in files), len(files)
    except OSError:
        return 0, 0
