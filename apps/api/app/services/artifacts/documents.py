"""Documents the agent can author and revise, with diffs and undo."""
from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Document, DocumentVersion

MAX_DOCUMENT_CHARS = 400_000
DIFF_CONTEXT_LINES = 3
KINDS = {"markdown", "latex"}


class DocumentError(ValueError):
    """A user- and model-facing problem with a document operation."""


def render_diff(before: str, after: str, *, title: str = "document") -> str:
    """A unified diff of two document bodies, or a note that nothing changed."""
    if before == after:
        return "(no change)"
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"{title} (current)",
        tofile=f"{title} (proposed)",
        lineterm="",
        n=DIFF_CONTEXT_LINES,
    )
    return "\n".join(lines)


def get_document(db: Session, *, workspace_id: str, document_id: str) -> Document:
    document = db.scalar(
        select(Document).where(
            Document.id == document_id, Document.workspace_id == workspace_id
        )
    )
    if document is None:
        raise DocumentError("No document with that id")
    return document


def find_by_title(
    db: Session, *, workspace_id: str, title: str
) -> Optional[Document]:
    """Case-insensitive title lookup, so the model can refer to a doc by name."""
    wanted = title.strip().lower()
    for document in db.scalars(
        select(Document).where(Document.workspace_id == workspace_id)
    ):
        if document.title.strip().lower() == wanted:
            return document
    return None


def resolve(
    db: Session, *, workspace_id: str, document_id: str = "", title: str = ""
) -> Document:
    """Accept either an id or a title, since models reliably remember only one."""
    if document_id:
        return get_document(db, workspace_id=workspace_id, document_id=document_id)
    if title:
        found = find_by_title(db, workspace_id=workspace_id, title=title)
        if found is None:
            raise DocumentError(f"No document titled “{title}”")
        return found
    raise DocumentError("Provide document_id or title")


def list_documents(db: Session, *, workspace_id: str) -> List[Document]:
    return list(
        db.scalars(
            select(Document)
            .where(Document.workspace_id == workspace_id)
            .order_by(Document.updated_at.desc())
        )
    )


def create_document(
    db: Session,
    *,
    workspace_id: str,
    title: str,
    content: str,
    kind: str = "markdown",
    created_by: str = "",
) -> Document:
    title = title.strip()
    if not title:
        raise DocumentError("A document needs a title")
    if kind not in KINDS:
        raise DocumentError(f"kind must be one of {sorted(KINDS)}")
    if len(content) > MAX_DOCUMENT_CHARS:
        raise DocumentError(
            f"Document exceeds the {MAX_DOCUMENT_CHARS:,}-character limit"
        )
    if find_by_title(db, workspace_id=workspace_id, title=title) is not None:
        raise DocumentError(f"A document titled “{title}” already exists")
    document = Document(
        workspace_id=workspace_id,
        title=title[:200],
        kind=kind,
        content=content,
        created_by=created_by,
    )
    db.add(document)
    db.commit()
    return document


def apply_replacement(
    content: str, find: str, replace: str, *, replace_all: bool
) -> Tuple[str, int]:
    """Exact-string replacement, refusing ambiguous single-target edits.

    Mirrors how a code editor applies a patch: a `find` that matches twice is a
    mistake unless the caller said it meant all of them.
    """
    if not find:
        raise DocumentError("find must not be empty")
    occurrences = content.count(find)
    if occurrences == 0:
        raise DocumentError("find text does not appear in the document")
    if occurrences > 1 and not replace_all:
        raise DocumentError(
            f"find text appears {occurrences} times; include more surrounding "
            "text to make it unique, or set replace_all"
        )
    if replace_all:
        return content.replace(find, replace), occurrences
    return content.replace(find, replace, 1), 1


def preview_edit(
    db: Session,
    *,
    workspace_id: str,
    document_id: str = "",
    title: str = "",
    find: str,
    replace: str,
    replace_all: bool = False,
) -> str:
    document = resolve(
        db, workspace_id=workspace_id, document_id=document_id, title=title
    )
    updated, _count = apply_replacement(
        document.content, find, replace, replace_all=replace_all
    )
    return render_diff(document.content, updated, title=document.title)


def edit_document(
    db: Session,
    *,
    workspace_id: str,
    document_id: str = "",
    title: str = "",
    find: str,
    replace: str,
    replace_all: bool = False,
    summary: str = "",
) -> Tuple[Document, str, int]:
    """Apply an edit, snapshotting the prior content first. Returns the diff."""
    document = resolve(
        db, workspace_id=workspace_id, document_id=document_id, title=title
    )
    updated, count = apply_replacement(
        document.content, find, replace, replace_all=replace_all
    )
    if len(updated) > MAX_DOCUMENT_CHARS:
        raise DocumentError(
            f"Edit would exceed the {MAX_DOCUMENT_CHARS:,}-character limit"
        )
    diff = render_diff(document.content, updated, title=document.title)
    db.add(
        DocumentVersion(
            workspace_id=workspace_id,
            document_id=document.id,
            content=document.content,
            summary=(summary or "Agent edit")[:300],
        )
    )
    document.content = updated
    db.commit()
    return document, diff, count


def replace_content(
    db: Session,
    *,
    workspace_id: str,
    document_id: str,
    content: str,
    summary: str = "Manual edit",
) -> Document:
    """Wholesale replacement, used by the editor pane rather than the agent."""
    if len(content) > MAX_DOCUMENT_CHARS:
        raise DocumentError(
            f"Document exceeds the {MAX_DOCUMENT_CHARS:,}-character limit"
        )
    document = get_document(db, workspace_id=workspace_id, document_id=document_id)
    if document.content == content:
        return document
    db.add(
        DocumentVersion(
            workspace_id=workspace_id,
            document_id=document.id,
            content=document.content,
            summary=summary[:300],
        )
    )
    document.content = content
    db.commit()
    return document


def list_versions(
    db: Session, *, workspace_id: str, document_id: str
) -> List[DocumentVersion]:
    return list(
        db.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.workspace_id == workspace_id,
                DocumentVersion.document_id == document_id,
            )
            .order_by(DocumentVersion.created_at.desc())
            .limit(50)
        )
    )


def restore_version(
    db: Session, *, workspace_id: str, document_id: str, version_id: str
) -> Document:
    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.workspace_id == workspace_id,
        )
    )
    if version is None:
        raise DocumentError("No such version")
    return replace_content(
        db,
        workspace_id=workspace_id,
        document_id=document_id,
        content=version.content,
        summary="Restored an earlier version",
    )


def delete_document(db: Session, *, workspace_id: str, document_id: str) -> None:
    document = get_document(db, workspace_id=workspace_id, document_id=document_id)
    for version in db.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document.id)
    ):
        db.delete(version)
    db.delete(document)
    db.commit()
