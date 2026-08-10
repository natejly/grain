"""REST surface over a LaTeX project's bibliography.

The editor needs three things the .bib file alone cannot give it: the entries as
structured records, the cross-reference against the document's citations, and a
way to append an entry without hand-writing BibTeX. All three read and write the
same ProjectFile rows the rest of the project filesystem uses, so path validation,
the size limits and workspace scoping come from the store rather than from here.
"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Project
from ..schemas import ApiModel
from ..services.projects import bibliography, store

router = APIRouter(prefix="/api/bibliography", tags=["bibliography"])


class BibEntryOut(ApiModel):
    key: str
    entry_type: str
    fields: Dict[str, str]
    defined_in: str
    line: int
    truncated: bool


class BibliographyOut(ApiModel):
    project_id: str
    bib_files: List[str]
    entries: List[BibEntryOut]
    problems: List[str]


class UndefinedOut(ApiModel):
    key: str
    cited_in: List[str]


class DefinedOut(ApiModel):
    key: str
    defined_in: str


class DuplicateOut(ApiModel):
    key: str
    defined_in: List[str]
    count: int


class IncompleteOut(ApiModel):
    key: str
    entry_type: str
    defined_in: str
    missing: List[str]


class ValidationOut(ApiModel):
    project_id: str
    ok: bool
    bib_files: List[str]
    tex_files: List[str]
    entry_count: int
    # Cited with no entry: bibtex emits a warning and the reference typesets as
    # [?], so this is the list that has to be empty for the PDF to be right.
    cited_but_undefined: List[UndefinedOut]
    defined_but_uncited: List[DefinedOut]
    duplicate_keys: List[DuplicateOut]
    missing_required_fields: List[IncompleteOut]
    problems: List[str]


class AddEntryRequest(BaseModel):
    entry_type: str
    key: str
    fields: Dict[str, str] = Field(default_factory=dict)
    # Optional: a project with one .bib does not need to name it.
    path: str = ""


class AddEntryOut(ApiModel):
    path: str
    key: str
    entry: str
    created: bool


def _load(db: Session, actor: Actor, project_id: str) -> Project:
    try:
        return store.get_project(
            db, workspace_id=actor.workspace_id, project_id=project_id
        )
    except store.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{project_id}/entries", response_model=BibliographyOut)
def list_entries(
    project_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BibliographyOut:
    project = _load(db, actor, project_id)
    try:
        entries, paths, problems = bibliography.list_entries(db, project=project)
    except store.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BibliographyOut(
        project_id=project.id,
        bib_files=paths,
        entries=[BibEntryOut(**bibliography.entry_payload(entry)) for entry in entries],
        problems=problems,
    )


@router.get("/{project_id}/validate", response_model=ValidationOut)
def validate(
    project_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ValidationOut:
    project = _load(db, actor, project_id)
    try:
        report = bibliography.validate_project(db, project=project)
    except store.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ValidationOut(
        project_id=report.project_id,
        ok=report.ok,
        bib_files=report.bib_files,
        tex_files=report.tex_files,
        entry_count=len(report.entries),
        cited_but_undefined=[UndefinedOut.model_validate(item) for item in report.undefined],
        defined_but_uncited=[DefinedOut.model_validate(item) for item in report.uncited],
        duplicate_keys=[DuplicateOut.model_validate(item) for item in report.duplicates],
        missing_required_fields=[
            IncompleteOut.model_validate(item) for item in report.incomplete
        ],
        problems=report.problems,
    )


@router.post("/{project_id}/entries", response_model=AddEntryOut, status_code=201)
def add_entry(
    project_id: str,
    payload: AddEntryRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AddEntryOut:
    project = _load(db, actor, project_id)
    try:
        _file, plan = bibliography.add_entry(
            db,
            workspace_id=actor.workspace_id,
            project=project,
            entry_type=payload.entry_type,
            key=payload.key,
            fields=payload.fields,
            path=payload.path,
        )
    except store.ProjectError as exc:
        # A duplicate key, a non-LaTeX project, an unusable field — all of them
        # are the caller's input, none of them are a server fault.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AddEntryOut(
        path=plan.path, key=plan.key, entry=plan.entry, created=plan.created
    )
