"""Inviting somebody into a workspace that already exists.

Until this module there was no such thing. `Membership` rows were only ever
written by `_create_account`, always `role="owner"` of a workspace created in
the same breath, so no workspace could gain a second member — which made
`require_owner` trivially true, the workspace switcher a list of one, and every
collaborative feature in the product rest on a multi-tenancy nothing could
produce.

The invite link is a credential, and it is treated as one. Everything
`services.auth.email` does for a reset link is done here for the same reasons:
only the SHA-256 reaches the database, the raw value is returned exactly once
from the call that mints it, `expires_at` bounds a leak, and redemption is a
one-way door decided by the *database* rather than by a Python `if`.

Two races are possible and both are closed by the storage layer, not by reading
before writing:

* Two clicks on one link. `accept_invite` claims the row with a conditional
  UPDATE whose ``WHERE`` names every terminal state; the rowcount is the
  verdict. The loser sees 0 and is refused, on Postgres as well as SQLite,
  because that predicate is evaluated while the row is locked for writing.
* Two links, or a link and something else, arriving at the same membership.
  `Membership` carries ``UniqueConstraint("workspace_id", "user_id")`` and the
  INSERT is allowed to fail against it; the handler then returns the membership
  the winner created. Acceptance is therefore idempotent without ever asking
  "does this exist yet?" and hoping the answer is still true a line later.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings
from ...models import Membership, User, Workspace, WorkspaceInvite
from .email import OutboundEmail

#: The two roles the product actually has. `Membership.role` and
#: `WorkspaceInvite.role` are free-text columns, so this is the only thing
#: standing between them and an "admin" role that no code anywhere honours —
#: a role that means nothing is worse than no role at all, because the panel
#: showing it implies a restriction that is not there.
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
ROLES = (ROLE_OWNER, ROLE_MEMBER)

#: Long enough to survive a weekend and a forwarded mail, short enough that a
#: link found in an old inbox two months from now is worthless. Deliberately
#: longer than a reset link (30 minutes): the recipient of a reset asked for it
#: seconds ago and is waiting, while an invitee was not expecting anything.
INVITE_TTL = timedelta(days=7)

#: pending -> the link works. The other three are terminal and say why not.
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def invite_status(invite: WorkspaceInvite, *, now: Optional[datetime] = None) -> str:
    """Why the link does or does not work, in the order that matters.

    Accepted before revoked before expired: an invitation that was used and
    *then* revoked was still used, and telling an owner "revoked" about somebody
    who is already sitting in their workspace would be a lie.
    """
    if invite.accepted_at is not None:
        return STATUS_ACCEPTED
    if invite.revoked_at is not None:
        return STATUS_REVOKED
    if invite.expires_at <= (now or utcnow()):
        return STATUS_EXPIRED
    return STATUS_PENDING


def issue_invite(
    db: Session,
    *,
    workspace_id: str,
    email: str,
    role: str,
    invited_by: str,
) -> tuple[WorkspaceInvite, str]:
    """Mint an invitation and return it with its raw token, once.

    Any pending invitation to the same address in the same workspace is revoked
    first, for the reason `issue_email_token` consumes its predecessors: two
    live links are two chances for the older, more-forwarded one to be the one
    that gets used. Re-inviting somebody is therefore also how you *rotate* a
    link you are no longer sure about.

    Does not commit. The caller owns the transaction, so the audit event and the
    invitation land together or not at all.
    """
    now = utcnow()
    db.execute(
        update(WorkspaceInvite)
        .where(
            WorkspaceInvite.workspace_id == workspace_id,
            WorkspaceInvite.email == email,
            WorkspaceInvite.accepted_at.is_(None),
            WorkspaceInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    raw_token = secrets.token_urlsafe(32)
    invite = WorkspaceInvite(
        workspace_id=workspace_id,
        email=email,
        role=role,
        token_hash=hash_token(raw_token),
        invited_by=invited_by,
        expires_at=now + INVITE_TTL,
        created_at=now,
    )
    db.add(invite)
    db.flush()
    return invite, raw_token


def load_invite(db: Session, raw_token: str) -> Optional[WorkspaceInvite]:
    """The row a raw link names, whatever state it is in.

    Terminal invitations are returned rather than hidden so the caller can say
    *why* a link failed. That is safe: reaching this function at all requires
    the 256-bit token, so nothing is disclosed to anyone who was not sent it.
    """
    if not raw_token:
        return None
    return db.scalar(
        select(WorkspaceInvite).where(
            WorkspaceInvite.token_hash == hash_token(raw_token)
        )
    )


def revoke_invite(db: Session, invite: WorkspaceInvite) -> bool:
    """Withdraw a pending invitation. True if this call is what withdrew it.

    Conditional, like the accept claim, so revoking twice — or revoking one an
    invitee is redeeming at that moment — cannot overwrite an acceptance that
    already happened. Does not commit.
    """
    revoked = cast(
        "CursorResult[Any]",
        db.execute(
            update(WorkspaceInvite)
            .where(
                WorkspaceInvite.id == invite.id,
                WorkspaceInvite.accepted_at.is_(None),
                WorkspaceInvite.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        ),
    ).rowcount
    return bool(revoked)


@dataclass(frozen=True)
class Accepted:
    membership: Membership
    #: False when the caller was already in the workspace. The link is still
    #: burnt; nothing about their existing place in it changed.
    joined: bool


def accept_invite(
    db: Session, *, invite: WorkspaceInvite, user: User
) -> Optional[Accepted]:
    """Redeem an invitation for `user`, or return None if it is not redeemable.

    Commits. The claim and the membership are one transaction on purpose: unlike
    `consume_email_token`, whose caller then writes a password, everything this
    function has to do lives in the same database, so there is no window in
    which a burnt link has bought the invitee nothing. Either they are a member
    and the link is spent, or neither.

    The caller has already checked that the invitation is addressed to this
    user. This function checks only what a race can change.
    """
    now = utcnow()
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(WorkspaceInvite)
            .where(
                WorkspaceInvite.id == invite.id,
                WorkspaceInvite.accepted_at.is_(None),
                WorkspaceInvite.revoked_at.is_(None),
                WorkspaceInvite.expires_at > now,
            )
            .values(accepted_at=now)
        ),
    ).rowcount
    if not claimed:
        # Spent, withdrawn, or expired. The empty write transaction is rolled
        # back rather than left holding a lock.
        db.rollback()
        return None

    existing = db.scalar(
        select(Membership).where(
            Membership.workspace_id == invite.workspace_id,
            Membership.user_id == user.id,
        )
    )
    if existing is not None:
        # They were already in. The link is spent — an invitation is answered
        # once — but their role is *not* rewritten to the invited one: changing
        # what somebody can do is `PATCH /api/admin/members/{id}`, an owner-only
        # act with its own audit entry, not a side effect of a link they clicked.
        db.commit()
        return Accepted(membership=existing, joined=False)

    membership = Membership(
        workspace_id=invite.workspace_id, user_id=user.id, role=invite.role
    )
    db.add(membership)
    try:
        db.commit()
    except IntegrityError:
        # Somebody else created this membership between the SELECT above and
        # this flush — a second invitation redeemed in the same instant. The
        # unique constraint is what makes that impossible to get wrong, and the
        # rollback took our claim with it, so re-burn the link and hand back
        # the membership that won.
        db.rollback()
        winner = db.scalar(
            select(Membership).where(
                Membership.workspace_id == invite.workspace_id,
                Membership.user_id == user.id,
            )
        )
        if winner is None:
            # Not the constraint we expected; do not swallow it.
            raise
        db.execute(
            update(WorkspaceInvite)
            .where(
                WorkspaceInvite.id == invite.id,
                WorkspaceInvite.accepted_at.is_(None),
            )
            .values(accepted_at=now)
        )
        db.commit()
        return Accepted(membership=winner, joined=False)
    return Accepted(membership=membership, joined=True)


def count_owners(db: Session, workspace_id: str, *, excluding: str = "") -> int:
    """How many owners this workspace would still have without `excluding`.

    A workspace with no owner is unreachable — nobody can invite, nobody can
    change a role, nobody can see the admin panel — and there is no operator
    above the workspace to repair it, which is the same argument
    `api/auth.py::_create_account` makes for why signup writes an owner row in
    the same transaction as the workspace.
    """
    query = select(Membership).where(
        Membership.workspace_id == workspace_id,
        Membership.role == ROLE_OWNER,
    )
    if excluding:
        query = query.where(Membership.id != excluding)
    return len(db.scalars(query).all())


def invite_url(settings: Settings, raw_token: str) -> str:
    return f"{settings.primary_web_origin}/auth/invite?token={raw_token}"


def invite_email(
    settings: Settings,
    *,
    to: str,
    workspace_name: str,
    inviter_name: str,
    raw_token: str,
) -> OutboundEmail:
    """The invitation itself.

    Names the workspace and the person who sent it, because the recipient was
    not expecting this mail and "someone has invited you somewhere" is
    indistinguishable from phishing. Says nothing about whether the address has
    an account here: unlike signup and reset, this message is only ever sent to
    an address an owner typed, so it is not an enumeration surface — but there
    is still nothing useful it could say.
    """
    days = int(INVITE_TTL.total_seconds() // 86400)
    inviter = inviter_name or "Someone"
    return OutboundEmail(
        to=to,
        subject=f"{inviter} invited you to {workspace_name}",
        body=(
            f"{inviter} has invited you to join the {workspace_name} workspace.\n"
            f"Accept the invitation (expires in {days} days):\n"
            f"{invite_url(settings, raw_token)}\n"
        ),
    )


def workspace_name(db: Session, workspace_id: str) -> str:
    workspace = db.get(Workspace, workspace_id)
    return workspace.name if workspace is not None else ""
