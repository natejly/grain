"""Minting and resolving the bearer tokens behind `POST /api/mcp`.

The secret's shape is `grain_<urlsafe-random>`, prefixed so a leaked one is
recognisable in log scrubbers and secret scanners the way `sk-`/`ghp_` keys
are. Only its sha256 is stored; resolution hashes the presented secret and
looks the digest up, so the comparison is an indexed equality over digests —
no plaintext ever touches the database, and no timing side channel over the
secret itself exists to protect.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import ApiToken, Membership

SECRET_PREFIX = "grain_"


@dataclass(frozen=True)
class MintedToken:
    """A fresh token and the one copy of its secret that will ever exist."""

    token: ApiToken
    secret: str


@dataclass(frozen=True)
class ResolvedToken:
    """Who a presented secret acts as."""

    token_id: str
    workspace_id: str
    user_id: str


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(db: Session, *, workspace_id: str, user_id: str, name: str) -> MintedToken:
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        workspace_id=workspace_id,
        user_id=user_id,
        name=name.strip()[:80],
        token_hash=_digest(secret),
    )
    db.add(token)
    db.flush()
    return MintedToken(token=token, secret=secret)


def resolve(db: Session, secret: str) -> Optional[ResolvedToken]:
    """The live identity behind a presented secret, or None.

    None for every failure — unknown digest, revoked stamp, or a member who
    has since left the workspace. That last check is the point of storing
    `user_id`: a token is a delegation of one member's access, and access a
    member no longer has is access their tokens no longer have either.
    """
    if not secret or not secret.startswith(SECRET_PREFIX):
        return None
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == _digest(secret)))
    if token is None or token.revoked_at is not None:
        return None
    membership = db.scalar(
        select(Membership.id).where(
            Membership.workspace_id == token.workspace_id,
            Membership.user_id == token.user_id,
        )
    )
    if membership is None:
        return None
    token.last_used_at = utcnow()
    db.commit()
    return ResolvedToken(
        token_id=token.id,
        workspace_id=token.workspace_id,
        user_id=token.user_id,
    )
