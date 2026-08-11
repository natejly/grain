"""Reading a parked document write the way the reviewer will see it.

Two callers need the same three answers about an `AgentToolCall` sitting at
`status="proposed"`: which document it would change, and — for an edit — what
the document looks like with the proposal laid over it, hunk by hunk.
`GET /api/documents-pending` needs them to render the review; the decision
endpoint needs them to check that the hunks a reviewer accepted are the hunks
they were shown. Sharing the resolution is what keeps those two honest about
each other: an index means the same change on both sides or neither.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import Document
from . import documents


def arguments_of(raw: str) -> Dict[str, Any]:
    """Model-generated JSON. Truncation and hallucinated shapes are routine, so
    anything that is not an object decodes to no arguments at all."""
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def text_of(args: Dict[str, Any], key: str) -> str:
    """A trimmed identifier — an id or a title, where surrounding space is noise."""
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def raw_of(args: Dict[str, Any], key: str) -> str:
    """A literal, kept byte for byte.

    `find` and `replace` are exact strings: trimming one turns a whitespace-only
    edit into a no-op and, worse, makes the review show a change the executor
    would not make.
    """
    value = args.get(key)
    return value if isinstance(value, str) else ""


def target_document(
    db: Session,
    *,
    workspace_id: str,
    name: str,
    args: Dict[str, Any],
    open_document_id: str = "",
) -> Optional[Document]:
    """The document this call would change, if it exists in this workspace.

    `open_document_id` mirrors the fallback the executor applies: a turn started
    from the chat panel beside a document lets the model edit it without naming
    it, so a proposal with neither id nor title is about that document and the
    review has to resolve it the same way or the card points at nothing.
    """
    if name == "create_document":
        return None
    document_id = text_of(args, "document_id")
    title = text_of(args, "title")
    if not document_id and not title:
        document_id = open_document_id
    try:
        return documents.resolve(
            db, workspace_id=workspace_id, document_id=document_id, title=title
        )
    except documents.DocumentError:
        return None


def review_segments(
    document: Optional[Document], args: Dict[str, Any]
) -> List[documents.Segment]:
    """The open document covered by what this edit would and would not touch.

    Empty when there is nothing reviewable hunk by hunk: a create, an
    unresolvable target, or a `find` that no longer appears — the last of which
    is the interesting one, because it means the proposal has gone stale and the
    all-or-nothing card, which says why, is the honest thing to show instead.
    """
    if document is None:
        return []
    try:
        updated, _count = documents.apply_replacement(
            document.content,
            raw_of(args, "find"),
            raw_of(args, "replace"),
            replace_all=bool(args.get("replace_all")),
        )
    except documents.DocumentError:
        return []
    return documents.segment_revision(document.content, updated)
