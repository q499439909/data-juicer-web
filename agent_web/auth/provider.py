"""Replaceable identity interface.

The built-in adapter uses the temporary username/password cookie.  A future
platform adapter only needs to implement ``authenticate`` and can leave all
conversation, task, and page code unchanged.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import Request

from agent_web.db.store import AppStore


class IdentityProvider(Protocol):
    def authenticate(self, request: Request, store: AppStore) -> dict | None: ...


class BuiltInCookieIdentityProvider:
    cookie_name = "dja_session"

    def authenticate(self, request: Request, store: AppStore) -> dict | None:
        return store.user_for_auth_session(request.cookies.get(self.cookie_name))
