"""The sidebar Favorites block — per user, cross device.

Four routes, modelled on the dashboard-pin routes: every query filters
`actor.user_id` AND `actor.workspace_id`; PUT and DELETE are idempotent by
construction, so there is no Idempotency-Key; and nothing here is audited —
which of the workspace's named things one member keeps in reach is a personal
view preference, not an action against shared state.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Favorite
from ..schemas import FavoriteOrderUpdate, FavoriteOut
from ..services import favorites as favorites_service
from ..services.favorites import FAVORITE_KINDS

router = APIRouter(prefix="/api/favorites", tags=["favorites"])


def _out(favorite: Favorite, label: str) -> FavoriteOut:
    return FavoriteOut(
        kind=favorite.kind,
        target_id=favorite.target_id,
        label=label,
        ordinal=favorite.ordinal,
    )


def _check_kind(kind: str) -> None:
    """422 an unknown kind at the boundary, before any id is looked at.

    422 and not 404: the request is malformed, not aimed at a missing resource
    — same split as a bad cron expression versus a missing cron.
    """
    if kind not in FAVORITE_KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown favorite kind {kind!r}")


@router.get("", response_model=List[FavoriteOut])
def list_favorites(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[FavoriteOut]:
    """The caller's favorites in their chosen order, labels resolved.

    A row whose target no longer resolves — deleted, or a thread unshared out
    from under the pin — is dropped from the answer, not an error and not
    deleted (see `services/favorites.list_favorites` for why it is left).
    """
    return [
        _out(favorite, label)
        for favorite, label in favorites_service.list_favorites(
            db, workspace_id=actor.workspace_id, user_id=actor.user_id
        )
    ]


@router.put("/order", response_model=List[FavoriteOut])
def save_favorites_order(
    payload: FavoriteOrderUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[FavoriteOut]:
    """Save the whole block's order in one write. See `FavoriteOrderUpdate`."""
    try:
        favorites_service.apply_order(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            ordinals={
                (entry.kind, entry.target_id): entry.ordinal
                for entry in payload.entries
            },
        )
    except KeyError as exc:
        # An order naming something this user has not favorited. 404 rather
        # than 422, the dashboard-layout reasoning: from the caller's side the
        # entry does not exist, and saying more would tell them whether it
        # exists for somebody else.
        raise HTTPException(status_code=404, detail="Not a favorite") from exc
    db.commit()
    return list_favorites(actor=actor, db=db)


@router.put("/{kind}/{target_id}", response_model=FavoriteOut)
def add_favorite(
    kind: str,
    target_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> FavoriteOut:
    """Pin one named thing to the caller's sidebar; already pinned is a no-op.

    Visibility is proved BEFORE anything is stored: without that, a favorite
    would be a write-side oracle for ids in other tenants' workspaces and in
    other members' personal threads. 404 and not 403 — `crons._load`'s
    reasoning — because a 403 would confirm the id names something real.
    """
    _check_kind(kind)
    label = favorites_service.resolve(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        kind=kind,
        target_id=target_id,
    )
    if label is None:
        raise HTTPException(status_code=404, detail="Favorite target not found")
    favorite = favorites_service.add_favorite(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        kind=kind,
        target_id=target_id,
    )
    db.commit()
    db.refresh(favorite)
    return _out(favorite, label)


@router.delete("/{kind}/{target_id}", status_code=204)
def remove_favorite(
    kind: str,
    target_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Response:
    """Unpin. 404 on anything the caller has not pinned — foreign, missing and
    never-favorited are the same fact from this side of the workspace wall."""
    _check_kind(kind)
    favorite = favorites_service.find_favorite(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        kind=kind,
        target_id=target_id,
    )
    if favorite is None:
        raise HTTPException(status_code=404, detail="Not a favorite")
    db.delete(favorite)
    db.commit()
    return Response(status_code=204)
