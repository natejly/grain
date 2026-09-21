"""Per-member preferences: the caller's own membership, nobody else's.

The daily digest opt-in, Safe mode, the memory opt-out, the response style,
the display name and the password change. None takes a resource
id at all: the row each edits is the (workspace, user) membership — or the
caller's own user row — the session already names, so there is nothing here
for a foreign id to probe (the isolation sweep covers them as SCOPED). Natural
upserts of one or two columns, so no Idempotency-Key — replaying "enabled at
9" is "enabled at 9".

The matching read is on `GET /api/bootstrap`, beside the identity these
preferences belong to; a second GET here would be a smaller copy of that.
`GET /recap` is the one read that does live here: it is not a preference
echo but the member's own month-to-date aggregates, which bootstrap has no
business computing on every load.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Agent, Conversation, Membership, Run, Space, User, UserSession
from ..schemas import (
    ApiModel,
    AuthAcknowledgement,
    MeRecapOut,
    PasswordChangeIn,
    ProfileIn,
    ProfileOut,
    RecapGroupOut,
    StylePrefIn,
    StylePrefOut,
)
from ..services import api_tokens as api_tokens_service
from ..services import memory as memory_service
from ..services.audit import record_audit
from ..services.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)
from ..services.auth.ratelimit import auth_rate_limiter

router = APIRouter(prefix="/api/me", tags=["me"])


class DigestPrefsIn(ApiModel):
    enabled: bool
    #: The UTC hour after which the daily mail may go out. Pydantic's bounds
    #: are the validation — 24 is a 422 at the door, never a bad row.
    hour_utc: int = Field(ge=0, le=23)


class DigestPrefsOut(ApiModel):
    enabled: bool
    hour_utc: int


class SafeModePrefIn(ApiModel):
    enabled: bool


class SafeModePrefOut(ApiModel):
    enabled: bool


class MemoryPrefIn(ApiModel):
    enabled: bool


class MemoryPrefOut(ApiModel):
    enabled: bool


def _own_membership(db: Session, actor: Actor) -> Membership:
    """The caller's membership row, or the 404 that says it is gone.

    The actor dependency vouched for the workspace; it does not vouch for the
    row, and a session outliving its membership is the case both routes here
    have to answer the same way.
    """
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == actor.workspace_id,
            Membership.user_id == actor.user_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    return membership


@router.put("/safe-mode", response_model=SafeModePrefOut)
def update_safe_mode(
    payload: SafeModePrefIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SafeModePrefOut:
    """Turn the approval step on or off for the caller's future threads.

    A seed, not a switch on anything already running: it changes what
    `services.conversations.default_approval_mode` hands the next thread this
    member creates, and touches no existing conversation. That is the whole
    reason it can be a plain preference rather than a privileged operation —
    turning it *off* cannot loosen a thread a colleague is watching, and
    turning it *on* cannot strand one mid-turn.

    Audited on both edges. Off is the interesting direction, and an audit trail
    that only recorded the cautious half would be no trail at all.
    """
    membership = _own_membership(db, actor)
    membership.safe_mode = payload.enabled
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="safe_mode.updated",
        resource_type="membership",
        resource_id=membership.id,
        detail={"enabled": payload.enabled},
    )
    db.commit()
    return SafeModePrefOut(enabled=membership.safe_mode)


@router.put("/memory", response_model=MemoryPrefOut)
def update_memory_pref(
    payload: MemoryPrefIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MemoryPrefOut:
    """Turn memory on or off for the caller's future runs.

    Off skips recall AND extraction on this member's runs, and takes effect on
    the next run — nothing mid-flight is touched. The explicit remember/forget
    tools still work: an explicit instruction outranks a default.

    Audited on both edges, like Safe mode: off is the interesting direction,
    and a trail that only recorded the cautious half would be no trail at all.
    """
    membership = _own_membership(db, actor)
    membership.memory_enabled = payload.enabled
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="memory_pref.updated",
        resource_type="membership",
        resource_id=membership.id,
        detail={"enabled": payload.enabled},
    )
    db.commit()
    return MemoryPrefOut(enabled=membership.memory_enabled)


@router.put("/digest", response_model=DigestPrefsOut)
def update_digest_prefs(
    payload: DigestPrefsIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DigestPrefsOut:
    """Set the caller's own digest opt-in and hour. Member self-serve."""
    membership = _own_membership(db, actor)
    membership.digest_enabled = payload.enabled
    membership.digest_hour_utc = payload.hour_utc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="digest.updated",
        resource_type="membership",
        resource_id=membership.id,
        detail={"enabled": payload.enabled, "hour_utc": payload.hour_utc},
    )
    db.commit()
    return DigestPrefsOut(
        enabled=membership.digest_enabled, hour_utc=membership.digest_hour_utc
    )


@router.put("/style", response_model=StylePrefOut)
def update_style_pref(
    payload: StylePrefIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> StylePrefOut:
    """Set the caller's response style for their future turns.

    "normal" means no style instruction at all — the byte-identity contract
    `services.styles.style_block` keeps. A "custom" preset with nothing to say
    is refused rather than stored: an empty custom block must be
    unrepresentable, so the run path never has to decide what it means.

    Only an explicit custom payload rewrites the stored prose. A fixed preset
    leaves `custom_style_text` untouched — the OpenAPI contract marks the
    field optional, so the natural minimal body `{"preset": "concise"}` from
    any client must not silently erase the member's saved directive; the text
    survives the switch and is waiting when they flip back to Custom.

    Audited on both edges like Safe mode, with the preset only — never the
    prose, which is the member's own working text.
    """
    if payload.preset == "custom" and not payload.custom_style_text.strip():
        raise HTTPException(
            status_code=422,
            detail="A custom style needs its text; pick a preset or write one.",
        )
    membership = _own_membership(db, actor)
    membership.style_preset = payload.preset
    if payload.preset == "custom":
        membership.custom_style_text = payload.custom_style_text.strip()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="style.updated",
        resource_type="membership",
        resource_id=membership.id,
        detail={"preset": payload.preset},
    )
    db.commit()
    return StylePrefOut(
        preset=membership.style_preset,
        custom_style_text=membership.custom_style_text,
    )


def _named(
    groups: List[Tuple[str, int]], names: Dict[str, str]
) -> List[RecapGroupOut]:
    """Ranked ids to rows, names resolved workspace-scoped beforehand. A stale
    id (its space or agent deleted since) keeps the id and answers "" for the
    name — the admin usage page's stale-id-stays-an-id shape."""
    return [
        RecapGroupOut(id=key, name=names.get(key, ""), count=count)
        for key, count in groups
    ]


@router.get("/recap", response_model=MeRecapOut)
def get_recap(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MeRecapOut:
    """The caller's own month so far — deterministic aggregates, no LLM.

    Window: UTC calendar month-to-date, no query params. Five bounded
    read-only selects, every one workspace- AND member-scoped, so a
    teammate's activity in the same workspace never inflates this recap.
    `top_spaces` excludes the "" sentinel — it is the global shelf, not a
    space — and `memories_learned` counts only what the caller themself
    learned (their own rows, plus shared rows their own runs extracted; see
    `services.memory.count_learned`), with liveness still through
    `services.memory._active`, so a superseded claim drops out of the number
    the way it drops out of recall. No `db.get`, no writes.
    """
    since = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mine_since = [
        Conversation.workspace_id == actor.workspace_id,
        Conversation.created_by == actor.user_id,
        Conversation.created_at >= since,
    ]
    threads_started = int(
        db.scalar(select(func.count(Conversation.id)).where(*mine_since)) or 0
    )
    runs_since = [
        Run.workspace_id == actor.workspace_id,
        Run.created_by == actor.user_id,
        Run.created_at >= since,
    ]
    runs_started = int(
        db.scalar(select(func.count(Run.id)).where(*runs_since)) or 0
    )
    memories_learned = memory_service.count_learned(
        db,
        workspace_id=actor.workspace_id,
        viewer_id=actor.user_id,
        since=since,
    )
    # The id tiebreak keeps equal counts in one order across engines — this
    # page promises determinism by name.
    space_groups: List[Tuple[str, int]] = [
        (str(space_id), int(count))
        for space_id, count in db.execute(
            select(Conversation.space_id, func.count(Conversation.id))
            .where(*mine_since, Conversation.space_id != "")
            .group_by(Conversation.space_id)
            .order_by(func.count(Conversation.id).desc(), Conversation.space_id.asc())
            .limit(5)
        ).all()
    ]
    space_names: Dict[str, str] = {
        str(space_id): str(name)
        for space_id, name in db.execute(
            select(Space.id, Space.name).where(
                Space.workspace_id == actor.workspace_id,
                Space.id.in_([key for key, _ in space_groups] or [""]),
            )
        ).all()
    }
    agent_groups: List[Tuple[str, int]] = [
        (str(agent_id), int(count))
        for agent_id, count in db.execute(
            select(Run.agent_id, func.count(Run.id))
            .where(*runs_since)
            .group_by(Run.agent_id)
            .order_by(func.count(Run.id).desc(), Run.agent_id.asc())
            .limit(5)
        ).all()
    ]
    agent_names: Dict[str, str] = {
        str(agent_id): str(name)
        for agent_id, name in db.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == actor.workspace_id,
                Agent.id.in_([key for key, _ in agent_groups] or [""]),
            )
        ).all()
    }
    return MeRecapOut(
        since=since,
        threads_started=threads_started,
        runs_started=runs_started,
        memories_learned=memories_learned,
        top_spaces=_named(space_groups, space_names),
        top_agents=_named(agent_groups, agent_names),
    )


def _own_user(db: Session, actor: Actor) -> User:
    """The caller's own user row, through a scoped select rather than db.get
    (DB_GET_ALLOWLIST stays untouched)."""
    user = db.scalar(select(User).where(User.id == actor.user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return user


@router.patch("/profile", response_model=ProfileOut)
def update_profile(
    payload: ProfileIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ProfileOut:
    """Rename the caller. `users.name` is already the attribution source
    everywhere (bootstrap Identity, coworking labels, message sender names),
    so this edits it in place — no second display-name column.

    Historical records keep the old name by design: actor labels stamped into
    past workspace events and audit rows are records of what happened, and the
    live surfaces pick the new name up on their next read.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A name is required")
    user = _own_user(db, actor)
    user.name = name
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="profile.updated",
        resource_type="user",
        resource_id=user.id,
        detail={},
    )
    db.commit()
    # Email is read off the row and never accepted in the body: this route
    # renames, it does not re-identify.
    return ProfileOut(user_id=user.id, email=user.email, name=user.name)


@router.post("/password", response_model=AuthAcknowledgement)
def change_password(
    payload: PasswordChangeIn,
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AuthAcknowledgement:
    """Change the caller's password, proving the current one first.

    Every OTHER session is revoked — a password change is what you do when you
    suspect the old one leaked — while the session making the change survives:
    logging the person out of the tab they just secured would read as a
    failure. (`revoke_all_sessions` is the logged-out reset flow's helper and
    revokes everything including the caller's, so the scoped sweep here is
    written out with the caller's session excluded.)

    The caller's live API tokens are revoked too, and the acknowledgement
    says how many: a `grain_…` bearer minted by whoever held the leaked
    password appears in no session list and would otherwise survive the
    rotation indefinitely. The reset flow makes the same sweep.

    The change itself is audited with no detail payload — the fact of it is
    the record — and each revoked token gets its own `api_token.revoked` row.
    """
    key = f"password-change:{actor.user_id}"
    if not auth_rate_limiter.allow(
        key,
        limit=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Try again later."
        )
    user = _own_user(db, actor)
    if not user.password_hash:
        # NULL and "" both mean "no password credential" (the verify_password
        # doctrine): there is no current password to prove, so there is
        # nothing this route can change.
        raise HTTPException(
            status_code=422,
            detail="This account signs in without a password (e.g. with Google).",
        )
    check = verify_password(user.password_hash, payload.current_password)
    if not check.ok:
        # Generic on purpose: which part was wrong is not the caller's to
        # learn beyond "not this".
        raise HTTPException(status_code=403, detail="Password change refused")
    try:
        validate_password(
            payload.new_password,
            min_length=settings.password_min_length,
            max_length=settings.password_max_length,
        )
    except PasswordPolicyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    user.password_hash = hash_password(payload.new_password)
    now = utcnow()
    others = db.scalars(
        select(UserSession).where(
            UserSession.user_id == actor.user_id,
            UserSession.revoked_at.is_(None),
            UserSession.id != (actor.session_id or ""),
        )
    )
    for session in others:
        session.revoked_at = now
    # The other bearer-credential class the leak-response must reach: API
    # tokens resolve past every session check, so they die with the password.
    revoked_tokens = api_tokens_service.revoke_all_for_user(
        db, user_id=actor.user_id, now=now
    )
    for token in revoked_tokens:
        record_audit(
            db,
            workspace_id=token.workspace_id,
            actor_id=actor.user_id,
            action="api_token.revoked",
            resource_type="api_token",
            resource_id=token.id,
            detail={"name": token.name, "reason": "password_changed"},
        )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="password.changed",
        resource_type="user",
        resource_id=user.id,
        detail={},
    )
    db.commit()
    count = len(revoked_tokens)
    if count:
        noun = "API token" if count == 1 else "API tokens"
        detail = (
            "Password updated. Other sessions were signed out "
            f"and {count} {noun} revoked."
        )
    else:
        detail = "Password updated. Other sessions were signed out."
    return AuthAcknowledgement(detail=detail)
