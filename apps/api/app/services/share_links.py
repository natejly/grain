"""Share links: mint, resolve and revoke revocable public read tokens.

The token rules are the house token rules (`services/auth/invites.py`,
`services/auth/sessions.py`): `secrets.token_urlsafe(32)` raw exactly once,
stored only as a SHA-256 hexdigest via a local `hash_token` copy — per-module
on purpose, auth modules do not import each other's — and looked up only by
hash. `load_active` is the public route's single gate and fail-closed by
construction: unknown, revoked and expired all come back as the same None,
because to an anonymous caller "this link does not work" must be one
indistinguishable fact.

Like every service here, nothing commits: the route owns the transaction.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import ShareLink

#: The kinds a link may point at. Published apps already have a public surface
#: of their own (`/published/apps/{slug}`), so they are deliberately absent.
RESOURCE_KINDS = ("dashboard", "document")


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue(
    db: Session,
    *,
    workspace_id: str,
    resource_kind: str,
    resource_id: str,
    created_by: str,
    expires_at: Optional[datetime] = None,
) -> Tuple[ShareLink, str]:
    """Mint a link and return it with its raw token — the only time it exists.

    The caller has already resolved the resource under its own workspace; this
    only records the grant. Flushes so the row has its id; never commits.
    """
    raw_token = secrets.token_urlsafe(32)
    link = ShareLink(
        workspace_id=workspace_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        token_hash=hash_token(raw_token),
        created_by=created_by,
        expires_at=expires_at,
    )
    db.add(link)
    db.flush()
    return link, raw_token


def load_active(
    db: Session, *, raw_token: str, now: Optional[datetime] = None
) -> Optional[ShareLink]:
    """The link this token names, iff it still works. None says nothing more."""
    link = db.scalar(
        select(ShareLink).where(ShareLink.token_hash == hash_token(raw_token))
    )
    if link is None:
        return None
    if link.revoked_at is not None:
        return None
    if link.expires_at is not None and link.expires_at <= (now or utcnow()):
        return None
    return link


def revoke(link: ShareLink, *, now: Optional[datetime] = None) -> bool:
    """Stop the link working. True iff this call did it (revoking is one-way,
    so a second revoke keeps the first timestamp and reports nothing new)."""
    if link.revoked_at is not None:
        return False
    link.revoked_at = now or utcnow()
    return True
