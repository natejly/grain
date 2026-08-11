"""Filing: the folder tree, and which folder a document sits in.

Its own module rather than more of `api/artifacts.py` because a folder is not an
artifact — it holds no content and nothing reads it but the file browser. The
`PUT /api/documents/{id}/folder` route lives here too, since "where does this
file live" is one idea and splitting it across two modules is how the two halves
end up disagreeing about what an unknown folder id means.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Document, Folder
from ..schemas import (
    DocumentFolderRequest,
    DocumentOut,
    FolderOut,
    FolderRequest,
    FolderUpdateRequest,
)
from ..services.artifacts import folders

router = APIRouter(prefix="/api", tags=["folders"])


def _out(folder: Folder) -> FolderOut:
    return FolderOut(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        updated_at=folder.updated_at,
    )


def _document_out(document: Document) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        title=document.title,
        kind=document.kind,
        content=document.content,
        folder_id=document.folder_id,
        updated_at=document.updated_at,
    )


@router.get("/folders", response_model=List[FolderOut])
def list_folders(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[FolderOut]:
    return [_out(row) for row in folders.list_folders(db, workspace_id=actor.workspace_id)]


@router.post("/folders", response_model=FolderOut, status_code=201)
def create_folder(
    payload: FolderRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> FolderOut:
    try:
        folder = folders.create_folder(
            db,
            workspace_id=actor.workspace_id,
            name=payload.name,
            parent_id=payload.parent_id,
            created_by=actor.user_id,
        )
    except folders.FolderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(folder)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: str,
    payload: FolderUpdateRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> FolderOut:
    # Resolved first so "not yours" is a 404 like every other route that names a
    # folder, and the 422 below can only ever mean "that edit is not allowed".
    try:
        folders.get_folder(db, workspace_id=actor.workspace_id, folder_id=folder_id)
    except folders.FolderError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        folder = folders.update_folder(
            db,
            workspace_id=actor.workspace_id,
            folder_id=folder_id,
            name=payload.name,
            parent_id=payload.parent_id,
        )
    except folders.FolderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(folder)


@router.delete("/folders/{folder_id}", status_code=204)
def delete_folder(
    folder_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    try:
        folders.get_folder(db, workspace_id=actor.workspace_id, folder_id=folder_id)
    except folders.FolderError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        folders.delete_folder(
            db, workspace_id=actor.workspace_id, folder_id=folder_id
        )
    except folders.FolderError as exc:
        # 409, not 422: the request is well-formed and would be accepted the
        # moment the folder is empty. The client shows the sentence and offers
        # the way through, which is not what it does with a validation error.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/documents/{document_id}/folder", response_model=DocumentOut)
def move_document(
    document_id: str,
    payload: DocumentFolderRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentOut:
    try:
        document = folders.move_document(
            db,
            workspace_id=actor.workspace_id,
            document_id=document_id,
            folder_id=payload.folder_id,
        )
    except folders.FolderError as exc:
        # Both halves — an unknown document and an unknown folder — are 404 on
        # purpose: to this caller they are the same fact, that one of the two
        # ids names nothing it can see.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _document_out(document)
