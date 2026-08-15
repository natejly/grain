"""Signup, login, logout, password reset, email verification, Google login.

Two rules shape almost every response in this file:

1. **Nothing here says whether an address has an account.** Signup and password
   reset answer identically for known and unknown addresses, and login answers
   identically for "no such user", "wrong password" and "locked out". The truth
   goes to the mailbox, which only the owner can read.
2. **Nothing here trusts the client about identity.** The only inputs are a
   password, a single-use emailed token, or an authorization code redeemed
   directly with Google over TLS.
"""
from __future__ import annotations

import logging
import secrets
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import get_db
from ..models import Agent, Membership, User, UserIdentity, UserSession, Workspace
from ..schemas import (
    AuthAcknowledgement,
    AuthSessionOut,
    DevOverrideOut,
    InviteAcceptOut,
    InvitePreviewOut,
    InviteTokenIn,
    LoginIn,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    PlaygroundOut,
    SignupIn,
    VerifyEmailIn,
    WorkspaceMembershipOut,
)
from ..services.audit import record_audit
from ..services.auth import email as email_service
from ..services.auth import invites, oauth
from ..services.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)
from ..services.auth.ratelimit import auth_rate_limiter
from ..services.auth.sessions import (
    clear_session_cookie,
    create_session,
    resolve_session,
    revoke_all_sessions,
    revoke_session,
    rotate_session,
    set_session_cookie,
)

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# One sentence for every signup and reset outcome. Copy it, do not branch on it.
GENERIC_EMAIL_ACK = "If that address can receive mail, we have sent it a message."
INVALID_CREDENTIALS = "Invalid email or password"

DEFAULT_AGENT_INSTRUCTIONS = (
    "Answer from workspace evidence and request approval before tools."
)


def _client_ip(request: Request) -> str:
    """The peer address, never an X-Forwarded-For header.

    A forwarded header is client-controlled, so honouring it would let one
    attacker spend everybody else's rate-limit budget — or dodge their own by
    rotating a string. Behind a reverse proxy this collapses to the proxy's
    address and the limiter becomes global; the per-account lockout in
    `users.locked_until` is the part that still bites in that deployment.
    """
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, bucket: str, settings: Settings) -> None:
    key = f"{bucket}:{_client_ip(request)}"
    if not auth_rate_limiter.allow(
        key,
        limit=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Try again later.",
        )


# Address handling lives in the mail service, not here, because the invite flow
# has to reach exactly the same answers: an invitation is matched against a
# `users.email` this module normalised on the way in, and two normalisers would
# eventually disagree about whether an address is the address it was sent to.
_send_quietly = email_service.send_quietly
_normalize_email = email_service.normalize_email
_looks_like_email = email_service.looks_like_email


def _create_account(
    db: Session,
    *,
    email: str,
    name: str,
    password_hash: Optional[str],
    email_verified: bool,
) -> Tuple[User, Workspace]:
    """User + personal workspace + owner membership + starter agent, atomically.

    One flush, one transaction: a user with no workspace cannot log in anywhere,
    and a workspace with no owner is unreachable, so a partial commit here is a
    broken account either way. The caller commits.
    """
    now = utcnow()
    user = User(
        email=email,
        name=name or email.split("@")[0],
        password_hash=password_hash,
        email_verified_at=now if email_verified else None,
        status="active",
    )
    db.add(user)
    db.flush()
    workspace = Workspace(name=f"{user.name}'s workspace")
    db.add(workspace)
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    db.add(
        Agent(
            workspace_id=workspace.id,
            name="Research partner",
            instructions=DEFAULT_AGENT_INSTRUCTIONS,
        )
    )
    return user, workspace


def _session_out(
    db: Session, actor: Actor, csrf_token: str, user: Optional[User] = None
) -> AuthSessionOut:
    subject = user or db.get(User, actor.user_id)
    return AuthSessionOut(
        user_id=actor.user_id,
        user_name=actor.user_name,
        user_email=actor.user_email,
        email_verified=bool(subject is not None and subject.email_verified_at),
        workspace_id=actor.workspace_id,
        workspace_name=actor.workspace_name,
        role=actor.role,
        csrf_token=csrf_token,
    )


def _primary_workspace(db: Session, user_id: str) -> Tuple[Workspace, Membership]:
    membership = db.scalar(
        select(Membership)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at, Membership.id)
    )
    if membership is None:
        # Every account gets a workspace at creation, so this means the data is
        # broken rather than the caller.
        raise HTTPException(status_code=403, detail="Workspace access denied")
    workspace = db.get(Workspace, membership.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=403, detail="Workspace access denied")
    return workspace, membership


def _issue_login(
    db: Session,
    request: Request,
    response: Response,
    user: User,
    settings: Settings,
) -> AuthSessionOut:
    """Start a session for `user` and attach the cookie to `response`.

    Any session the caller already holds is revoked first. Logging in is a
    privilege change, so the token that existed before it must not survive it —
    otherwise an attacker who planted a session cookie in the victim's browser
    still holds a valid token after the victim signs in (session fixation).

    Deliberately does not require ``email_verified_at``. Signing in with your own
    password proves what login needs to prove; the mailbox proof is a *second*
    factor that only matters where an address is being taken as identity, which
    is the Google account-linking branch below and nowhere else. Gating login on
    it as well would make every session depend on outbound mail, converting an
    SMTP outage into a total lockout that no one locked out can self-service.
    The flag is reported on ``AuthSessionOut.email_verified`` so the state is
    visible rather than silent.
    """
    existing_token = request.cookies.get(settings.session_cookie_name, "")
    existing = (
        resolve_session(db, existing_token, settings=settings) if existing_token else None
    )
    user_agent = request.headers.get("user-agent", "")
    ip_address = _client_ip(request)
    if existing is not None and existing.user_id == user.id:
        issued = rotate_session(
            db, existing, settings=settings, user_agent=user_agent, ip_address=ip_address
        )
    else:
        if existing is not None:
            # Signing in as somebody else on a browser that still holds another
            # account's session: that token must not survive the switch.
            revoke_session(db, existing)
        issued = create_session(
            db,
            user_id=user.id,
            settings=settings,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    workspace, membership = _primary_workspace(db, user.id)
    set_session_cookie(response, issued, settings)
    actor = Actor(
        user_id=user.id,
        user_name=user.name,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        role=membership.role,
        session_id=issued.session.id,
        user_email=user.email,
    )
    record_audit(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="auth.session.started",
        resource_type="user_session",
        resource_id=issued.session.id,
        detail={"user_agent": request.headers.get("user-agent", "")[:120]},
    )
    return _session_out(db, actor, issued.csrf_token, user)


@router.post(
    "/signup",
    response_model=AuthAcknowledgement,
    status_code=status.HTTP_202_ACCEPTED,
)
def signup(
    payload: SignupIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthAcknowledgement:
    _rate_limit(request, "signup", settings)
    email = _normalize_email(payload.email)
    if not _looks_like_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    try:
        validate_password(
            payload.password,
            min_length=settings.password_min_length,
            max_length=settings.password_max_length,
        )
    except PasswordPolicyError as error:
        # Safe to be specific: this is about the password the caller just typed,
        # and reveals nothing about whether the address is registered.
        raise HTTPException(status_code=400, detail=str(error)) from error

    # Hashed before the existence check, so the ~50ms of argon2 work happens on
    # both paths. Skipping it for known addresses would make response time the
    # enumeration oracle the identical body is meant to prevent.
    password_hash = hash_password(payload.password)
    sender = email_service.get_email_sender(settings)

    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        _send_quietly(sender, email_service.account_exists_email(settings, to=email))
        return AuthAcknowledgement(detail=GENERIC_EMAIL_ACK)

    try:
        user, workspace = _create_account(
            db,
            email=email,
            name=payload.name.strip(),
            password_hash=password_hash,
            email_verified=False,
        )
        raw_token = email_service.issue_email_token(
            db,
            user_id=user.id,
            purpose=email_service.PURPOSE_VERIFY_EMAIL,
            ttl=email_service.VERIFY_TOKEN_TTL,
        )
        record_audit(
            db,
            workspace_id=workspace.id,
            actor_id=user.id,
            action="auth.account.created",
            resource_type="user",
            resource_id=user.id,
            detail={"method": "password"},
        )
        db.commit()
    except IntegrityError:
        # Lost the race against a concurrent signup for the same address. The
        # unique index is what actually prevents two accounts; this branch just
        # keeps the answer identical.
        db.rollback()
        return AuthAcknowledgement(detail=GENERIC_EMAIL_ACK)

    _send_quietly(
        sender, email_service.verification_email(settings, to=email, raw_token=raw_token)
    )
    return AuthAcknowledgement(detail=GENERIC_EMAIL_ACK)


@router.post("/login", response_model=AuthSessionOut)
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthSessionOut:
    _rate_limit(request, "login", settings)
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    now = utcnow()
    locked = (
        user is not None
        and user.locked_until is not None
        and user.locked_until > now
    )

    # A locked or unknown account still pays for a hash comparison against the
    # module's dummy hash, so "locked", "unknown" and "wrong password" cost the
    # same and answer the same. A lockout that announces itself is an
    # enumeration oracle wearing a safety vest.
    stored_hash = None if (user is None or locked) else user.password_hash
    check = verify_password(stored_hash, payload.password)

    if user is None or locked or not check.ok or user.status != "active":
        if user is not None and not locked:
            user.failed_logins += 1
            if user.failed_logins >= settings.login_max_failures:
                user.locked_until = now + settings.lockout_duration
                user.failed_logins = 0
            db.commit()
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    user.failed_logins = 0
    user.locked_until = None
    if check.needs_rehash:
        # The only moment we legitimately hold the plaintext: upgrade the stored
        # parameters now rather than leaving the row on the old cost forever.
        user.password_hash = hash_password(payload.password)
    session_out = _issue_login(db, request, response, user, settings)
    db.commit()
    return session_out


@router.get("/dev-override", response_model=DevOverrideOut)
def dev_override_status(
    settings: Settings = Depends(get_settings),
) -> DevOverrideOut:
    handle = settings.dev_override_handle
    return DevOverrideOut(enabled=bool(handle), handle=handle)


@router.post("/dev-login", response_model=AuthSessionOut)
def dev_login(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthSessionOut:
    """One-click local sign-in for DEV_USER. Unreachable outside development."""
    handle = settings.dev_override_handle
    if not handle:
        raise HTTPException(status_code=404, detail="Dev sign-in is not enabled")
    _rate_limit(request, "dev-login", settings)
    email = f"{handle}@local.dev"
    display_name = handle[:1].upper() + handle[1:]
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user, workspace = _create_account(
            db,
            email=email,
            name=display_name,
            password_hash=None,
            email_verified=True,
        )
        record_audit(
            db,
            workspace_id=workspace.id,
            actor_id=user.id,
            action="auth.account.created",
            resource_type="user",
            resource_id=user.id,
            detail={"method": "dev_override", "handle": handle},
        )
        db.commit()
    elif user.status != "active":
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
    session_out = _issue_login(db, request, response, user, settings)
    db.commit()
    return session_out


@router.get("/playground", response_model=PlaygroundOut)
def playground_status(
    settings: Settings = Depends(get_settings),
) -> PlaygroundOut:
    """Whether the anonymous "try it" mode is available.

    Public and unauthenticated like ``dev_override_status``: the login screen
    needs to know whether to render the button before any session exists. Kept
    off /api/bootstrap, which is auth-gated and unreachable pre-login.
    """
    return PlaygroundOut(enabled=settings.playground_enabled)


@router.post("/playground", response_model=AuthSessionOut)
def playground_login(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthSessionOut:
    """Mint an ephemeral, fully-isolated guest session — only when enabled.

    Isolation is structural, not bespoke. ``_create_account`` seeds a throwaway
    ``User`` + personal ``Workspace`` + owner ``Membership`` — the same primitive
    signup uses — and ``_issue_login`` mints a normal session over it. Every
    request therefore scopes the guest through the exact ``get_actor`` /
    ``_resolve_workspace`` membership fence every real tenant passes: the guest
    owns one fresh workspace and ``_resolve_workspace`` only ever returns a
    workspace it holds a membership in, so a guest can no more read another
    tenant's data than two real tenants can read each other's. ``password_hash``
    is NULL — ``verify_password`` refuses a null hash, so nothing can ever
    password-login to a guest account (the same stance as Google and dev-seed
    accounts), and there is no promotion path to a credentialed user.
    """
    if not settings.playground_enabled:
        raise HTTPException(status_code=404, detail="Playground is not enabled")
    _rate_limit(request, "playground", settings)
    # A hard ceiling on live guests. Each call seeds a throwaway workspace and
    # nothing cascades on delete, so per-IP rate limiting alone (which collapses
    # to one bucket behind a reverse proxy) is not a real bound on row creation.
    # At capacity the guest is turned away rather than the database grown.
    live_guests = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.like("%@playground.local"))
        )
        or 0
    )
    if live_guests >= settings.playground_max_sessions:
        raise HTTPException(
            status_code=429,
            detail="The playground is at capacity right now — try again shortly.",
        )
    handle = secrets.token_hex(8)
    user, workspace = _create_account(
        db,
        email=f"playground-{handle}@playground.local",
        name="Playground guest",
        password_hash=None,
        email_verified=False,
    )
    record_audit(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action="auth.account.created",
        resource_type="user",
        resource_id=user.id,
        detail={"method": "playground"},
    )
    session_out = _issue_login(db, request, response, user, settings)
    db.commit()
    return session_out


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Depends(get_actor), so an unsafe method without the CSRF header is a 403
    # before anything is revoked: a cross-site page must not be able to log
    # somebody out any more than it can act as them.
    if actor.session_id:
        session = db.get(UserSession, actor.session_id)
        if session is not None:
            revoke_session(db, session)
            db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.get("/me", response_model=AuthSessionOut)
def me(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> AuthSessionOut:
    """The current identity, plus the CSRF token for this session.

    The session cookie is HttpOnly, so this is how a freshly loaded page learns
    the double-submit value it must echo. CORS keeps the response readable only
    from the configured web origins.
    """
    csrf_token = ""
    if actor.session_id:
        session = db.get(UserSession, actor.session_id)
        csrf_token = session.csrf_secret if session is not None else ""
    return _session_out(db, actor, csrf_token)


@router.get("/workspaces", response_model=List[WorkspaceMembershipOut])
def list_workspaces(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[WorkspaceMembershipOut]:
    """The workspaces the caller belongs to, for the switcher to offer.

    Discovery only. ``X-Workspace-Id`` was already checked against `memberships`
    before it was believed, so this endpoint grants nothing new — it just stops
    a user with three workspaces from having to guess two of the ids. The query
    is keyed on the caller's own user id, so it cannot list a workspace they are
    not in; `tests/test_auth_boundaries.py` pins that a second user sees none of
    the first's.

    Ordered by membership age, exactly as `_resolve_workspace` orders its
    default pick, so "the first one" names the same row in both places.
    """
    rows = db.execute(
        select(Workspace, Membership)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == actor.user_id)
        .order_by(Membership.created_at, Membership.id)
    ).all()
    return [
        WorkspaceMembershipOut(
            id=workspace.id,
            name=workspace.name,
            role=membership.role,
            is_current=workspace.id == actor.workspace_id,
        )
        for workspace, membership in rows
    ]


# --------------------------------------------------------------------------
# Invitations, from the invitee's side. The owner's half lives in api/admin.py.
#
# Both routes take the token in the *body*. A GET with the token in the path
# would put a credential into every access log, proxy trace and Referer header
# between the browser and here — the same reason `verify-email` and
# `login-link/consume` are POSTs over a body rather than the links themselves.

#: Why a link did not work, in words the person holding it can act on. Being
#: specific is safe here in a way it is not on login: there is no address to
#: enumerate, and reaching any of these already required the token.
INVITE_STATUS_DETAIL = {
    invites.STATUS_ACCEPTED: "That invitation has already been accepted.",
    invites.STATUS_REVOKED: "That invitation was withdrawn.",
    invites.STATUS_EXPIRED: "That invitation has expired. Ask for a new one.",
}
INVITE_UNKNOWN = "That invitation link is not valid."


@router.post("/invites/preview", response_model=InvitePreviewOut)
def preview_invite(
    payload: InviteTokenIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> InvitePreviewOut:
    """What this link is for. Deliberately unauthenticated.

    An invitee who has never been here has no session and cannot sensibly get
    one without knowing what they are signing up for, so the page has to render
    "X invited you to Y, as a member" before any account exists. Rate limited
    like every other unauthenticated token endpoint in this file: the token
    space is 256 bits and unguessable, and the limiter is what keeps that from
    being merely a probability argument.
    """
    _rate_limit(request, "invite-preview", settings)
    invite = invites.load_invite(db, payload.token)
    if invite is None:
        raise HTTPException(status_code=404, detail=INVITE_UNKNOWN)
    inviter = db.get(User, invite.invited_by) if invite.invited_by else None
    return InvitePreviewOut(
        workspace_name=invites.workspace_name(db, invite.workspace_id),
        email=invite.email,
        role=invite.role,
        status=invites.invite_status(invite),
        invited_by_name=inviter.name if inviter is not None else "",
        expires_at=invite.expires_at,
    )


@router.post("/invites/accept", response_model=InviteAcceptOut)
def accept_invite(
    payload: InviteTokenIn,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> InviteAcceptOut:
    """Redeem an invitation for the signed-in caller.

    A session is required — this writes a `Membership`, which names a user, and
    the only trustworthy source of "which user" is the session cookie. It is
    also what supplies the CSRF check, so a cross-site page cannot walk somebody
    into a workspace they never chose to join.

    The invited *address* must be the caller's own. The token alone would be an
    argument for admission — it went to one mailbox — but requiring both means a
    link forwarded, quoted in a ticket, or read off a screenshot is not a
    workspace: whoever holds it must also hold the account it names. The cost is
    one clear refusal for somebody signed in as the wrong account, which the
    page can tell them how to fix.

    Everything a race can change is checked inside `invites.accept_invite`,
    where the database rather than a Python `if` decides who won.
    """
    _rate_limit(request, "invite-accept", settings)
    invite = invites.load_invite(db, payload.token)
    if invite is None:
        raise HTTPException(status_code=404, detail=INVITE_UNKNOWN)
    state = invites.invite_status(invite)
    if state != invites.STATUS_PENDING:
        raise HTTPException(status_code=400, detail=INVITE_STATUS_DETAIL[state])

    user = db.get(User, actor.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=401, detail="Not authenticated")
    if invite.email != _normalize_email(user.email):
        raise HTTPException(
            status_code=403,
            detail=(
                f"That invitation was sent to {invite.email}. Sign in with that "
                "address to accept it."
            ),
        )

    accepted = invites.accept_invite(db, invite=invite, user=user)
    if accepted is None:
        # Between the status check above and the claim, somebody else spent or
        # withdrew it. The conditional UPDATE is what noticed; this is the only
        # place that can report it.
        raise HTTPException(
            status_code=409, detail="That invitation was just used or withdrawn."
        )
    record_audit(
        db,
        workspace_id=invite.workspace_id,
        actor_id=user.id,
        action="invite.accepted",
        resource_type="workspace_invite",
        resource_id=invite.id,
        detail={"role": accepted.membership.role, "joined": accepted.joined},
    )
    db.commit()
    return InviteAcceptOut(
        workspace_id=invite.workspace_id,
        workspace_name=invites.workspace_name(db, invite.workspace_id),
        role=accepted.membership.role,
        joined=accepted.joined,
    )


@router.post(
    "/password/reset/request",
    response_model=AuthAcknowledgement,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_password_reset(
    payload: PasswordResetRequestIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthAcknowledgement:
    _rate_limit(request, "password-reset", settings)
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    sender = email_service.get_email_sender(settings)
    if user is not None and user.status == "active":
        raw_token = email_service.issue_email_token(
            db,
            user_id=user.id,
            purpose=email_service.PURPOSE_PASSWORD_RESET,
            ttl=email_service.RESET_TOKEN_TTL,
        )
        db.commit()
        message = email_service.password_reset_email(
            settings, to=email, raw_token=raw_token
        )
    else:
        # Both branches send exactly one message, for the same reason signup
        # does: with SMTP, "sent a mail" and "did nothing" differ by a network
        # round trip, and a caller with a stopwatch reads that difference as
        # "this address is registered". Measured at 59ms vs 2ms before this
        # branch existed — a 29x oracle behind a byte-identical body.
        message = email_service.no_account_email(settings, to=email)
    _send_quietly(sender, message)
    return AuthAcknowledgement(detail=GENERIC_EMAIL_ACK)


@router.post("/password/reset/confirm", response_model=AuthAcknowledgement)
def confirm_password_reset(
    payload: PasswordResetConfirmIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthAcknowledgement:
    _rate_limit(request, "password-reset-confirm", settings)
    try:
        validate_password(
            payload.password,
            min_length=settings.password_min_length,
            max_length=settings.password_max_length,
        )
    except PasswordPolicyError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    token = email_service.consume_email_token(
        db, raw_token=payload.token, purpose=email_service.PURPOSE_PASSWORD_RESET
    )
    if token is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user = db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user.password_hash = hash_password(payload.password)
    # Redeeming the link proved control of the mailbox, which is the same proof
    # verification asks for, and it clears any lockout the attacker's failed
    # guesses caused — otherwise a spray could keep the real owner locked out.
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    user.failed_logins = 0
    user.locked_until = None
    # Every other device is logged out: a reset is what you do when you think
    # someone else has your account.
    revoke_all_sessions(db, user.id)
    db.commit()
    return AuthAcknowledgement(detail="Password updated. Sign in with your new password.")


@router.post(
    "/login-link/request",
    response_model=AuthAcknowledgement,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_login_link(
    payload: PasswordResetRequestIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthAcknowledgement:
    """Email a passwordless sign-in link, without saying whether the account exists.

    A byte-for-byte sibling of ``request_password_reset``: the same
    deliberately-uninformative acknowledgement, the same 202, and exactly one
    message on both branches so response *time* is not the enumeration oracle the
    identical body hides. The only differences from reset are the token purpose
    and TTL — the single-use/expiry/hash-at-rest mechanism is reused verbatim.
    """
    _rate_limit(request, "login-link", settings)
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    sender = email_service.get_email_sender(settings)
    if user is not None and user.status == "active":
        raw_token = email_service.issue_email_token(
            db,
            user_id=user.id,
            purpose=email_service.PURPOSE_LOGIN,
            ttl=email_service.LOGIN_TOKEN_TTL,
        )
        db.commit()
        message = email_service.login_link_email(settings, to=email, raw_token=raw_token)
    else:
        # Same non-oracle discipline as the reset request: the unknown branch
        # still sends one message, so "issued a link" and "did nothing" cost the
        # same network round trip and read identically to a caller with a
        # stopwatch. Reuses the reset flow's no-account copy.
        message = email_service.no_account_email(settings, to=email)
    _send_quietly(sender, message)
    return AuthAcknowledgement(detail=GENERIC_EMAIL_ACK)


@router.post("/login-link/consume", response_model=AuthSessionOut)
def consume_login_link(
    payload: VerifyEmailIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthSessionOut:
    """Redeem a sign-in link and mint a session, exactly like login.

    The consume half models ``confirm_password_reset`` — the same single-use
    conditional UPDATE, committed on its own before anything is minted, so a
    burnt link is a one-way door; a spent, expired, or unknown link 400s with the
    same generic detail, never a distinct code that would leak which. The
    session-mint half is ``_issue_login`` verbatim (fixation-safe rotation,
    cookie, audit). Like ``_issue_login`` and unlike ``login`` it does not
    require ``email_verified_at``: redeeming a link emailed to the address proves
    mailbox control, which is exactly the proof login accepts. A non-active
    account is rejected with the same generic 400 so status is not an oracle.
    """
    _rate_limit(request, "login-link-consume", settings)
    token = email_service.consume_email_token(
        db, raw_token=payload.token, purpose=email_service.PURPOSE_LOGIN
    )
    if token is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user = db.get(User, token.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    session_out = _issue_login(db, request, response, user, settings)
    db.commit()
    return session_out


@router.post("/verify-email", response_model=AuthAcknowledgement)
def verify_email(
    payload: VerifyEmailIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthAcknowledgement:
    _rate_limit(request, "verify-email", settings)
    token = email_service.consume_email_token(
        db, raw_token=payload.token, purpose=email_service.PURPOSE_VERIFY_EMAIL
    )
    if token is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user = db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    db.commit()
    return AuthAcknowledgement(detail="Email verified.")


@router.get("/google/start")
def google_start(
    request: Request, settings: Settings = Depends(get_settings)
) -> RedirectResponse:
    _rate_limit(request, "google-start", settings)
    if not settings.google_login_ready:
        raise HTTPException(status_code=503, detail="Google login is not configured")
    state = oauth.new_state()
    response = RedirectResponse(
        oauth.authorize_url(settings, state), status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
    oauth.set_state_cookie(response, state, settings)
    return response


def _oauth_failure(settings: Settings, reason: str) -> RedirectResponse:
    """Send the browser back to the app with a machine-readable reason.

    The callback is a top-level navigation, so a JSON error would strand the
    user on the API's domain looking at a stack of braces.
    """
    response = RedirectResponse(
        f"{settings.post_login_redirect_url}/?auth_error={reason}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    oauth.clear_state_cookie(response, settings)
    return response


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if error or not code:
        return _oauth_failure(settings, "denied")
    # The state check *is* the CSRF defence for this flow. Without it an
    # attacker can hand a victim a callback URL carrying the attacker's own
    # authorization code and silently sign the victim into the attacker's
    # account — or, on a linking flow, attach their identity to the victim's.
    if not oauth.state_matches(request.cookies.get(oauth.STATE_COOKIE_NAME), state):
        return _oauth_failure(settings, "state")
    try:
        profile = oauth.fetch_google_profile(code, settings)
    except oauth.OAuthError:
        return _oauth_failure(settings, "exchange")

    identity = db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == oauth.PROVIDER_GOOGLE,
            UserIdentity.subject == profile.subject,
        )
    )
    user: Optional[User]
    if identity is not None:
        # Matched on the provider's subject, which is what makes a Google
        # account address change harmless here.
        user = db.get(User, identity.user_id)
        if user is None or user.status != "active":
            return _oauth_failure(settings, "account")
        if profile.email and identity.email != profile.email:
            identity.email = profile.email
    else:
        existing = (
            db.scalar(select(User).where(User.email == profile.email))
            if profile.email
            else None
        )
        if existing is not None:
            # An address collision is not proof of ownership. Linking is only
            # safe when both sides have independently proved control of that
            # mailbox: Google says it verified the address, and our own account
            # completed email verification. Anything less would let a Google
            # account with an unverified address claim a local account.
            if not (profile.email_verified and existing.email_verified_at is not None):
                return _oauth_failure(settings, "account_exists")
            user = existing
            db.add(
                UserIdentity(
                    user_id=user.id,
                    provider=oauth.PROVIDER_GOOGLE,
                    subject=profile.subject,
                    email=profile.email,
                )
            )
        else:
            if not profile.email:
                return _oauth_failure(settings, "no_email")
            # password_hash stays NULL: this account has no password, and
            # verify_password() refuses a null hash, so nothing can log into it
            # with a guessed or empty password.
            user, workspace = _create_account(
                db,
                email=profile.email,
                name=profile.name,
                password_hash=None,
                email_verified=profile.email_verified,
            )
            db.add(
                UserIdentity(
                    user_id=user.id,
                    provider=oauth.PROVIDER_GOOGLE,
                    subject=profile.subject,
                    email=profile.email,
                )
            )
            record_audit(
                db,
                workspace_id=workspace.id,
                actor_id=user.id,
                action="auth.account.created",
                resource_type="user",
                resource_id=user.id,
                detail={"method": "google"},
            )

    response = RedirectResponse(
        settings.post_login_redirect_url, status_code=status.HTTP_303_SEE_OTHER
    )
    _issue_login(db, request, response, user, settings)
    db.commit()
    oauth.clear_state_cookie(response, settings)
    return response
