"""The sidebar Favorites block: anything with a name, pinned per user.

One store for eight kinds of target rather than eight pin tables, because the
sidebar treats them as one list — a thread above a dashboard above a cron, in
the order the user chose. What keeps that safe is that this module owns *both*
directions of the pair: `FAVORITE_KINDS` is the closed set a route will accept,
and `resolve` is the only way a (kind, target_id) becomes a label — always
under the target kind's own visibility rule, never a bare primary-key lookup.
A favorite is a pointer the user wrote down; it must never become a way to
read a name they could not otherwise see.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute, Session

from ..models import (
    Agent,
    Board,
    Cron,
    Dashboard,
    Document,
    Favorite,
    Project,
    Workflow,
    new_id,
)
from . import conversations

#: The kinds a favorite can point at, mirroring `subjects.SUBJECT_KINDS`.
CONVERSATION = "conversation"
AGENT = "agent"
DOCUMENT = "document"
PROJECT = "project"
BOARD = "board"
DASHBOARD = "dashboard"
WORKFLOW = "workflow"
CRON = "cron"
FAVORITE_KINDS = (
    CONVERSATION,
    AGENT,
    DOCUMENT,
    PROJECT,
    BOARD,
    DASHBOARD,
    WORKFLOW,
    CRON,
)

#: kind -> (id, workspace_id, label) columns for everything whose visibility is
#: simply "in the caller's workspace". Conversations are absent on purpose: a
#: thread can be another member's personal one, and the single rule that
#: decides that is `conversations.resolve_visible` — `resolve` routes them
#: through it rather than restating it here.
_Column = InstrumentedAttribute[str]
_WORKSPACE_LABELS: Dict[str, Tuple[_Column, _Column, _Column]] = {
    AGENT: (Agent.id, Agent.workspace_id, Agent.name),
    DOCUMENT: (Document.id, Document.workspace_id, Document.title),
    PROJECT: (Project.id, Project.workspace_id, Project.name),
    BOARD: (Board.id, Board.workspace_id, Board.name),
    DASHBOARD: (Dashboard.id, Dashboard.workspace_id, Dashboard.name),
    WORKFLOW: (Workflow.id, Workflow.workspace_id, Workflow.name),
    CRON: (Cron.id, Cron.workspace_id, Cron.name),
}


def resolve(
    db: Session, *, workspace_id: str, user_id: str, kind: str, target_id: str
) -> Optional[str]:
    """The target's label as this actor may see it, or None.

    None deliberately covers deleted and merely-invisible alike: the two are
    the same fact to this caller, and distinguishing them is exactly the oracle
    the favorites store must not become. Raises KeyError on a kind outside
    `FAVORITE_KINDS` — routes reject those at the boundary before any id is
    looked at.
    """
    if kind == CONVERSATION:
        conversation = conversations.resolve_visible(
            db, workspace_id=workspace_id, user_id=user_id, conversation_id=target_id
        )
        return conversation.title if conversation is not None else None
    id_column, workspace_column, label_column = _WORKSPACE_LABELS[kind]
    return db.scalar(
        select(label_column).where(
            id_column == target_id,
            workspace_column == workspace_id,
        )
    )


def list_favorites(
    db: Session, *, workspace_id: str, user_id: str
) -> List[Tuple[Favorite, str]]:
    """This user's favorites with resolved labels, unresolvable rows dropped.

    Dropped, not lazily deleted: a target can be invisible without being gone —
    a thread unshared beneath the favorite resolves again the day it is
    re-shared — and deleting on read would also turn every GET into a write.
    A row whose target is truly gone costs one indexed SELECT per listing,
    which is nothing against silently erasing a preference that might come
    back.
    """
    rows = db.scalars(
        select(Favorite)
        .where(
            Favorite.workspace_id == workspace_id,
            Favorite.user_id == user_id,
        )
        .order_by(Favorite.ordinal, Favorite.created_at)
    ).all()
    resolved: List[Tuple[Favorite, str]] = []
    for favorite in rows:
        label = resolve(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            kind=favorite.kind,
            target_id=favorite.target_id,
        )
        if label is not None:
            resolved.append((favorite, label))
    return resolved


def find_favorite(
    db: Session, *, workspace_id: str, user_id: str, kind: str, target_id: str
) -> Optional[Favorite]:
    return db.scalar(
        select(Favorite).where(
            Favorite.workspace_id == workspace_id,
            Favorite.user_id == user_id,
            Favorite.kind == kind,
            Favorite.target_id == target_id,
        )
    )


def add_favorite(
    db: Session, *, workspace_id: str, user_id: str, kind: str, target_id: str
) -> Favorite:
    """Pin one named thing, or return the pin if it is already there.

    The caller proves visibility via `resolve` BEFORE calling this; nothing
    here re-checks it. A new favorite lands after everything already pinned —
    "add to favorites" means "append", not "shuffle what I arranged".
    """
    favorite = find_favorite(
        db, workspace_id=workspace_id, user_id=user_id, kind=kind, target_id=target_id
    )
    if favorite is None:
        favorite = Favorite(
            id=new_id(),
            workspace_id=workspace_id,
            user_id=user_id,
            kind=kind,
            target_id=target_id,
            ordinal=_next_ordinal(db, workspace_id=workspace_id, user_id=user_id),
        )
        db.add(favorite)
    db.flush()
    return favorite


def _next_ordinal(db: Session, *, workspace_id: str, user_id: str) -> int:
    ordinals = db.scalars(
        select(Favorite.ordinal).where(
            Favorite.workspace_id == workspace_id,
            Favorite.user_id == user_id,
        )
    ).all()
    return max(ordinals, default=-1) + 1


def apply_order(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    ordinals: Mapping[Tuple[str, str], int],
) -> None:
    """Reorder several favorites at once. Every named pair must be pinned.

    Raises `KeyError` naming the first (kind, target_id) that is not, which the
    caller turns into a 404 — the same contract as `dashboards.store
    .apply_layout`, for the same reason: an order naming an entry the user does
    not have is a stale screen, and applying the rest would half-save it.
    """
    pinned = {
        (favorite.kind, favorite.target_id): favorite
        for favorite in db.scalars(
            select(Favorite).where(
                Favorite.workspace_id == workspace_id,
                Favorite.user_id == user_id,
            )
        )
    }
    missing = [pair for pair in ordinals if pair not in pinned]
    if missing:
        raise KeyError(sorted(missing)[0])
    for pair, ordinal in ordinals.items():
        pinned[pair].ordinal = ordinal
    db.flush()
