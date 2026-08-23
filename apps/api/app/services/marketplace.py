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

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Agent, Listing, ListingInstall, ListingVersion, Skill, Workflow
from .llm_tools import ToolContext, registry_families

#: What installing may not silently collide with forever: cap the suffix probe.
_MAX_NAME_ATTEMPTS = 50

#: Families whose tools exist in every workspace by construction. Everything
#: else — MCP servers, sandbox custom tools, connected integrations, database
#: connections — is workspace furniture: its *names* would travel but the things
#: they name would not, so those names are dropped at publish and reported as
#: `unresolved_tools` instead of being smuggled into a grant that cannot resolve.
PORTABLE_FAMILIES = frozenset(
    {"core", "memory", "graph", "artifacts", "projects", "dashboards", "sandbox"}
)

#: What every published workflow's trigger becomes. A schedule is an intent of
#: the publisher's workspace ("run this against OUR sources every Monday"), not
#: part of the program — an installer re-arms scheduling deliberately, the same
#: way an installed agent is re-enabled deliberately.
MANUAL_TRIGGER = {"kind": "manual", "cron": "", "timezone": "UTC"}


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


class AgentPayload(BaseModel):
    """The whole of what a published agent is.

    `allowed_tools_json` keeps the source's semantics — "" is "the whole
    registry", a JSON list is an explicit narrowing — but a list is already
    reduced to PORTABLE_FAMILIES at publish. What was dropped is named in
    `unresolved_tools`, so the gallery can render the requires-checklist
    honestly instead of the installer discovering the holes at run time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    instructions: str
    allowed_tools_json: str
    unresolved_tools: List[str] = Field(default_factory=list)


class WorkflowPayload(BaseModel):
    """The whole of what a published workflow is: the program, never the timer.

    The trigger inside `graph_json` is forced to manual and the schedule
    columns are simply not fields here — excluded by construction, not
    stripped. Crons are not a publishable kind at all: a cron is a personal
    prompt on a timer, and both halves of that are workspace-bound.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    source_prompt: str
    graph_json: str


def _family_of_name(db: Session, *, workspace_id: str, user_id: str) -> dict:
    context = ToolContext(
        workspace_id=workspace_id, user_id=user_id, conversation_id=""
    )
    return {
        name: family
        for family, tools in registry_families(db, context)
        for name in tools
    }


def registry_names(db: Session, *, workspace_id: str, user_id: str) -> set:
    """Every tool name this workspace's registry offers right now."""
    return set(_family_of_name(db, workspace_id=workspace_id, user_id=user_id))


def snapshot_agent(db: Session, agent: Agent, *, user_id: str) -> AgentPayload:
    if not agent.allowed_tools_json:
        return AgentPayload(
            name=agent.name,
            description=agent.description,
            instructions=agent.instructions,
            allowed_tools_json="",
        )
    try:
        raw = json.loads(agent.allowed_tools_json)
    except ValueError:
        raw = []
    names = [name for name in raw if isinstance(name, str)] if isinstance(raw, list) else []
    families = _family_of_name(db, workspace_id=agent.workspace_id, user_id=user_id)
    portable = sorted(
        name for name in names if families.get(name) in PORTABLE_FAMILIES
    )
    unresolved = sorted(
        name for name in names if families.get(name) not in PORTABLE_FAMILIES
    )
    return AgentPayload(
        name=agent.name,
        description=agent.description,
        instructions=agent.instructions,
        allowed_tools_json=json.dumps(portable, separators=(",", ":")),
        unresolved_tools=unresolved,
    )


def snapshot_workflow(workflow: Workflow) -> WorkflowPayload:
    """Raises ValueError when the stored graph does not parse — a workflow that
    unreadable cannot be honestly published, only fixed."""
    graph = json.loads(workflow.graph_json)
    if not isinstance(graph, dict):
        raise ValueError("workflow graph is not an object")
    graph["trigger"] = dict(MANUAL_TRIGGER)
    return WorkflowPayload(
        name=workflow.name,
        description=workflow.description,
        source_prompt=workflow.source_prompt,
        graph_json=json.dumps(graph, separators=(",", ":"), sort_keys=True),
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


# ---------------------------------------------------------------------------
# Lineage: what a workspace installed, and whether it moved since


def local_content_hash(
    db: Session, *, kind: str, target_id: str, workspace_id: str
) -> Optional[str]:
    """The identity of the installed copy's content, per kind; None when the
    copy no longer exists. Divergence is "this function's answer changed since
    install" — the same function runs at install time and at check time, so it
    only ever compares a copy to itself, never one kind to another.

    For agents the local tool grant is part of the identity on purpose: the
    grant is the part of an agent an installer most deliberately shapes, and an
    update must not be able to silently overwrite a reshaped one.
    """
    if kind == "skill":
        skill = db.scalar(
            select(Skill).where(
                Skill.id == target_id, Skill.workspace_id == workspace_id
            )
        )
        return skill.content_hash if skill is not None else None
    if kind == "agent":
        agent = db.scalar(
            select(Agent).where(
                Agent.id == target_id, Agent.workspace_id == workspace_id
            )
        )
        if agent is None:
            return None
        return _hash_fields(
            agent.name, agent.description, agent.instructions, agent.allowed_tools_json
        )
    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == target_id, Workflow.workspace_id == workspace_id
        )
    )
    if workflow is None:
        return None
    return _hash_fields(
        workflow.name, workflow.description, workflow.source_prompt, workflow.graph_json
    )


def _hash_fields(*fields: str) -> str:
    return hashlib.sha256("\x00".join(fields).encode("utf-8")).hexdigest()


def find_install(
    db: Session, *, workspace_id: str, listing_id: str
) -> Optional[ListingInstall]:
    return db.scalar(
        select(ListingInstall).where(
            ListingInstall.workspace_id == workspace_id,
            ListingInstall.listing_id == listing_id,
        )
    )


def install_states(
    db: Session, *, listings: List[Listing], workspace_id: str
) -> dict[str, tuple[str, bool]]:
    """listing_id -> (state, pinned) for the caller's workspace. State is:

    - ``""`` — never installed, or the copy was deleted (installable again).
      A deleted copy also reports ``pinned=False``: the pin froze a copy that
      no longer exists, so every surface treats the listing as not installed.
    - ``"installed"`` — the copy is intact and there is nothing newer to offer
      (including when there IS something newer but the install is pinned: a pin
      means "stop offering", so the suppression happens here, not in the UI);
    - ``"update_available"`` — a newer version exists and the copy is unedited;
    - ``"diverged"`` — the copy was edited locally since install. Reported even
      when no newer version exists: it is a statement about the copy, and the
      update flow uses it to gate overwrites (a confirmed update re-syncs).

    Batched — a constant number of queries however long the gallery grows:
    one for the lineage rows, one per kind for the copies, one for versions.
    """
    states: dict[str, tuple[str, bool]] = {row.id: ("", False) for row in listings}
    if not listings:
        return states
    listing_by_id = {row.id: row for row in listings}
    installs = list(
        db.scalars(
            select(ListingInstall).where(
                ListingInstall.workspace_id == workspace_id,
                ListingInstall.listing_id.in_(list(listing_by_id)),
            )
        )
    )
    if not installs:
        return states

    targets_by_kind: dict[str, List[str]] = {}
    for install in installs:
        targets_by_kind.setdefault(install.target_kind, []).append(install.target_id)
    local: dict[tuple[str, str], str] = {}
    if "skill" in targets_by_kind:
        for skill in db.scalars(
            select(Skill).where(
                Skill.workspace_id == workspace_id,
                Skill.id.in_(targets_by_kind["skill"]),
            )
        ):
            local[("skill", skill.id)] = skill.content_hash
    if "agent" in targets_by_kind:
        for agent in db.scalars(
            select(Agent).where(
                Agent.workspace_id == workspace_id,
                Agent.id.in_(targets_by_kind["agent"]),
            )
        ):
            local[("agent", agent.id)] = _hash_fields(
                agent.name, agent.description, agent.instructions,
                agent.allowed_tools_json,
            )
    if "workflow" in targets_by_kind:
        for workflow in db.scalars(
            select(Workflow).where(
                Workflow.workspace_id == workspace_id,
                Workflow.id.in_(targets_by_kind["workflow"]),
            )
        ):
            local[("workflow", workflow.id)] = _hash_fields(
                workflow.name, workflow.description, workflow.source_prompt,
                workflow.graph_json,
            )

    # Columns only — a full ListingVersion row would drag every installed
    # listing's payload body along on each gallery render.
    versions = {
        row.id: row.version
        for row in db.execute(
            select(ListingVersion.id, ListingVersion.version).where(
                ListingVersion.id.in_(
                    [install.listing_version_id for install in installs]
                )
            )
        )
    }

    for install in installs:
        listing = listing_by_id[install.listing_id]
        copy_hash = local.get((install.target_kind, install.target_id))
        if copy_hash is None:
            continue  # deleted copy: keep ("", False)
        if copy_hash != install.content_hash_at_install:
            states[listing.id] = ("diverged", install.pinned)
            continue
        installed_version = versions.get(install.listing_version_id)
        if (
            not install.pinned
            and installed_version is not None
            and installed_version < listing.latest_version
        ):
            states[listing.id] = ("update_available", install.pinned)
        else:
            states[listing.id] = ("installed", install.pinned)
    return states


def install_state(
    db: Session, *, listing: Listing, workspace_id: str
) -> tuple[str, bool]:
    """The single-listing view of `install_states` — same contract, one row."""
    return install_states(db, listings=[listing], workspace_id=workspace_id)[
        listing.id
    ]
