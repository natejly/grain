from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Document
from ..schemas import (
    BoardCardRequest,
    BoardOut,
    BoardRequest,
    DocumentContentRequest,
    DocumentOut,
    DocumentRequest,
    DocumentSummaryOut,
    DocumentVersionOut,
)
from ..services import conversations, subjects
from ..services.artifacts import boards, documents

router = APIRouter(prefix="/api", tags=["artifacts"])


def _document_out(document: Document) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        title=document.title,
        kind=document.kind,
        content=document.content,
        folder_id=document.folder_id,
        updated_at=document.updated_at,
    )


def _summary_out(document: Document) -> DocumentSummaryOut:
    return DocumentSummaryOut(
        id=document.id,
        title=document.title,
        kind=document.kind,
        characters=len(document.content),
        folder_id=document.folder_id,
        updated_at=document.updated_at,
    )


@router.get("/documents", response_model=List[DocumentSummaryOut])
def list_documents(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DocumentSummaryOut]:
    return [
        _summary_out(row)
        for row in documents.list_documents(db, workspace_id=actor.workspace_id)
    ]


@router.post("/documents", response_model=DocumentOut, status_code=201)
def create_document(
    payload: DocumentRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentOut:
    try:
        document = documents.create_document(
            db,
            workspace_id=actor.workspace_id,
            title=payload.title,
            content=payload.content,
            kind=payload.kind,
            folder_id=payload.folder_id,
            created_by=actor.user_id,
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _document_out(document)


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentOut:
    try:
        document = documents.get_document(
            db, workspace_id=actor.workspace_id, document_id=document_id
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _document_out(document)


@router.put("/documents/{document_id}", response_model=DocumentOut)
def update_document(
    document_id: str,
    payload: DocumentContentRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentOut:
    # Resolve first, so "that document is not yours" answers 404 like every
    # other /documents route rather than 422. `replace_content` raises the same
    # DocumentError type for a missing document and for content over the limit,
    # and those are not the same answer.
    try:
        documents.get_document(
            db, workspace_id=actor.workspace_id, document_id=document_id
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        document = documents.replace_content(
            db,
            workspace_id=actor.workspace_id,
            document_id=document_id,
            content=payload.content,
            created_by=actor.user_id,
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _document_out(document)


@router.get("/documents/{document_id}/versions", response_model=List[DocumentVersionOut])
def list_versions(
    document_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DocumentVersionOut]:
    # Resolve the document first. `list_versions` is workspace-filtered, so this
    # was never a leak — but without it a document in someone else's workspace
    # answered 200 [] while every other /documents route answered 404, and
    # "empty" versus "not yours" is a distinction the caller should not get.
    try:
        documents.get_document(
            db, workspace_id=actor.workspace_id, document_id=document_id
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        DocumentVersionOut(
            id=row.id, summary=row.summary, created_at=row.created_at
        )
        for row in documents.list_versions(
            db, workspace_id=actor.workspace_id, document_id=document_id
        )
    ]


@router.post(
    "/documents/{document_id}/versions/{version_id}/restore", response_model=DocumentOut
)
def restore_version(
    document_id: str,
    version_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentOut:
    try:
        document = documents.restore_version(
            db,
            workspace_id=actor.workspace_id,
            document_id=document_id,
            version_id=version_id,
            created_by=actor.user_id,
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _document_out(document)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    # The side-panel thread goes with it. It was only ever about this document —
    # every turn was handed the document's text — so leaving it behind would
    # leave a conversation whose subject no longer exists, invisible in the Chat
    # rail (which filters scoped threads out) and reachable by nothing.
    conversations.purge_for_subject(
        db,
        workspace_id=actor.workspace_id,
        subject_kind=subjects.DOCUMENT,
        subject_id=document_id,
    )
    try:
        documents.delete_document(
            db, workspace_id=actor.workspace_id, document_id=document_id
        )
    except documents.DocumentError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Boards


def _board_out(db: Session, board: Any) -> BoardOut:
    return BoardOut.model_validate(boards.snapshot(db, board))


@router.get("/boards", response_model=List[BoardOut])
def list_boards(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[BoardOut]:
    return [
        _board_out(db, board)
        for board in boards.list_boards(db, workspace_id=actor.workspace_id)
    ]


@router.post("/boards", response_model=BoardOut, status_code=201)
def create_board(
    payload: BoardRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    try:
        board = boards.create_board(
            db,
            workspace_id=actor.workspace_id,
            name=payload.name,
            columns=payload.columns or None,
            created_by=actor.user_id,
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


def _load_board(db: Session, actor: Actor, board_id: str):
    try:
        return boards.get_board(
            db, workspace_id=actor.workspace_id, board_id=board_id
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/boards/{board_id}/cards", response_model=BoardOut, status_code=201)
def add_card(
    board_id: str,
    payload: BoardCardRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        boards.add_card(
            db,
            workspace_id=actor.workspace_id,
            board=board,
            column=payload.column,
            title=payload.title,
            body=payload.body,
            labels=payload.labels,
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


@router.post("/boards/{board_id}/cards/{card_id}/move", response_model=BoardOut)
def move_card(
    board_id: str,
    card_id: str,
    column: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        boards.move_card(db, board=board, card=card_id, column=column)
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


@router.delete("/boards/{board_id}/cards/{card_id}", response_model=BoardOut)
def delete_card(
    board_id: str,
    card_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        boards.delete_card(db, board=board, card=card_id)
    except boards.BoardError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _board_out(db, board)


@router.delete("/boards/{board_id}", status_code=204)
def delete_board(
    board_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    _load_board(db, actor, board_id)
    boards.delete_board(db, workspace_id=actor.workspace_id, board_id=board_id)
