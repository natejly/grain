"""The organization tier: provisioning, membership, and what it bounds.

Everything here is *lookup and configuration*. The one decision that matters —
whether a tool may run — deliberately does not live here: it lives in
`agent_loop.evaluate_policy`, which stays the single decision point and reaches
into this module only for rows. A second place that answers "may this run?" would
be a second place to get the one-way rule wrong.
"""

from __future__ import annotations

import json
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    ORG_ADMIN,
    Organization,
    OrgMembership,
    Workspace,
)
from .harness import HARNESSES


def provision_org(db: Session, *, name: str, founder_id: str) -> Organization:
    """A new organization with one named admin. The front door.

    `founder_id` is required rather than optional because the alternative — an
    org that quietly enrolls nobody — is the thing `models._attach_orphan_workspaces`
    already produces as its safe floor. This function exists for the other case,
    where somebody really is meant to hold the authority, and making the caller
    say who removes any chance of the two being confused.

    Does not commit; the caller owns the transaction, exactly as `_create_account`
    does for the user it creates alongside.
    """
    org = Organization(name=name[:160] or "Organization")
    db.add(org)
    db.flush()
    db.add(OrgMembership(organization_id=org.id, user_id=founder_id, role=ORG_ADMIN))
    return org


def org_id_for_workspace(db: Session, workspace_id: str) -> str:
    """Which org governs this workspace, or "" if the workspace does not exist.

    A scalar select rather than `db.get(Workspace, ...)`: this runs on every
    single policy decision, and fetching one indexed column beats hydrating a
    whole entity into the identity map to read one attribute off it.
    """
    return (
        db.scalar(select(Workspace.organization_id).where(Workspace.id == workspace_id))
        or ""
    )


def role_in_org(db: Session, *, organization_id: str, user_id: str) -> str:
    """This person's org role, or "" if they are not in the org at all.

    "" is the common case and a meaningful one: a contractor invited into a
    single workspace is governed by that workspace's org while having no standing
    in it. Not-a-member and member-with-no-powers are different facts, and the
    admin gate must refuse both.
    """
    if not organization_id or not user_id:
        return ""
    return (
        db.scalar(
            select(OrgMembership.role).where(
                OrgMembership.organization_id == organization_id,
                OrgMembership.user_id == user_id,
            )
        )
        or ""
    )


def decode_allow_list(raw: str) -> Optional[List[str]]:
    """The stored allow-list, or None for "no bound at all".

    "" means unbounded and `[]` means nothing is permitted — a distinction the
    column comment argues for and this function is where it is honoured. A value
    that is neither (corruption, a hand-edited row) is treated as unbounded,
    because failing open on *configuration* while the policy tier still fails
    closed is the lesser of the two: a garbled model list must not brick every
    turn in the org, whereas a garbled `OrgToolPolicy.policy` is filtered out
    against a literal set before it can decide anything.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    if not isinstance(decoded, list):
        return None
    return [str(item) for item in decoded]


def _bounded(offered: Sequence[str], allowed: Optional[List[str]]) -> List[str]:
    """`offered`, minus anything the org has not allowed. Order is `offered`'s.

    An intersection, never a union, and that is the ceiling rule applied to sets
    instead of to verdicts: an org naming a model the deployment does not offer
    does not conjure it into existence, it just names nothing.
    """
    if allowed is None:
        return list(offered)
    permitted = set(allowed)
    return [name for name in offered if name in permitted]


def allowed_models(db: Session, *, workspace_id: str, settings: Settings) -> List[str]:
    """The models this workspace may actually pick from.

    The deployment's allow-list is the outer bound and the org's is an inner one.
    Both the composer's dropdown (`/api/bootstrap`) and the refusal at
    `POST /api/conversations/{id}/messages` read this, so what is offered and what
    is accepted cannot drift apart — the failure where a UI shows a choice the
    server 422s.
    """
    org_id = org_id_for_workspace(db, workspace_id)
    org = db.get(Organization, org_id) if org_id else None
    allowed = decode_allow_list(org.allowed_models_json) if org else None
    return _bounded(settings.selectable_models, allowed)


def allowed_harnesses(
    db: Session, *, workspace_id: str, settings: Settings
) -> List[str]:
    """The harnesses this workspace may run on, out of those registered.

    Unlike models, the harness is a process-wide setting today, so this is not a
    menu anybody picks from — it is the set the deployment's own choice has to be
    inside. `harness_permitted` is the question actually asked at run time.
    """
    org_id = org_id_for_workspace(db, workspace_id)
    org = db.get(Organization, org_id) if org_id else None
    allowed = decode_allow_list(org.allowed_harnesses_json) if org else None
    return _bounded(sorted(HARNESSES), allowed)


def harness_permitted(db: Session, *, workspace_id: str, settings: Settings) -> bool:
    """Whether this workspace's org permits the harness this process is running."""
    return settings.active_model_provider in allowed_harnesses(
        db, workspace_id=workspace_id, settings=settings
    )


def encode_allow_list(names: Optional[Sequence[str]]) -> str:
    """The storage form of an allow-list. None is "no bound"; a list is a bound.

    Inverse of `decode_allow_list`, kept beside it so the "" / `[]` distinction
    is written and read within ten lines of each other.
    """
    if names is None:
        return ""
    return json.dumps([str(name) for name in names])
