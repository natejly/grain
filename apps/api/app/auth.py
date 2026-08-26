"""Who is calling, and which workspace they are allowed to touch.

This module used to read ``X-User-Id`` and ``X-Workspace-Id`` headers *with
defaults*, which meant any client could claim any identity in any workspace and
a client that claimed nothing was still handed the seed owner. That is gone.
Identity now comes from an opaque session cookie and nowhere else:

    cookie -> UserSession (live) -> User (active) -> Membership -> Workspace

Every step fails closed. No cookie, an unknown cookie, an expired or revoked
session, a disabled user, or a user with no membership in the workspace they
named all raise 401/403. There is no branch that produces an Actor without a
membership row backing it.

``X-Workspace-Id`` survives as a *selection*, not a claim: a user who belongs to
several workspaces says which one this request is about, and the header is
checked against ``memberships`` before it is believed. A workspace the caller is
not a member of is a 403 whether or not it exists.

CSRF is enforced here too, on unsafe methods, because the session cookie must be
``SameSite=None`` to cross from the Vercel-hosted web app to this API. SameSite
is what normally makes CSRF impossible for free; with it set to None, a
cross-site form post *will* carry the cookie, so the request must also prove it
came from our own JavaScript. It does that by echoing the session's
``csrf_secret`` in a header — a browser will send the cookie cross-site on its
own, but nothing can make it copy that secret into a header on an attacker's
page (reading it requires same-origin JS, which CORS denies). GET/HEAD/OPTIONS
are exempt because they must not change state; a GET that mutates is the bug,
not the missing header. Enforcing it inside ``get_actor`` means every route that
requires an identity is covered by construction — and
``tests/test_auth_boundaries.py`` fails the build if a state-changing route is
ever added without one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import ORG_ADMIN, Agent, Membership, Tool, ToolGrant, User, Workspace
from .services import api_tokens, orgs
from .services.auth.sessions import csrf_token_matches, resolve_session

# Fixed ids for the rows `seed_dev_workspace` creates. They are a development
# *seed* — data to look at — and no longer an identity anything trusts: the
# seeded user has no password hash, so it cannot log in, and reaching it over
# HTTP still requires a real session row (the test suite creates one, and
# DEV_AUTO_LOGIN creates one for the browser suite). See the note on
# `seed_dev_workspace` for why the old DEFAULT_* names had to go.
DEV_SEED_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
DEV_SEED_USER_ID = "00000000-0000-4000-8000-000000000002"
DEV_SEED_AGENT_ID = "00000000-0000-4000-8000-000000000003"
DEV_SEED_TOOL_ID = "00000000-0000-4000-8000-000000000004"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


@dataclass(frozen=True)
class Actor:
    user_id: str
    user_name: str
    workspace_id: str
    workspace_name: str
    role: str
    #: The org governing `workspace_id`, and this caller's standing in it. "" for
    #: `org_role` means "not a member of this org", which is a real and expected
    #: state — a contractor invited into one workspace is governed by its org
    #: without belonging to it — and is exactly what `require_org_admin` refuses.
    #:
    #: Deliberately *not* derived from `role`: a workspace owner has no org
    #: standing by virtue of being an owner, and if these two ever fed each other
    #: the tier would collapse back into the one it sits above.
    organization_id: str = ""
    org_role: str = ""
    # None only for the DEV_AUTO_LOGIN fallback, which has no session row.
    session_id: Optional[str] = None
    user_email: str = ""


def seed_dev_workspace(db: Session, settings: Optional[Settings] = None) -> None:
    """Create the demo workspace, user, agent and tool for local development.

    Kept, but demoted. The dangerous half of the old helper was not the rows, it
    was that ``get_actor`` handed that user out to anonymous callers; the rows
    themselves are just a workspace with something in it, and deleting them
    would mean `make dev` and both test suites start against an empty database.

    What changed: it is named for what it is, it refuses to run outside
    development/test (guarded like ``_guard_model_provider``), and the user it
    creates has ``password_hash`` NULL — so even if these rows reached
    production they would authenticate nobody.
    """
    settings = settings or get_settings()
    if not settings.is_dev_env:
        raise RuntimeError(
            "seed_dev_workspace requires APP_ENV to be development or test"
        )
    user = db.get(User, DEV_SEED_USER_ID)
    if user is None:
        db.add(
            User(
                id=DEV_SEED_USER_ID,
                email="demo@example.com",
                name="Nate",
                # No password, deliberately: the seed is data, not a credential.
                password_hash=None,
            )
        )
        db.flush()
    workspace = db.get(Workspace, DEV_SEED_WORKSPACE_ID)
    if workspace is None:
        # The seed models a real account, so it gets a real org with a real admin
        # rather than leaning on the orphan-adoption floor in `models`. A dev
        # poking at org configuration should be able to, exactly as a signed-up
        # user can; a dev workspace whose org nobody administers would make the
        # whole tier untestable by hand.
        org = orgs.provision_org(
            db, name="Acme Knowledge Lab", founder_id=DEV_SEED_USER_ID
        )
        db.add(
            Workspace(
                id=DEV_SEED_WORKSPACE_ID,
                organization_id=org.id,
                name="Acme Knowledge Lab",
            )
        )
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == DEV_SEED_WORKSPACE_ID,
            Membership.user_id == DEV_SEED_USER_ID,
        )
    )
    if membership is None:
        db.add(
            Membership(
                workspace_id=DEV_SEED_WORKSPACE_ID,
                user_id=DEV_SEED_USER_ID,
                role="owner",
            )
        )
    agent = db.get(Agent, DEV_SEED_AGENT_ID)
    if agent is None:
        db.add(
            Agent(
                id=DEV_SEED_AGENT_ID,
                workspace_id=DEV_SEED_WORKSPACE_ID,
                name="Research partner",
                instructions="Answer from workspace evidence and request approval before tools.",
            )
        )
    tool = db.get(Tool, DEV_SEED_TOOL_ID)
    if tool is None:
        db.add(
            Tool(
                id=DEV_SEED_TOOL_ID,
                workspace_id=DEV_SEED_WORKSPACE_ID,
                name="github-zen",
                description="Fetch the public GitHub Zen message",
                base_url="https://api.github.com/zen",
                requires_approval=True,
            )
        )
    grant = db.scalar(
        select(ToolGrant).where(
            ToolGrant.agent_id == DEV_SEED_AGENT_ID,
            ToolGrant.tool_id == DEV_SEED_TOOL_ID,
        )
    )
    if grant is None:
        db.add(
            ToolGrant(
                workspace_id=DEV_SEED_WORKSPACE_ID,
                agent_id=DEV_SEED_AGENT_ID,
                tool_id=DEV_SEED_TOOL_ID,
            )
        )
    db.commit()


def _unauthenticated() -> HTTPException:
    # One message for every failure to resolve a session: "no cookie", "expired"
    # and "revoked" are the same answer to the caller, and a specific one would
    # tell an attacker which of their guesses was closest.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
    )


def _resolve_workspace(
    db: Session, user: User, requested_workspace_id: Optional[str]
) -> tuple[Workspace, Membership]:
    """Pick the workspace for this request and prove membership in it."""
    query = select(Membership).where(Membership.user_id == user.id)
    if requested_workspace_id:
        membership = db.scalar(
            query.where(Membership.workspace_id == requested_workspace_id)
        )
    else:
        # No selection: the workspace they have been in longest, which for the
        # common single-workspace account is simply "theirs".
        membership = db.scalar(query.order_by(Membership.created_at, Membership.id))
    if membership is None:
        raise HTTPException(status_code=403, detail="Workspace access denied")
    workspace = db.get(Workspace, membership.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=403, detail="Workspace access denied")
    return workspace, membership


def _actor_for(
    db: Session,
    user: User,
    workspace: Workspace,
    membership: Membership,
    *,
    session_id: Optional[str],
) -> Actor:
    """One Actor, built the same way on both doors.

    Both call sites had to grow the two organization fields, and two hand-written
    constructors that must stay identical is how one of them ends up with an empty
    `org_role` and an org gate that silently passes on the dev door. One
    constructor, two callers, and the only thing they differ in is the session.
    """
    return Actor(
        user_id=user.id,
        user_name=user.name,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        role=membership.role,
        # The org that governs the workspace this request is about — not "the
        # user's org", which is not a thing: a person in two workspaces may be
        # under two different postures, and which one applies is decided by what
        # they are touching, exactly as `role` already is.
        organization_id=workspace.organization_id,
        org_role=orgs.role_in_org(
            db, organization_id=workspace.organization_id, user_id=user.id
        ),
        session_id=session_id,
        user_email=user.email,
    )


def _dev_fallback_actor(db: Session, settings: Settings) -> Actor:
    """The DEV_AUTO_LOGIN door. Unreachable unless APP_ENV is development/test.

    Settings refuse to construct with DEV_AUTO_LOGIN outside those two
    environments, so this cannot be switched on in a deployment even by
    accident; the check below is the belt to that suspenders.

    It exists for one reason: the browser suite drives a web app that has no
    login screen yet. Requests taking this path are exempt from the CSRF check
    because there is no session, and therefore no csrf_secret to echo.
    """
    if not settings.dev_auto_login or not settings.is_dev_env:
        raise _unauthenticated()
    user = db.get(User, DEV_SEED_USER_ID)
    if user is None:
        raise _unauthenticated()
    workspace, membership = _resolve_workspace(db, user, DEV_SEED_WORKSPACE_ID)
    return _actor_for(db, user, workspace, membership, session_id=None)


def get_actor(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_workspace_id: Optional[str] = Header(default=None),
) -> Actor:
    raw_token = request.cookies.get(settings.session_cookie_name, "")
    if not raw_token:
        return _dev_fallback_actor(db, settings)

    session = resolve_session(db, raw_token, settings=settings)
    if session is None:
        raise _unauthenticated()

    if request.method not in SAFE_METHODS and not csrf_token_matches(
        session, request.headers.get(settings.csrf_header_name)
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

    user = db.get(User, session.user_id)
    if user is None or user.status != "active":
        raise _unauthenticated()

    workspace, membership = _resolve_workspace(db, user, x_workspace_id)
    return _actor_for(db, user, workspace, membership, session_id=session.id)


def get_token_actor(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Actor:
    """The machine door, BESIDE `get_actor` rather than inside it.

    `Authorization: Bearer grain_…` → hash lookup in `api_tokens` → the Actor
    of the member who minted the token, bound to the token's own workspace. A
    token is a delegation of one member's access: if that membership is gone,
    `api_tokens.resolve` answers None and the request is a 401 like any other
    identity failure — one uniform message, nothing about *why*.

    Deliberately not widened into `get_actor`: the cookie path carries CSRF
    (cookies ride cross-site; bearer headers cannot be attached by an
    attacker's page, so there is nothing to double-submit here), the
    `X-Workspace-Id` selection (a token names its workspace; letting the
    header override it would turn one workspace's credential into a chooser),
    and the dev fallback (a machine credential must never fall back to a
    seeded human). Routes taking this dependency are listed in
    PUBLIC_UNSAFE_ROUTES with their justification, because the tripwire in
    test_auth_boundaries.py looks for `get_actor` specifically.
    """
    presented = authorization.removeprefix("Bearer ").strip()
    resolved = api_tokens.resolve(db, presented)
    if resolved is None:
        raise _unauthenticated()
    user = db.get(User, resolved.user_id)
    if user is None or user.status != "active":
        raise _unauthenticated()
    workspace = db.get(Workspace, resolved.workspace_id)
    if workspace is None:
        raise _unauthenticated()
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == resolved.workspace_id,
            Membership.user_id == resolved.user_id,
        )
    )
    if membership is None:
        raise _unauthenticated()
    return _actor_for(db, user, workspace, membership, session_id=None)


def require_owner(actor: Actor = Depends(get_actor)) -> Actor:
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    return actor


def require_org_admin(actor: Actor = Depends(get_actor)) -> Actor:
    """The gate above `require_owner`, and the reason the tier is worth having.

    It reads `org_role` and nothing else. In particular it does **not** fall back
    to `actor.role`, and does not treat a workspace owner as an org admin for the
    org their workspace happens to sit in. That fallback is the obvious
    convenience and it is precisely the inversion this exists to prevent: if
    being an owner implied org powers, then the organization could never forbid
    anything the workspace owner wanted, and "scopes can only tighten
    organization-wide policies" would be a comment.

    The two gates therefore compose in one direction only. Every route under
    `/api/org` that writes takes this one; nothing under `/api/admin` writes an
    org role at all, so there is no path from owner to admin — a workspace owner
    cannot promote themselves, because no endpoint they can reach writes the row
    that would do it.
    """
    if actor.org_role != ORG_ADMIN:
        raise HTTPException(status_code=403, detail="Organization admin role required")
    return actor
