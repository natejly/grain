from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api import (
    admin,
    agents,
    analytics,
    api_tokens,
    artifacts,
    audit,
    bibliography,
    board_ops,
    chat,
    comments,
    coworking,
    crons,
    dashboard_subscriptions,
    dashboards,
    dbconnect,
    doc_pending,
    favorites,
    folders,
    generated_apps,
    graph,
    hooks,
    inbound_email,
    inbox,
    integrations,
    latex,
    marketplace,
    mcp,
    mcp_server,
    me,
    memory,
    monitors,
    org,
    projects,
    sandbox,
    sandbox_secrets,
    sandbox_tools,
    share_links,
    skills,
    sources,
    spaces,
    system,
    templates,
    todos,
    tools,
    webhooks,
    workflows,
)
from .api.auth import router as auth_router
from .auth import seed_dev_workspace
from .config import get_settings
from .database import Base, SessionLocal, engine
from .services.recovery import recover_durable_work

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_dev_env:
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            # Seed rows only. Nothing here authenticates: the seeded user has no
            # password hash, and reaching it still needs a real session.
            seed_dev_workspace(db, settings)
        finally:
            db.close()
        asyncio.create_task(asyncio.to_thread(recover_durable_work))
    yield


app = FastAPI(
    title="Agentic Knowledge Workspace API",
    version="0.1.0",
    description=(
        "Durable chat, cited local knowledge, provenance, and permissioned read-only tools."
    ),
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_web_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_headers(request: Request, call_next):
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

app.include_router(auth_router)
app.include_router(system.router)
app.include_router(chat.router)
app.include_router(agents.router)
app.include_router(skills.router)
app.include_router(marketplace.router)
app.include_router(sources.router)
app.include_router(spaces.router)
app.include_router(tools.router)
app.include_router(audit.router)
app.include_router(graph.router)
app.include_router(memory.router)
app.include_router(integrations.router)
app.include_router(mcp.router)
app.include_router(dbconnect.router)
app.include_router(artifacts.router)
app.include_router(board_ops.router)
app.include_router(todos.router)
app.include_router(coworking.router)
app.include_router(doc_pending.router)
app.include_router(inbox.router)
app.include_router(me.router)
app.include_router(comments.router)
app.include_router(folders.router)
app.include_router(projects.router)
app.include_router(latex.router)
app.include_router(bibliography.router)
app.include_router(sandbox.router)
app.include_router(sandbox_secrets.router)
app.include_router(sandbox_tools.router)
app.include_router(analytics.router)
app.include_router(dashboards.router)
app.include_router(favorites.router)
app.include_router(dashboard_subscriptions.router)
app.include_router(share_links.router)
app.include_router(api_tokens.router)
app.include_router(webhooks.router)
app.include_router(hooks.router)
app.include_router(inbound_email.router)
app.include_router(generated_apps.router)
app.include_router(workflows.router)
app.include_router(templates.router)
app.include_router(crons.router)
app.include_router(monitors.router)
app.include_router(admin.router)
app.include_router(org.router)
app.include_router(mcp_server.router)
