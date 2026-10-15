"""FastAPI entrypoint for Data-Juicer Agent Web."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent_web.config import Settings
from agent_web.routes import admin, auth, conversations, tasks
from agent_web.state import build_state


def create_app(settings: Settings | None = None) -> FastAPI:
    container = build_state(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        container.scheduler.start()
        try:
            yield
        finally:
            container.scheduler.stop()

    app = FastAPI(title="Data-Juicer Agent", version="0.2.0", lifespan=lifespan)
    app.state.container = container
    app.mount("/static", StaticFiles(directory=container.settings.root / "static"), name="static")
    templates = Jinja2Templates(directory=container.settings.root / "templates")

    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(tasks.router)
    app.include_router(admin.router)

    def page_user(request: Request):
        return container.identity_provider.authenticate(request, container.store)

    @app.get("/health")
    def health():
        return {"status": "ok", "scheduler": container.scheduler.status()}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if page_user(request):
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/", response_class=HTMLResponse)
    def workspace_page(request: Request):
        user = page_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse(request, "workspace.html", {"user": user, "page": "workspace"})

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request):
        user = page_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse(request, "tasks.html", {"user": user, "page": "tasks"})

    @app.get("/admin/runs", response_class=HTMLResponse)
    def admin_page(request: Request):
        user = page_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if user["role"] != "admin":
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "admin.html", {"user": user, "page": "admin"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
