"""REST surface over the project sandbox filesystem.

Nothing here executes project code — the browser compiles these files with
esbuild-wasm and renders them in a no-network iframe, so the API's whole job is
handing over content the bundler can resolve.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Project, ProjectFile
from ..schemas import ApiModel
from ..services.projects import store

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectFileOut(ApiModel):
    path: str
    content: str
    bytes: int


class ProjectSummaryOut(ApiModel):
    id: str
    name: str
    description: str
    kind: str
    entry_path: str
    file_count: int
    total_bytes: int
    updated_at: datetime


class ProjectOut(ApiModel):
    id: str
    name: str
    description: str
    kind: str
    entry_path: str
    files: List[ProjectFileOut]
    total_bytes: int
    updated_at: datetime


class ProjectRequest(BaseModel):
    name: str
    description: str = ""
    # "web" bundles with esbuild; "latex" compiles with the TeX engine. The
    # entry path is left empty by default so each kind gets its own.
    kind: Literal["web", "latex"] = "web"
    entry_path: str = ""


class ProjectFileRequest(BaseModel):
    path: str
    content: str = Field(default="")


def _file_out(file: ProjectFile) -> ProjectFileOut:
    return ProjectFileOut(
        path=file.path, content=file.content, bytes=store.byte_size(file.content)
    )


def _summary_out(db: Session, project: Project) -> ProjectSummaryOut:
    files = store.list_files(db, project_id=project.id)
    return ProjectSummaryOut(
        id=project.id,
        name=project.name,
        description=project.description,
        kind=project.kind,
        entry_path=project.entry_path,
        file_count=len(files),
        total_bytes=sum(store.byte_size(file.content) for file in files),
        updated_at=project.updated_at,
    )


def _project_out(db: Session, project: Project) -> ProjectOut:
    files = [_file_out(file) for file in store.list_files(db, project_id=project.id)]
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        kind=project.kind,
        entry_path=project.entry_path,
        files=files,
        total_bytes=sum(file.bytes for file in files),
        updated_at=project.updated_at,
    )


def _load(db: Session, actor: Actor, project_id: str) -> Project:
    try:
        return store.get_project(
            db, workspace_id=actor.workspace_id, project_id=project_id
        )
    except store.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("", response_model=List[ProjectSummaryOut])
def list_projects(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ProjectSummaryOut]:
    return [
        _summary_out(db, project)
        for project in store.list_projects(db, workspace_id=actor.workspace_id)
    ]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ProjectOut:
    try:
        project = store.create_project(
            db,
            workspace_id=actor.workspace_id,
            name=payload.name,
            description=payload.description,
            kind=payload.kind,
            entry_path=payload.entry_path,
            created_by=actor.user_id,
        )
    except store.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _project_out(db, project)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ProjectOut:
    return _project_out(db, _load(db, actor, project_id))


@router.put("/{project_id}/files", response_model=ProjectFileOut)
def write_project_file(
    project_id: str,
    payload: ProjectFileRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ProjectFileOut:
    """Create or overwrite one file. Paths carry slashes, so they ride in the body."""
    project = _load(db, actor, project_id)
    try:
        file, _created = store.write_file(
            db,
            workspace_id=actor.workspace_id,
            project=project,
            path=payload.path,
            content=payload.content,
        )
    except store.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _file_out(file)


@router.delete("/{project_id}/files", status_code=204)
def delete_project_file(
    project_id: str,
    path: str = Query(..., description="Project-relative file path"),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    project = _load(db, actor, project_id)
    try:
        clean = store.normalize_path(path)
    except store.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if store.find_file(db, project_id=project.id, path=clean) is None:
        raise HTTPException(status_code=404, detail=f"{clean} is not in {project.name}")
    try:
        store.delete_file(db, project=project, path=clean)
    except store.ProjectError as exc:
        # What's left is the entry file, which the store refuses to remove.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    project = _load(db, actor, project_id)
    store.delete_project(db, workspace_id=actor.workspace_id, project_id=project.id)
