from __future__ import annotations

import json
from typing import List, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor, require_owner
from ..clock import utcnow
from ..config import get_settings
from ..database import get_db
from ..models import AppRelease, GeneratedApp, new_id
from ..schemas import (
    AppGenerateRequest,
    AppReleaseCreate,
    AppReleaseOut,
    GeneratedAppCreate,
    GeneratedAppOut,
    PublishedAppOut,
)
from ..services.app_codegen import AppCodegenError, build_code_manifest
from ..services.audit import record_audit
from ..services.generated_apps import (
    GeneratedAppValidationError,
    build_release_manifest,
)
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(tags=["generated-apps"])


def _frame_response(html: str) -> Response:
    """Serve generated app code as a document with a no-network, no-nav CSP.

    The web app embeds this via <iframe sandbox="allow-scripts">, so the code
    runs in an opaque origin with no cookies and no parent access; this CSP
    removes its network access entirely.
    """
    frame_ancestors = " ".join(get_settings().allowed_web_origins) or "'none'"
    response = HTMLResponse(content=html)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src data:; connect-src 'none'; "
        "form-action 'none'; base-uri 'none'; "
        f"frame-ancestors {frame_ancestors}"
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _code_manifest_or_404(release: AppRelease) -> dict:
    manifest = json.loads(release.manifest_json)
    if manifest.get("kind") != "code" or not manifest.get("html"):
        raise HTTPException(status_code=404, detail="Release has no code frame")
    return manifest


def _release_out(release: AppRelease) -> AppReleaseOut:
    return AppReleaseOut(
        id=release.id,
        version=release.version,
        status=release.status,
        content_hash=release.content_hash,
        manifest=json.loads(release.manifest_json),
        created_at=release.created_at,
        published_at=release.published_at,
    )


def _app_out(db: Session, app: GeneratedApp) -> GeneratedAppOut:
    releases = list(
        db.scalars(
            select(AppRelease)
            .where(
                AppRelease.app_id == app.id,
                AppRelease.workspace_id == app.workspace_id,
            )
            .order_by(AppRelease.version.desc())
        )
    )
    return GeneratedAppOut(
        id=app.id,
        name=app.name,
        slug=app.slug,
        description=app.description,
        visibility=cast(Literal["private", "public"], app.visibility),
        app_type=app.app_type,
        current_release_id=app.current_release_id,
        releases=[_release_out(release) for release in releases],
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


def _workspace_app(db: Session, actor: Actor, app_id: str) -> GeneratedApp:
    app = db.scalar(
        select(GeneratedApp).where(
            GeneratedApp.id == app_id,
            GeneratedApp.workspace_id == actor.workspace_id,
        )
    )
    if app is None:
        raise HTTPException(status_code=404, detail="App not found")
    return app


@router.get("/api/apps", response_model=List[GeneratedAppOut])
def list_apps(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[GeneratedAppOut]:
    apps = db.scalars(
        select(GeneratedApp)
        .where(GeneratedApp.workspace_id == actor.workspace_id)
        .order_by(GeneratedApp.updated_at.desc())
    )
    return [_app_out(db, app) for app in apps]


@router.post("/api/apps", response_model=GeneratedAppOut, status_code=201)
def create_app(
    payload: GeneratedAppCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> GeneratedAppOut:
    if payload.visibility == "public" and actor.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required for public apps")
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="app.create",
        key=key,
    )
    if replay:
        replayed = db.scalar(
            select(GeneratedApp).where(
                GeneratedApp.id == replay.resource_id,
                GeneratedApp.workspace_id == actor.workspace_id,
            )
        )
        if replayed is None:
            raise replayed_resource_gone()
        return _app_out(db, replayed)
    slug = payload.slug.strip().lower()
    existing = db.scalar(
        select(GeneratedApp).where(
            GeneratedApp.slug == slug,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="App slug already exists")
    app = GeneratedApp(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=payload.name.strip(),
        slug=slug,
        description=payload.description.strip(),
        visibility=payload.visibility,
        app_type=payload.app_type,
    )
    release = None
    if payload.app_type == "dashboard":
        try:
            manifest, content_hash = build_release_manifest(
                db,
                workspace_id=actor.workspace_id,
                dashboard_ids=payload.dashboard_ids,
            )
        except GeneratedAppValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        release = AppRelease(
            id=new_id(),
            workspace_id=actor.workspace_id,
            app_id=app.id,
            created_by=actor.user_id,
            version=1,
            status="draft",
            manifest_json=json.dumps(manifest, ensure_ascii=False),
            content_hash=content_hash,
        )
    db.add(app)
    if release is not None:
        db.add(release)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="app.create",
        key=key,
        resource_id=app.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="app.created",
        resource_type="generated_app",
        resource_id=app.id,
        detail={
            "slug": app.slug,
            "visibility": app.visibility,
            "app_type": app.app_type,
            "release_id": release.id if release else "",
        },
    )
    db.commit()
    db.refresh(app)
    return _app_out(db, app)


@router.post(
    "/api/apps/{app_id}/releases",
    response_model=GeneratedAppOut,
    status_code=201,
)
def create_release(
    app_id: str,
    payload: AppReleaseCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> GeneratedAppOut:
    app = _workspace_app(db, actor, app_id)
    operation = f"app.release:{app.id}"
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
    )
    if replay:
        return _app_out(db, app)
    latest = db.scalar(
        select(AppRelease)
        .where(
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == actor.workspace_id,
        )
        .order_by(AppRelease.version.desc())
        .limit(1)
    )
    try:
        manifest, content_hash = build_release_manifest(
            db,
            workspace_id=actor.workspace_id,
            dashboard_ids=payload.dashboard_ids,
        )
    except GeneratedAppValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    release = AppRelease(
        id=new_id(),
        workspace_id=actor.workspace_id,
        app_id=app.id,
        created_by=actor.user_id,
        version=(latest.version + 1) if latest else 1,
        status="draft",
        manifest_json=json.dumps(manifest, ensure_ascii=False),
        content_hash=content_hash,
    )
    db.add(release)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
        resource_id=release.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="app.release_created",
        resource_type="generated_app",
        resource_id=app.id,
        detail={"version": release.version, "release_id": release.id},
    )
    db.commit()
    return _app_out(db, app)


def _activate_release(
    *,
    app: GeneratedApp,
    release: AppRelease,
    actor: Actor,
    db: Session,
    action: str,
) -> None:
    if app.current_release_id and app.current_release_id != release.id:
        current = db.scalar(
            select(AppRelease).where(
                AppRelease.id == app.current_release_id,
                AppRelease.workspace_id == actor.workspace_id,
            )
        )
        if current is not None:
            current.status = "superseded"
    release.status = "published"
    if release.published_at is None:
        release.published_at = utcnow()
    app.current_release_id = release.id
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action=action,
        resource_type="generated_app",
        resource_id=app.id,
        detail={"version": release.version, "release_id": release.id},
    )


@router.post(
    "/api/apps/{app_id}/releases/{release_id}/publish",
    response_model=GeneratedAppOut,
)
def publish_release(
    app_id: str,
    release_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> GeneratedAppOut:
    app = _workspace_app(db, actor, app_id)
    release = db.scalar(
        select(AppRelease).where(
            AppRelease.id == release_id,
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == actor.workspace_id,
        )
    )
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    operation = f"app.publish:{app.id}"
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
    )
    if replay:
        return _app_out(db, app)
    _activate_release(
        app=app,
        release=release,
        actor=actor,
        db=db,
        action="app.published",
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
        resource_id=release.id,
    )
    db.commit()
    return _app_out(db, app)


@router.post(
    "/api/apps/{app_id}/rollback/{release_id}",
    response_model=GeneratedAppOut,
)
def rollback_release(
    app_id: str,
    release_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> GeneratedAppOut:
    app = _workspace_app(db, actor, app_id)
    release = db.scalar(
        select(AppRelease).where(
            AppRelease.id == release_id,
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == actor.workspace_id,
        )
    )
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    operation = f"app.rollback:{app.id}"
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
    )
    if replay:
        return _app_out(db, app)
    _activate_release(
        app=app,
        release=release,
        actor=actor,
        db=db,
        action="app.rolled_back",
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
        resource_id=release.id,
    )
    db.commit()
    return _app_out(db, app)


@router.post(
    "/api/apps/{app_id}/generate",
    response_model=GeneratedAppOut,
    status_code=201,
)
def generate_app_release(
    app_id: str,
    payload: AppGenerateRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> GeneratedAppOut:
    app = _workspace_app(db, actor, app_id)
    if app.app_type != "code":
        raise HTTPException(status_code=422, detail="App is not a coded app")
    operation = f"app.generate:{app.id}"
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
    )
    if replay:
        return _app_out(db, app)
    latest = db.scalar(
        select(AppRelease)
        .where(
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == actor.workspace_id,
        )
        .order_by(AppRelease.version.desc())
        .limit(1)
    )
    previous_html = None
    if latest is not None:
        previous_manifest = json.loads(latest.manifest_json)
        if previous_manifest.get("kind") == "code":
            previous_html = previous_manifest.get("html")
    try:
        manifest, content_hash = build_code_manifest(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            app_name=app.name,
            prompt=payload.prompt,
            dataset_ids=payload.dataset_ids,
            previous_html=previous_html,
        )
    except AppCodegenError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    release = AppRelease(
        id=new_id(),
        workspace_id=actor.workspace_id,
        app_id=app.id,
        created_by=actor.user_id,
        version=(latest.version + 1) if latest else 1,
        status="draft",
        manifest_json=json.dumps(manifest, ensure_ascii=False),
        content_hash=content_hash,
    )
    db.add(release)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation=operation,
        key=key,
        resource_id=release.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="app.code_generated",
        resource_type="generated_app",
        resource_id=app.id,
        detail={"version": release.version, "release_id": release.id},
    )
    db.commit()
    return _app_out(db, app)


@router.get("/api/apps/{app_id}/releases/{release_id}/frame", response_class=HTMLResponse)
def app_release_frame(
    app_id: str,
    release_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Response:
    app = _workspace_app(db, actor, app_id)
    release = db.scalar(
        select(AppRelease).where(
            AppRelease.id == release_id,
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == actor.workspace_id,
        )
    )
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    manifest = _code_manifest_or_404(release)
    return _frame_response(manifest["html"])


@router.get("/published/apps/{slug}/frame", response_class=HTMLResponse)
def published_app_frame(
    slug: str,
    db: Session = Depends(get_db),
) -> Response:
    app = db.scalar(
        select(GeneratedApp).where(
            GeneratedApp.slug == slug,
            GeneratedApp.visibility == "public",
            GeneratedApp.current_release_id.is_not(None),
        )
    )
    if app is None or app.current_release_id is None:
        raise HTTPException(status_code=404, detail="Published app not found")
    release = db.scalar(
        select(AppRelease).where(
            AppRelease.id == app.current_release_id,
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == app.workspace_id,
            AppRelease.status == "published",
        )
    )
    if release is None:
        raise HTTPException(status_code=404, detail="Published app not found")
    manifest = _code_manifest_or_404(release)
    return _frame_response(manifest["html"])


@router.get("/published/apps/{slug}", response_model=PublishedAppOut)
def published_app(
    slug: str,
    db: Session = Depends(get_db),
) -> PublishedAppOut:
    app = db.scalar(
        select(GeneratedApp).where(
            GeneratedApp.slug == slug,
            GeneratedApp.visibility == "public",
            GeneratedApp.current_release_id.is_not(None),
        )
    )
    if app is None or app.current_release_id is None:
        raise HTTPException(status_code=404, detail="Published app not found")
    release = db.scalar(
        select(AppRelease).where(
            AppRelease.id == app.current_release_id,
            AppRelease.app_id == app.id,
            AppRelease.workspace_id == app.workspace_id,
            AppRelease.status == "published",
        )
    )
    if release is None or release.published_at is None:
        raise HTTPException(status_code=404, detail="Published app not found")
    return PublishedAppOut(
        name=app.name,
        slug=app.slug,
        description=app.description,
        version=release.version,
        published_at=release.published_at,
        manifest=json.loads(release.manifest_json),
    )
