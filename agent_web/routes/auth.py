from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agent_web.auth.dependencies import COOKIE_NAME, app_state, current_user
from agent_web.auth.passwords import hash_password, verify_password
from agent_web.state import AppState

router = APIRouter(prefix="/api/auth", tags=["auth"])
_USERNAME = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


class RegisterBody(BaseModel):
    username: str
    display_name: str
    password: str = Field(min_length=8, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str = Field(min_length=1, max_length=128)


def _public_user(user: dict) -> dict:
    return {key: user[key] for key in ("id", "username", "display_name", "role", "status", "must_change_password")}


@router.post("/register")
def register(body: RegisterBody, response: Response, state: AppState = Depends(app_state)):
    if not _USERNAME.fullmatch(body.username.strip()):
        raise HTTPException(400, "用户名需为 3～32 位字母、数字、点、横线或下划线")
    if not body.display_name.strip():
        raise HTTPException(400, "请输入显示名称")
    try:
        user = state.store.create_user(
            body.username.strip(), body.display_name.strip(), hash_password(body.password)
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "用户名已存在") from exc
    token = state.store.create_auth_session(user["id"])
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=state.settings.cookie_secure,
        samesite="lax", max_age=7 * 24 * 3600, path="/",
    )
    state.workspaces.ensure_user_workspace(user["id"])
    return _public_user(user)


@router.post("/login")
def login(body: LoginBody, response: Response, state: AppState = Depends(app_state)):
    user = state.store.find_user_by_username(body.username)
    if user is None or user["status"] != "active" or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    token = state.store.create_auth_session(user["id"])
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=state.settings.cookie_secure,
        samesite="lax", max_age=7 * 24 * 3600, path="/",
    )
    return _public_user(user)


@router.post("/logout")
def logout(request: Request, response: Response, state: AppState = Depends(app_state)):
    state.store.delete_auth_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return _public_user(user)
