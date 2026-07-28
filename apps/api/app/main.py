from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api import (
    analytics,
    audit,
    chat,
    generated_apps,
    graph,
    integrations,
    memory,
    sources,
    system,
    tools,
)
from .auth import ensure_development_identity
from .config import get_settings
from .database import Base, SessionLocal, engine
from .services.recovery import recover_durable_work

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_settings().app_env == "development":
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            ensure_development_identity(db)
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

app.include_router(system.router)
app.include_router(chat.router)
app.include_router(sources.router)
app.include_router(tools.router)
app.include_router(audit.router)
app.include_router(graph.router)
app.include_router(memory.router)
app.include_router(integrations.router)
app.include_router(analytics.router)
app.include_router(generated_apps.router)
