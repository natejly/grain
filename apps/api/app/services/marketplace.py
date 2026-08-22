"""The mechanics of publishing and installing: payloads, visibility, the lint.

`api/marketplace.py` owns the HTTP shape (idempotency, audit, the gates); this
module owns the three rules that must hold everywhere they happen:

- What crosses a workspace boundary is an *allowlist*, not a filter. Each kind
  has a pydantic payload model (`extra="forbid"`) naming exactly the fields
  that transfer; a secret, grant, or workspace id can only ride along by
  editing that model, never by a new column defaulting into the payload.
- Every read goes through one `resolve_visible`/`_visible` chokepoint,
  mirroring `conversations.resolve_visible` — a foreign or invisible listing
  is a 404 at the call site, never a leak.
- Publishing runs a secret lint over every payload string. The allowlist keeps
  the *schema* clean; the lint is for what a person pastes into a skill body.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import List, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Listing, ListingVersion, Skill

#: What installing may not silently collide with forever: cap the suffix probe.
_MAX_NAME_ATTEMPTS = 50


class SkillPayload(BaseModel):
    """The whole of what a published skill is. Exactly these fields transfer."""

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str
    body: str
    args_json: str


def snapshot_skill(skill: Skill) -> SkillPayload:
    """The current content, which by construction equals the head SkillVersion."""
    return SkillPayload(
        title=skill.title,
        description=skill.description,
        body=skill.body,
        args_json=skill.args_json,
    )


def serialize_payload(payload: BaseModel) -> str:
    """Canonical JSON for a payload: sorted keys, compact separators, so the
    content hash is stable for identical content regardless of field order."""
    return json.dumps(payload.model_dump(), separators=(",", ":"), sort_keys=True)


def payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def resolve_visible(
    db: Session,
    *,
    workspace_id: str,
    organization_id: str,
    listing_id: str,
) -> Optional[Listing]:
    """The one visibility rule, in one place.

    A listing is visible when it is published and either workspace-tier in the
    caller's workspace, or org-tier in the caller's organization. Returns None
    so a foreign or invisible id becomes a 404 at the call site, never a leak.
    'delisted' rows are invisible here on purpose — installs already made are
    copies and keep working; the listing itself withdraws from every surface.
    """
    listing = db.scalar(
        select(Listing).where(Listing.id == listing_id, *_visible(workspace_id, organization_id))
    )
    return listing


def list_visible(
    db: Session, *, workspace_id: str, organization_id: str
) -> List[Listing]:
    rows = db.scalars(
        select(Listing)
        .where(*_visible(workspace_id, organization_id))
        .order_by(Listing.created_at.desc(), Listing.id.desc())
    )
    return list(rows)


def _visible(workspace_id: str, organization_id: str):
    """The WHERE clauses of listing visibility, shared by every query above.

    Workspace-tier rows are confined to the publisher's workspace; org-tier
    rows reach every workspace of the same organization. The workspace filter
    on the workspace tier is NEVER removed — mirroring the rule stated in
    `services/skills.resolve_visible` and enforced by the isolation sweep.
    """
    return (
        Listing.status == "published",
        (
            (Listing.visibility == "workspace") & (Listing.workspace_id == workspace_id)
        )
        | ((Listing.visibility == "org") & (Listing.organization_id == organization_id)),
    )


def latest_version(db: Session, *, listing: Listing) -> Optional[ListingVersion]:
    return db.scalar(
        select(ListingVersion)
        .where(ListingVersion.listing_id == listing.id)
        .order_by(ListingVersion.version.desc())
        .limit(1)
    )


def list_versions(db: Session, *, listing: Listing) -> List[ListingVersion]:
    rows = db.scalars(
        select(ListingVersion)
        .where(ListingVersion.listing_id == listing.id)
        .order_by(ListingVersion.version.desc())
    )
    return list(rows)


def free_skill_name(db: Session, *, workspace_id: str, base: str) -> str:
    """A slug not yet taken in the installing workspace: `base`, else `base-2`,
    `base-3`, … Deterministic and never a 409 — a name collision is the normal
    case for a popular listing, not an error the installer can do anything about."""
    taken = {
        row
        for row in db.scalars(
            select(Skill.name).where(
                Skill.workspace_id == workspace_id, Skill.name.like(f"{base}%")
            )
        )
    }
    if base not in taken:
        return base
    for attempt in range(2, _MAX_NAME_ATTEMPTS + 2):
        candidate = f"{base}-{attempt}"
        if candidate not in taken:
            return candidate
    # Unreachable outside adversarial fixtures; fall back to something unique.
    return f"{base}-{payload_hash(base)[:8]}"


# --------------------------------------------------------------------------
# The secret lint


#: Shapes that are a credential wherever they appear. Each pattern carries the
#: label the 422 detail reports, so the publisher is told what to remove, not
#: just refused.
_TOKEN_SHAPES = (
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "a GitHub token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("a Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("an API secret key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("a Stripe key", re.compile(r"\b[sprk]{2}_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("a bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9_.+/=-]{24,}")),
)

#: A URL whose query string carries a credential-shaped parameter — the signed
#: link problem: the URL is the secret.
_TOKENED_URL = re.compile(
    r"https?://\S*[?&](?:token|secret|signature|sig|apikey|api_key|access_token|key)=[^&\s\"']{8,}",
    re.IGNORECASE,
)

#: Candidate runs for the entropy check: long unbroken token-alphabet strings.
_ENTROPY_RUN = re.compile(r"[A-Za-z0-9+/_=-]{40,}")


def _entropy(value: str) -> float:
    counts = {char: value.count(char) for char in set(value)}
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def lint_strings(strings: List[str], *, workspace_id: str) -> List[str]:
    """Findings that make a payload unpublishable, as human-readable reasons.

    Three detectors: known token shapes, credential-bearing URLs, and long
    high-entropy runs (a key the shapes above don't know yet). Plus one that is
    not about secrets at all: the publisher's own workspace id, which must not
    escape because a foreign id in someone else's workspace is at best noise
    and at worst a probe target.
    """
    findings: List[str] = []
    for text in strings:
        if not text:
            continue
        for label, pattern in _TOKEN_SHAPES:
            if pattern.search(text):
                findings.append(f"the content contains what looks like {label}")
        if _TOKENED_URL.search(text):
            findings.append("the content contains a URL carrying a credential parameter")
        if workspace_id and workspace_id in text:
            findings.append("the content contains this workspace's id")
        for run in _ENTROPY_RUN.findall(text):
            # A UUID-dense line or base64 blob; prose never gets close to 4.5
            # bits/char over 40+ characters.
            if _entropy(run) > 4.5:
                findings.append(
                    "the content contains a long high-entropy string that looks like a key"
                )
                break
    # Deduplicate, preserving first-seen order, so one leaked key pasted five
    # times reads as one finding.
    seen = set()
    unique: List[str] = []
    for finding in findings:
        if finding not in seen:
            seen.add(finding)
            unique.append(finding)
    return unique


def payload_strings(payload: BaseModel) -> List[str]:
    """Every string the payload carries, flattened, for the lint."""
    out: List[str] = []

    def _walk(value) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(payload.model_dump())
    return out
