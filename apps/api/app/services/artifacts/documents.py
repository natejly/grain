"""Documents the agent can author and revise, with diffs and undo."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Document, DocumentVersion

MAX_DOCUMENT_CHARS = 400_000
DIFF_CONTEXT_LINES = 3

# The two things a document can be. "text" is shown verbatim in a monospace
# pane; "markdown" is rendered, including $…$ and $$…$$ maths via KaTeX.
#
# "latex" used to be a third value and was never a third format: it rendered
# exactly like markdown and compiled nothing, so users who picked it expecting a
# PDF reported the TeX compiler as broken. Real LaTeX is a *project* kind, which
# does compile. Migration 0026 rewrote every stored "latex" document to
# "markdown" — they were already that in every respect.
KINDS = {"text", "markdown"}


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


# --------------------------------------------------------------------------
# Reviewing a proposal one change at a time


@dataclass(frozen=True)
class Segment:
    """One stretch of a proposed revision, as a reviewer meets it.

    The whole document is covered by segments in order, so a client can render
    the text and the proposal as one thing rather than as a document and a
    separate diff card. `index` is -1 for an untouched stretch and otherwise the
    hunk's number — the number a reviewer accepts or rejects by.
    """

    index: int
    before: List[str] = field(default_factory=list)
    after: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.index >= 0


def _lines(content: str) -> List[str]:
    """Split so that "\\n".join is an exact inverse, trailing newline included.

    `splitlines` is wrong here: it discards whether the file ended in a newline,
    so reassembling a partially-accepted document would silently add or drop one.
    """
    return content.split("\n")


def segment_revision(before: str, after: str) -> List[Segment]:
    """Cover `before` with alternating untouched and changed stretches.

    Adjacent non-equal opcodes are merged, so a delete immediately followed by an
    insert reads as the one replacement it is rather than as two decisions a
    reviewer could take in a combination that means nothing.
    """
    old, new = _lines(before), _lines(after)
    segments: List[Segment] = []
    pending_old: List[str] = []
    pending_new: List[str] = []

    def flush() -> None:
        nonlocal pending_old, pending_new
        if pending_old or pending_new:
            segments.append(
                Segment(index=sum(1 for s in segments if s.changed),
                        before=pending_old, after=pending_new)
            )
            pending_old, pending_new = [], []

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old, b=new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            flush()
            segments.append(Segment(index=-1, before=old[i1:i2], after=old[i1:i2]))
        else:
            pending_old.extend(old[i1:i2])
            pending_new.extend(new[j1:j2])
    flush()
    return segments


def hunk_count(segments: Sequence[Segment]) -> int:
    return sum(1 for segment in segments if segment.changed)


def apply_segments(segments: Sequence[Segment], accepted: Set[int]) -> str:
    """Rebuild the document taking the accepted hunks and leaving the rest."""
    out: List[str] = []
    for segment in segments:
        if not segment.changed:
            out.extend(segment.before)
        elif segment.index in accepted:
            out.extend(segment.after)
        else:
            out.extend(segment.before)
    return "\n".join(out)


def select_hunks(
    accepted_hunks: Optional[Iterable[int]], total: int
) -> Optional[Set[int]]:
    """None means "the whole proposal"; a set means exactly these hunks.

    An out-of-range index is refused rather than ignored. It means the reviewer
    decided against a diff that is no longer the diff — the document moved under
    them — and quietly applying whatever now sits at that position would apply a
    change nobody looked at.
    """
    if accepted_hunks is None:
        return None
    wanted = {int(index) for index in accepted_hunks}
    if not wanted:
        raise DocumentError("No changes were accepted")
    out_of_range = sorted(index for index in wanted if not 0 <= index < total)
    if out_of_range:
        raise DocumentError(
            f"The document changed since this was proposed: it now has {total} "
            f"change{'' if total == 1 else 's'}, so {out_of_range} cannot be applied"
        )
    return wanted


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
    folder_id: str = "",
    created_by: str = "",
) -> Document:
    title = title.strip()
    if not title:
        raise DocumentError("A document needs a title")
    if kind not in KINDS:
        raise DocumentError(f"kind must be one of {sorted(KINDS)}")
    if folder_id:
        # Imported here rather than at module scope: filing is a property of the
        # document, but the folder rules are not, and a document created by an
        # agent tool never supplies one.
        from .folders import FolderError, get_folder

        try:
            get_folder(db, workspace_id=workspace_id, folder_id=folder_id)
        except FolderError as exc:
            raise DocumentError(str(exc)) from exc
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
        folder_id=folder_id,
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
    accepted_hunks: Optional[Iterable[int]] = None,
) -> str:
    document = resolve(
        db, workspace_id=workspace_id, document_id=document_id, title=title
    )
    updated, _count = apply_replacement(
        document.content, find, replace, replace_all=replace_all
    )
    if accepted_hunks is not None:
        segments = segment_revision(document.content, updated)
        wanted = select_hunks(accepted_hunks, hunk_count(segments))
        updated = apply_segments(segments, wanted or set())
    return render_diff(document.content, updated, title=document.title)


def proposed_segments(
    db: Session,
    *,
    workspace_id: str,
    document_id: str = "",
    title: str = "",
    find: str,
    replace: str,
    replace_all: bool = False,
) -> List[Segment]:
    """The open document, covered by what this proposal would and would not touch."""
    document = resolve(
        db, workspace_id=workspace_id, document_id=document_id, title=title
    )
    updated, _count = apply_replacement(
        document.content, find, replace, replace_all=replace_all
    )
    return segment_revision(document.content, updated)


@dataclass
class EditOutcome:
    """What an edit actually did, which is not always what was proposed."""

    document: Document
    diff: str
    replacements: int
    applied_hunks: int
    total_hunks: int

    @property
    def partial(self) -> bool:
        return self.applied_hunks < self.total_hunks


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
    accepted_hunks: Optional[Iterable[int]] = None,
    created_by: str = "",
) -> EditOutcome:
    """Apply an edit, snapshotting the prior content first.

    `accepted_hunks` is the reviewer's amendment, not the model's: None means the
    proposal was taken whole, and a set of indices means only those hunks were
    accepted. The version summary records the difference either way, because a
    row reading "Agent edit" beside a document holding one of the three changes
    the agent asked for is a lie the undo button cannot correct.
    """
    document = resolve(
        db, workspace_id=workspace_id, document_id=document_id, title=title
    )
    updated, count = apply_replacement(
        document.content, find, replace, replace_all=replace_all
    )
    segments = segment_revision(document.content, updated)
    total = hunk_count(segments)
    wanted = select_hunks(accepted_hunks, total)
    if wanted is not None:
        updated = apply_segments(segments, wanted)
    applied = total if wanted is None else len(wanted)
    if len(updated) > MAX_DOCUMENT_CHARS:
        raise DocumentError(
            f"Edit would exceed the {MAX_DOCUMENT_CHARS:,}-character limit"
        )
    diff = render_diff(document.content, updated, title=document.title)
    note = summary or "Agent edit"
    if applied < total:
        note = f"{note} — {applied} of {total} proposed changes applied"
    db.add(
        DocumentVersion(
            workspace_id=workspace_id,
            document_id=document.id,
            content=document.content,
            summary=note[:300],
            created_by=created_by,
        )
    )
    document.content = updated
    db.commit()
    return EditOutcome(
        document=document,
        diff=diff,
        replacements=count,
        applied_hunks=applied,
        total_hunks=total,
    )


def replace_content(
    db: Session,
    *,
    workspace_id: str,
    document_id: str,
    content: str,
    summary: str = "Manual edit",
    created_by: str = "",
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
            created_by=created_by,
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
    db: Session,
    *,
    workspace_id: str,
    document_id: str,
    version_id: str,
    created_by: str = "",
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
        created_by=created_by,
    )


def delete_document(db: Session, *, workspace_id: str, document_id: str) -> None:
    document = get_document(db, workspace_id=workspace_id, document_id=document_id)
    for version in db.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document.id)
    ):
        db.delete(version)
    db.delete(document)
    db.commit()
