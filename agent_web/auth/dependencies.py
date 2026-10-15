"""FastAPI authentication and administrator authorization."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from agent_web.state import AppState

COOKIE_NAME = "dja_session"


def app_state(request: Request) -> AppState:
    return request.app.state.container


def current_user(request: Request) -> dict:
    state = app_state(request)
    user = state.identity_provider.authenticate(request, state.store)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def admin_user(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user
