"""Live coworking: workspace events, presence heartbeats, card claims.

Three primitives with one purpose — a user and their agents working the same
workspace at the same time without treading on each other:

- `workspace_events` is the workspace-scoped sibling of `run_events`: run
  lifecycle, card claims and ticks land here so the shell holds ONE SSE
  connection (`/api/coworking/stream`) instead of one per run.
- `presences` is where "who is where" lives — cursor, selection, typing, and
  the live draft a follower watches. Upserted per (actor, surface), expired by
  TTL on read; nothing accumulates and nothing needs a sweep.
- claims make "don't do it twice" a database fact rather than an etiquette:
  one conditional UPDATE wins the card, and an expired claim reads as free —
  the `Run.lease_expires_at` philosophy applied to work items.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Agent, BoardCard, Presence, Run, WorkspaceEvent

#: A presence older than this is gone. Heartbeats arrive every few seconds
#: while an actor is active (and per keystroke burst while typing), so 15s
#: distinguishes "closed the tab" from "reading quietly".
PRESENCE_TTL_SECONDS = 15

#: How long a claim holds without being renewed. Long enough for a real chunk
#: of work, short enough that a crashed agent's card frees within the hour.
CLAIM_TTL_MINUTES = 30

#: Prompt excerpts quoted into digests and event payloads stay short: they are
#: labels for a person or a co-agent, not transcript.
EXCERPT_CHARS = 120


class ClaimConflict(RuntimeError):
    """The card is already being worked, and by whom."""

    def __init__(self, card: BoardCard) -> None:
        holder = card.claimed_label or card.claimed_by
        super().__init__(f"“{card.title}” is already claimed by {holder}")
        self.holder_id = card.claimed_by
        self.holder_kind = card.claimed_kind
        self.holder_label = holder


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Workspace events


def append_workspace_event(
    db: Session,
    *,
    workspace_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> WorkspaceEvent:
    """Append one event, sequence computed inside the INSERT.

    The same discipline — and the same reasoning — as `events.append_event`:
    two writers race this table in production (the run loop's worker thread
    and request threads), and the scalar subquery makes the assignment atomic
    on SQLite, the shipped backend. Callers on request threads keep a one-shot
    IntegrityError retry for snapshot-isolated backends; loop-side callers do
    not retry, because a rollback there would take the turn's own uncommitted
    work with it.
    """
    event = WorkspaceEvent(
        workspace_id=workspace_id,
        sequence=(
            select(func.coalesce(func.max(WorkspaceEvent.sequence), 0) + 1)
            .where(WorkspaceEvent.workspace_id == workspace_id)
            .scalar_subquery()
        ),
        event_type=event_type,
        payload_json=json.dumps(payload, separators=(",", ":"), default=str),
    )
    db.add(event)
    db.flush()
    return event


def events_after(
    db: Session, *, workspace_id: str, after: int, limit: int = 200
) -> List[WorkspaceEvent]:
    return list(
        db.scalars(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.sequence > after,
            )
            .order_by(WorkspaceEvent.sequence.asc())
            .limit(limit)
        )
    )


# ---------------------------------------------------------------------------
# Presence


def heartbeat_presence(
    db: Session,
    *,
    workspace_id: str,
    actor_id: str,
    actor_kind: str,
    actor_label: str,
    surface: str,
    state: Optional[Dict[str, Any]] = None,
) -> Presence:
    """Upsert this actor's position on one surface. Caller commits.

    An actor moving from surface to surface leaves stale rows behind on
    purpose — they expire by TTL, and deleting them here would make every
    keystroke a two-statement write for a row the reader already ignores.
    """
    row = db.scalar(
        select(Presence).where(
            Presence.workspace_id == workspace_id,
            Presence.actor_id == actor_id,
            Presence.surface == surface,
        )
    )
    if row is None:
        row = Presence(
            workspace_id=workspace_id,
            actor_id=actor_id,
            surface=surface,
        )
        db.add(row)
    row.actor_kind = actor_kind
    row.actor_label = actor_label[:120]
    row.state_json = json.dumps(state or {}, separators=(",", ":"), default=str)
    row.updated_at = utcnow()
    db.flush()
    return row


#: Pointer coordinates ride the heartbeat as a fraction of the surface's own
#: box, never pixels. Two people on a laptop and a wall monitor are looking at
#: the same document at different sizes, and a pixel offset would put one
#: person's cursor in the margin of the other's screen.
POINTER_PRECISION = 4


def sanitize_pointer(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return `state` with any `pointer` key reduced to two clamped fractions.

    `Presence.state_json` is relayed verbatim to every other client on the
    surface, so it is an input from one member that renders in another
    member's browser. Everything else in the blob is text a view escapes; a
    pointer is different because its numbers become a CSS transform. A NaN or
    a 1e9 there is not a security hole but it is a cursor drawn a mile off the
    page, dragging scrollbars onto everyone else's screen — so the shape is
    enforced once, here, rather than trusted in the renderer.

    A malformed pointer is dropped rather than 422'd: the rest of the
    heartbeat (typing, the live draft, the caret) is still worth relaying, and
    a member whose cursor stops moving is a smaller failure than a member who
    stops appearing.
    """
    raw = state.get("pointer")
    if raw is None:
        return state
    out = dict(state)
    if not isinstance(raw, dict):
        out.pop("pointer", None)
        return out
    # A missing key lands in the same place as an unparseable one: `float(None)`
    # raises TypeError straight into the handler below. Checked explicitly
    # rather than left to that, because `raw` is untyped JSON — `.get` is
    # `Any | None`, and mypy rejects the None arm even though the except catches
    # it. Same behaviour, stated instead of caught.
    raw_x, raw_y = raw.get("x"), raw.get("y")
    if raw_x is None or raw_y is None:
        out.pop("pointer", None)
        return out
    try:
        x = float(raw_x)
        y = float(raw_y)
    except (TypeError, ValueError):
        out.pop("pointer", None)
        return out
    # NaN fails every comparison, so `min`/`max` would carry it straight
    # through into the payload wearing the shape of a clamp. Reject it and the
    # infinities explicitly instead.
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x, y)):
        out.pop("pointer", None)
        return out
    out["pointer"] = {
        "x": round(min(1.0, max(0.0, x)), POINTER_PRECISION),
        "y": round(min(1.0, max(0.0, y)), POINTER_PRECISION),
    }
    return out


def drop_presence(
    db: Session, *, workspace_id: str, actor_id: str, surface: str = ""
) -> None:
    """Explicit goodbye — the tab closed, or the agent's turn ended. Caller
    commits. Without a surface, every row the actor holds goes."""
    stmt = select(Presence).where(
        Presence.workspace_id == workspace_id, Presence.actor_id == actor_id
    )
    if surface:
        stmt = stmt.where(Presence.surface == surface)
    for row in db.scalars(stmt):
        db.delete(row)
    db.flush()


def active_presences(
    db: Session,
    *,
    workspace_id: str,
    ttl_seconds: int = PRESENCE_TTL_SECONDS,
) -> List[Presence]:
    horizon = utcnow() - timedelta(seconds=ttl_seconds)
    return list(
        db.scalars(
            select(Presence)
            .where(
                Presence.workspace_id == workspace_id,
                Presence.updated_at >= horizon,
            )
            .order_by(Presence.updated_at.desc())
        )
    )


def presence_snapshot(row: Presence) -> Dict[str, Any]:
    try:
        state = json.loads(row.state_json)
    except ValueError:
        state = {}
    return {
        "actor_id": row.actor_id,
        "actor_kind": row.actor_kind,
        "actor_label": row.actor_label,
        "surface": row.surface,
        "state": state if isinstance(state, dict) else {},
        "updated_at": row.updated_at,
    }


# ---------------------------------------------------------------------------
# Claims


def _claim_free(now: Any):
    """The WHERE-fragment meaning "nobody holds this": never claimed, released,
    or held past its lease. Expiry is read this way everywhere instead of being
    swept away by a job — the clock does the releasing."""
    return or_(
        BoardCard.claimed_by == "",
        BoardCard.claim_expires_at.is_(None),
        BoardCard.claim_expires_at < now,
    )


def is_claimed(card: BoardCard) -> bool:
    return bool(
        card.claimed_by
        and card.claim_expires_at is not None
        and card.claim_expires_at >= utcnow()
    )


def claim_card(
    db: Session,
    *,
    workspace_id: str,
    card_id: str,
    actor_id: str,
    actor_kind: str,
    actor_label: str,
    run_id: str = "",
    ttl_minutes: int = CLAIM_TTL_MINUTES,
) -> BoardCard:
    """Take (or renew) the claim on one card, atomically.

    One conditional UPDATE decides the winner — the database picks it, not a
    Python `if`, exactly as the tool-approval routes claim a parked call. The
    holder re-claiming renews their lease; anyone else gets `ClaimConflict`
    naming who is on it. Caller commits.
    """
    now = utcnow()
    result = db.execute(
        update(BoardCard)
        .where(
            BoardCard.id == card_id,
            BoardCard.workspace_id == workspace_id,
            or_(_claim_free(now), BoardCard.claimed_by == actor_id),
        )
        .values(
            claimed_by=actor_id,
            claimed_kind=actor_kind,
            claimed_label=actor_label[:120],
            claimed_run_id=run_id,
            claim_expires_at=now + timedelta(minutes=ttl_minutes),
        )
    )
    won = (getattr(result, "rowcount", 0) or 0) > 0
    card = db.get(BoardCard, card_id)
    if card is None or card.workspace_id != workspace_id:
        raise ClaimConflict(BoardCard(title="that card", claimed_label="nobody"))
    db.expire(card)
    if not won:
        raise ClaimConflict(card)
    return card


def release_card(
    db: Session,
    *,
    workspace_id: str,
    card: BoardCard,
    actor_id: str,
    force: bool = False,
) -> BoardCard:
    """Give a card back. The holder may; anyone may with `force` — the API
    grants force to users only, because humans outrank agents on their own
    board. Releasing an unclaimed card is a no-op, not an error. Caller
    commits."""
    if card.workspace_id != workspace_id:
        raise ClaimConflict(card)
    if is_claimed(card) and card.claimed_by != actor_id and not force:
        raise ClaimConflict(card)
    clear_claim(card)
    db.flush()
    return card


def clear_claim(card: BoardCard) -> None:
    card.claimed_by = ""
    card.claimed_kind = ""
    card.claimed_label = ""
    card.claimed_run_id = ""
    card.claim_expires_at = None


def claim_snapshot(card: BoardCard) -> Dict[str, Any]:
    """The claim as the API and the agent tools report it: an expired lease
    reads as no claim at all, so no caller ever renders a ghost holder."""
    if not is_claimed(card):
        return {"claimed": False}
    return {
        "claimed": True,
        "claimed_by": card.claimed_by,
        "claimed_kind": card.claimed_kind,
        "claimed_label": card.claimed_label,
        "claimed_run_id": card.claimed_run_id,
        "claim_expires_at": card.claim_expires_at,
    }


# ---------------------------------------------------------------------------
# Awareness digest — what one agent should know about everyone else


def agent_actor(db: Session, run: Run) -> tuple[str, str]:
    """(actor_id, label) an agent run presents as. A run without an agent row
    is the stock assistant; it still needs a stable id so its claims and
    presence read as one actor across turns."""
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    if agent is not None and agent.workspace_id == run.workspace_id and agent.name:
        return agent.id, agent.name
    return run.agent_id or "assistant", "Assistant"


def digest_block(db: Session, *, run: Run) -> str:
    """The coworking context spliced into a turn's instructions: other runs in
    flight and cards under claim, so an agent routes around work already in
    hand instead of duplicating it. "" when there is nothing to say — the
    common case, and the reason this never earns a heading in a quiet
    workspace."""
    from . import conversations

    now = utcnow()
    lines: List[str] = []
    others = list(
        db.scalars(
            select(Run)
            .where(
                Run.workspace_id == run.workspace_id,
                Run.id != run.id,
                Run.status.in_(["queued", "running", "waiting_for_approval"]),
            )
            .order_by(Run.created_at.desc())
            .limit(8)
        )
    )
    for other in others:
        # The digest speaks with the authority of the person whose turn this
        # is: a prompt from another member's personal thread must not reach
        # them through their agent's instructions. Same gate as the streams.
        if not conversations.run_activity_visible(
            db,
            actor_workspace_id=run.workspace_id,
            actor_user_id=run.created_by,
            run=other,
        ):
            continue
        _, label = agent_actor(db, other)
        lines.append(f'- {label} ({other.status}): "{excerpt(other.prompt)}"')
    claimed = list(
        db.scalars(
            select(BoardCard)
            .where(
                BoardCard.workspace_id == run.workspace_id,
                BoardCard.claimed_by != "",
                BoardCard.claim_expires_at.is_not(None),
                BoardCard.claim_expires_at >= now,
                BoardCard.done_at.is_(None),
            )
            .order_by(BoardCard.updated_at.desc())
            .limit(12)
        )
    )
    for card in claimed:
        holder = card.claimed_label or card.claimed_by
        lines.append(f"- Card “{excerpt(card.title, 80)}” is claimed by {holder}")
    if not lines:
        return ""
    return (
        "## Coworking awareness\n"
        "Work already in other hands right now — do not redo it, and claim a "
        "todo item (todo_claim) before working it:\n" + "\n".join(lines)
    )
