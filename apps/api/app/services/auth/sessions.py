"""Opaque server-side sessions, their cookie, and the CSRF pair that guards them.

Design notes that are easy to undo by accident:

* The session token is random, not a signed claim. Nothing about the user is
  encoded in it, so revocation is a row update rather than a keyring rotation.
* Only ``sha256(token)`` is stored. A dump of ``user_sessions`` therefore hands
  an attacker nothing replayable — the same reason ``EmailToken`` is hashed.
* ``expires_at`` and ``revoked_at`` are both enforced on every read, in the
  query itself, so no code path can resolve a dead session by forgetting one.
* ``csrf_secret`` is stored in the clear on purpose. It is not a credential: it
  authenticates nothing on its own, and the whole double-submit trick is that
  the browser will send the cookie cross-site but cannot be made to copy this
  value into a header cross-site.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings
from ...models import UserSession

# 32 bytes of urlsafe base64 is 43 characters at ~192 bits of entropy. Guessing
# one is not a threat model anybody has to think about again.
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 24


@dataclass(frozen=True)
class IssuedSession:
    """A freshly minted session plus the two values only the client keeps."""

    session: UserSession
    token: str
    csrf_token: str


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    *,
    user_id: str,
    settings: Settings,
    user_agent: str = "",
    ip_address: str = "",
) -> IssuedSession:
    """Mint a session row and return the raw token exactly once."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    csrf_token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
    now = utcnow()
    session = UserSession(
        user_id=user_id,
        token_hash=hash_token(token),
        csrf_secret=csrf_token,
        # Truncated to the column width: a hostile client can send a header of
        # any length, and a too-long value would fail the insert instead of the
        # login it belongs to.
        user_agent=user_agent[:400],
        ip_address=ip_address[:64],
        expires_at=now + settings.session_ttl,
        last_seen_at=now,
        created_at=now,
    )
    db.add(session)
    db.flush()
    return IssuedSession(session=session, token=token, csrf_token=csrf_token)


def resolve_session(
    db: Session, raw_token: str, *, settings: Settings, now: Optional[datetime] = None
) -> Optional[UserSession]:
    """Look up a live session, or None. Never raises for a bad token."""
    if not raw_token:
        return None
    moment = now or utcnow()
    session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(raw_token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > moment,
        )
    )
    if session is None:
        return None
    _touch(db, session, moment, settings)
    return session


def _touch(
    db: Session, session: UserSession, moment: datetime, settings: Settings
) -> None:
    if (moment - session.last_seen_at).total_seconds() < settings.session_touch_seconds:
        return
    session.last_seen_at = moment
    db.commit()


def revoke_session(db: Session, session: UserSession) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()


def revoke_all_sessions(db: Session, user_id: str) -> int:
    """Log every device out. Used after a password reset or a compromise."""
    now = utcnow()
    live = list(
        db.scalars(
            select(UserSession).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        )
    )
    for session in live:
        session.revoked_at = now
    return len(live)


def rotate_session(
    db: Session,
    session: UserSession,
    *,
    settings: Settings,
    user_agent: str = "",
    ip_address: str = "",
) -> IssuedSession:
    """Replace a session with a new token, revoking the old one.

    Called whenever the trust level of the session changes — logging in over an
    existing cookie, or a privilege change — so a token an attacker planted or
    observed beforehand is worthless afterwards (session fixation).
    """
    revoke_session(db, session)
    return create_session(
        db,
        user_id=session.user_id,
        settings=settings,
        user_agent=user_agent,
        ip_address=ip_address,
    )


def set_session_cookie(
    response: Response, issued: IssuedSession, settings: Settings
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=issued.token,
        max_age=int(settings.session_ttl.total_seconds()),
        path="/",
        domain=settings.session_cookie_domain,
        secure=settings.session_cookie_secure,
        httponly=True,
        # None, not Lax: the web app is on another registrable domain, so a Lax
        # cookie would never be attached to the API's requests at all. The cost
        # is that CSRF is back on the table, which is what csrf_secret answers.
        samesite="none",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        domain=settings.session_cookie_domain,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="none",
    )


def csrf_token_matches(session: UserSession, presented: Optional[str]) -> bool:
    """Constant-time compare of the header against the session's secret.

    Compared as bytes, not str. ``compare_digest`` raises TypeError on a str
    holding any non-ASCII character, and a header value is attacker-controlled
    bytes that Starlette decodes as latin-1 — so the str form turned a header of
    ``\\xe9...`` into an unhandled 500 instead of a 403. Encoding first keeps the
    answer "no" for every input, which is what a CSRF check owes its caller.
    """
    if not presented or not session.csrf_secret:
        return False
    return hmac.compare_digest(
        session.csrf_secret.encode("utf-8"), presented.encode("utf-8")
    )
