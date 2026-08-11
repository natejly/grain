"""Folders: where a file is filed.

A folder holds nothing of its own. It is a label with a parent, and the only
thing that can be *lost* by deleting one is a document — which is why deletion
refuses rather than cascades (see `delete_folder`).

Three invariants, each enforced here rather than in the route, because the
executor of a future agent tool would otherwise get a second, weaker copy:

* the tree is a tree — a folder can never become its own ancestor,
* it is shallow — `MAX_DEPTH` levels, so the sidebar stays readable and a
  recursive walk stays bounded,
* siblings have distinct names, case-insensitively, because "Notes" and "notes"
  side by side is a filing system that cannot be read aloud.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Document, Folder

MAX_NAME_CHARS = 120
#: Counted from 1 for a top-level folder. Deep hierarchies are how a file tree
#: becomes unnavigable, and every level costs a horizontal indent in a sidebar
#: that is already sharing the width with an editor.
MAX_DEPTH = 6
#: A ceiling on the whole workspace, so a loop in a client cannot grow a tree
#: nobody can render.
MAX_FOLDERS = 200

#: The top level, which is not a folder. Empty rather than None so a document's
#: `folder_id` has one representation everywhere — see `Folder.parent_id`.
ROOT = ""


class FolderError(ValueError):
    """A user-facing problem with a folder operation."""


def list_folders(db: Session, *, workspace_id: str) -> List[Folder]:
    """Every folder in the workspace, siblings in name order.

    The whole tree in one query: it is bounded by `MAX_FOLDERS`, and the client
    needs all of it to render a tree or a "move to…" list anyway.
    """
    return list(
        db.scalars(
            select(Folder)
            .where(Folder.workspace_id == workspace_id)
            .order_by(func.lower(Folder.name), Folder.id)
        )
    )


def get_folder(db: Session, *, workspace_id: str, folder_id: str) -> Folder:
    folder = db.scalar(
        select(Folder).where(
            Folder.id == folder_id, Folder.workspace_id == workspace_id
        )
    )
    if folder is None:
        raise FolderError("No folder with that id")
    return folder


def _children(folders: List[Folder]) -> Dict[str, List[Folder]]:
    index: Dict[str, List[Folder]] = {}
    for folder in folders:
        index.setdefault(folder.parent_id, []).append(folder)
    return index


def _depth(folders: List[Folder], parent_id: str) -> int:
    """How many levels sit above a child of `parent_id`, `parent_id` included."""
    by_id = {folder.id: folder for folder in folders}
    depth = 0
    cursor = parent_id
    # Bounded by the folder count, so a cycle that somehow reached storage is a
    # wrong answer rather than a hung request.
    while cursor and depth <= len(folders):
        parent = by_id.get(cursor)
        if parent is None:
            break
        depth += 1
        cursor = parent.parent_id
    return depth


def _subtree_height(folders: List[Folder], folder_id: str) -> int:
    """Levels of folder below `folder_id`, itself counted as 1."""
    children = _children(folders)

    def walk(node: str, guard: int) -> int:
        if guard <= 0:
            return 1
        below = [walk(child.id, guard - 1) for child in children.get(node, [])]
        return 1 + max(below, default=0)

    return walk(folder_id, len(folders))


def descendants(folders: List[Folder], folder_id: str) -> List[Folder]:
    """Every folder beneath this one, at any depth. Excludes the folder itself."""
    children = _children(folders)
    out: List[Folder] = []
    queue = list(children.get(folder_id, []))
    # Guarded by the folder count for the same reason `_depth` is.
    while queue and len(out) <= len(folders):
        node = queue.pop()
        out.append(node)
        queue.extend(children.get(node.id, []))
    return out


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise FolderError("A folder needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise FolderError(f"A folder name is at most {MAX_NAME_CHARS} characters")
    # A name is rendered as one row in a tree, so an embedded newline would draw
    # a folder that is two lines tall and reads as two folders.
    if "\n" in name or "\r" in name:
        raise FolderError("A folder name is a single line")
    return name


def _resolve_parent(parent_id: str, folders: List[Folder]) -> str:
    if not parent_id:
        return ROOT
    if not any(folder.id == parent_id for folder in folders):
        raise FolderError("No folder with that id")
    if _depth(folders, parent_id) >= MAX_DEPTH:
        raise FolderError(f"Folders nest at most {MAX_DEPTH} levels deep")
    return parent_id


def _refuse_duplicate(
    folders: List[Folder], *, parent_id: str, name: str, ignore_id: str = ""
) -> None:
    wanted = name.strip().lower()
    for folder in folders:
        if folder.id == ignore_id or folder.parent_id != parent_id:
            continue
        if folder.name.strip().lower() == wanted:
            where = "here" if parent_id else "at the top level"
            raise FolderError(f"A folder named “{folder.name}” already exists {where}")


def create_folder(
    db: Session,
    *,
    workspace_id: str,
    name: str,
    parent_id: str = ROOT,
    created_by: str = "",
) -> Folder:
    folders = list_folders(db, workspace_id=workspace_id)
    if len(folders) >= MAX_FOLDERS:
        raise FolderError(f"A workspace holds at most {MAX_FOLDERS} folders")
    clean = _clean_name(name)
    parent = _resolve_parent(parent_id, folders)
    _refuse_duplicate(folders, parent_id=parent, name=clean)
    folder = Folder(
        workspace_id=workspace_id,
        name=clean,
        parent_id=parent,
        created_by=created_by,
    )
    db.add(folder)
    db.commit()
    return folder


def update_folder(
    db: Session,
    *,
    workspace_id: str,
    folder_id: str,
    name: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Folder:
    """Rename and/or move, in one call because a client often does both.

    `None` means "leave it alone"; `""` for `parent_id` means the top level, so
    moving a folder out of its parent is expressible and is not the same request
    as not moving it.
    """
    folder = get_folder(db, workspace_id=workspace_id, folder_id=folder_id)
    folders = list_folders(db, workspace_id=workspace_id)
    target = folder.parent_id if parent_id is None else parent_id
    clean = folder.name if name is None else _clean_name(name)

    if parent_id is not None and target != folder.parent_id:
        if target == folder.id:
            raise FolderError("A folder cannot contain itself")
        if any(child.id == target for child in descendants(folders, folder.id)):
            raise FolderError("A folder cannot be moved inside itself")
        target = _resolve_parent(target, folders)
        # The folder's own children move with it, so the *tallest* branch under
        # it is what has to fit — checking only the folder would let a three-deep
        # subtree land at level five and quietly break the invariant.
        if _depth(folders, target) + _subtree_height(folders, folder.id) > MAX_DEPTH:
            raise FolderError(
                f"That move would nest folders more than {MAX_DEPTH} levels deep"
            )

    _refuse_duplicate(folders, parent_id=target, name=clean, ignore_id=folder.id)
    folder.name = clean
    folder.parent_id = target
    db.commit()
    return folder


def documents_in(db: Session, *, workspace_id: str, folder_ids: List[str]) -> int:
    if not folder_ids:
        return 0
    return int(
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.workspace_id == workspace_id,
                Document.folder_id.in_(folder_ids),
            )
        )
        or 0
    )


def delete_folder(db: Session, *, workspace_id: str, folder_id: str) -> int:
    """Delete an empty folder and the empty folders under it. Returns the count.

    **A folder holding files is refused, not cascaded.** The folder is a label
    and can be recreated in a second; the documents are the work, and they take
    their version history with them. The confirmation dialog that a cascade
    would need — "delete 14 documents?" — asks a question the user cannot answer
    at the moment it is asked, because the fourteen are not on screen. So the
    refusal names the count instead, and the user moves or deletes the files
    themselves, having seen them.

    Empty descendants go, because nothing can be lost with them and requiring a
    delete per level to dismantle an empty tree is busywork, not a safeguard.
    """
    folder = get_folder(db, workspace_id=workspace_id, folder_id=folder_id)
    folders = list_folders(db, workspace_id=workspace_id)
    doomed = [folder, *descendants(folders, folder.id)]
    held = documents_in(
        db, workspace_id=workspace_id, folder_ids=[row.id for row in doomed]
    )
    if held:
        raise FolderError(
            f"“{folder.name}” still holds {held} file{'' if held == 1 else 's'}. "
            "Move or delete them first."
        )
    for row in doomed:
        db.delete(row)
    db.commit()
    return len(doomed)


def move_document(
    db: Session, *, workspace_id: str, document_id: str, folder_id: str
) -> Document:
    """File a document, or return it to the top level with an empty `folder_id`."""
    document = db.scalar(
        select(Document).where(
            Document.id == document_id, Document.workspace_id == workspace_id
        )
    )
    if document is None:
        raise FolderError("No document with that id")
    if folder_id:
        # Resolved through this module so a folder in another workspace answers
        # "no folder with that id" rather than silently filing the document into
        # a folder its owner cannot see.
        get_folder(db, workspace_id=workspace_id, folder_id=folder_id)
    document.folder_id = folder_id
    db.commit()
    return document
