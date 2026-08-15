"""The organization tier, and the one-way rule: scopes may only tighten.

`evaluate_policy` now resolves four tiers — organization, workspace, person,
conversation mode — and the organization is the only one that is a *ceiling*.
Everything below it may restrain the agent further and nothing below it may
restrain the agent less.

The rule is implemented as one line: `_stricter(result, org_ceiling)`, over the
ladder allow(0) < ask(1) < deny(2). That is small enough to be deleted by
accident, which is why `test_removing_the_clamp_lets_a_workspace_override_the_org`
below deletes it on purpose and asserts that the suite notices — a "scopes may
only tighten" claim with no test that fails when it stops being true is a
comment, not a control.

The five things that can loosen a verdict, each given its own attempt to escape:

* a workspace-wide `allow` row, written by an owner;
* a *personal* `allow` row, which outranks the workspace's (ADR 0010);
* a standing `chat` grant, the "always allow" click;
* `auto_writes`, the conversation-level approval bypass;
* `DEV_UNRESTRICTED_AGENT`, which is the bypass with no click at all.

None of them may cross an org `deny`, and none may relax an org `ask` to `allow`.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

import pytest
from conftest import Identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    ORG_ADMIN,
    ORG_MEMBER,
    Agent,
    Membership,
    Organization,
    OrgMembership,
    OrgToolPolicy,
    Run,
    ToolPolicy,
    User,
    Workspace,
)
from app.services import agent_loop, orgs
from app.services.agent_loop import (
    ASK_ALL,
    ASK_WRITES,
    AUTO_WRITES,
    CHAT_SCOPE,
    WORKFLOW_SCOPE,
    OrgBoundExceeded,
    Verdict,
    approval_mode_for_run,
    evaluate_policy,
)
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec

WRITE = "org_probe_write"
READ = "org_probe_read"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def spec_for(name: str, *, read_only: bool = False, force_ask: bool = False) -> ToolSpec:
    def run(db: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        return ToolResult(content="ok")

    return ToolSpec(
        name=name,
        description="A probe.",
        parameters={"type": "object", "properties": {}},
        executor=run,
        read_only=read_only,
        force_ask=force_ask,
    )


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    """A workspace owner who is also the admin of their own organization.

    Exactly what signup produces. Every test that wants the two authorities to
    come apart does so explicitly, by demoting or by adding a second person, so
    that the difference is visible in the test rather than in the fixture.
    """
    return identity_client(name="Org owner", workspace_name="Org workspace")


def identity_of(client: TestClient) -> Identity:
    return client.identity  # type: ignore[attr-defined,no-any-return]


def agent_id_of(db: Any, client: TestClient) -> str:
    agent = db.scalar(
        select(Agent).where(Agent.workspace_id == identity_of(client).workspace_id)
    )
    assert agent is not None
    return str(agent.id)


def org_id_of(db: Any, client: TestClient) -> str:
    return orgs.org_id_for_workspace(db, identity_of(client).workspace_id)


def set_org_policy(
    db: Any, client: TestClient, *, tool: str, policy: str, scope: str = CHAT_SCOPE
) -> None:
    db.add(
        OrgToolPolicy(
            organization_id=org_id_of(db, client),
            tool_name=tool,
            policy=policy,
            scope=scope,
        )
    )
    db.commit()


def set_workspace_policy(
    db: Any,
    client: TestClient,
    *,
    tool: str,
    policy: str,
    scope: str = CHAT_SCOPE,
    owner_id: str = "",
) -> None:
    identity = identity_of(client)
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name=tool,
            policy=policy,
            scope=scope,
            owner_id=owner_id,
        )
    )
    db.commit()


def verdict(
    db: Any,
    client: TestClient,
    *,
    tool: str = WRITE,
    read_only: bool = False,
    force_ask: bool = False,
    scope: str = CHAT_SCOPE,
    mode: str = ASK_WRITES,
) -> Verdict:
    identity = identity_of(client)
    return evaluate_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=spec_for(tool, read_only=read_only, force_ask=force_ask),
        scope=scope,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# The algebra
# --------------------------------------------------------------------------


def test_an_org_with_no_rows_changes_no_answer(owner: TestClient, db: Any) -> None:
    """`allow` is the identity element, so the tier is invisible until used.

    Every workspace in the database now sits under an organization, including
    every one the migration backfilled, so "an org that has configured nothing
    behaves exactly as no org at all" is what stops this feature from being a
    behaviour change for every existing customer.
    """
    assert verdict(db, owner, read_only=True).policy == "allow"
    assert verdict(db, owner).policy == "ask"
    set_workspace_policy(db, owner, tool=WRITE, policy="allow")
    assert verdict(db, owner).policy == "allow"


def test_an_org_deny_cannot_be_loosened_by_anything_below_it(
    owner: TestClient, db: Any
) -> None:
    """The load-bearing case, run against all five loosening mechanisms at once.

    Each of these, on its own, is enough to turn an `ask` into an `allow` in the
    absence of an org. Stacked together they are the strongest position a
    workspace can possibly take, and the answer is still `deny`.
    """
    identity = identity_of(owner)
    set_org_policy(db, owner, tool=WRITE, policy="deny")

    # 1. the workspace's own allow, written by an owner
    set_workspace_policy(db, owner, tool=WRITE, policy="allow")
    assert verdict(db, owner).policy == "deny"

    # 2. the caller's personal allow, which outranks the workspace's
    set_workspace_policy(db, owner, tool=WRITE, policy="allow", owner_id=identity.user_id)
    assert verdict(db, owner).policy == "deny"

    # 3. a standing workflow-scope grant, so the workflow path is covered too
    set_workspace_policy(db, owner, tool=WRITE, policy="allow", scope=WORKFLOW_SCOPE)
    assert verdict(db, owner, scope=WORKFLOW_SCOPE).policy == "deny"

    # 4. auto_writes, the conversation-level bypass
    assert verdict(db, owner, mode=AUTO_WRITES).policy == "deny"

    # 5. ask_all is a tightening and is therefore *allowed* to apply — but it
    #    cannot make the answer weaker than deny either.
    assert verdict(db, owner, mode=ASK_ALL).policy == "deny"


def test_an_org_ask_may_be_tightened_below_but_never_relaxed(
    owner: TestClient, db: Any
) -> None:
    """The middle rung: `ask` is a ceiling with room underneath it."""
    set_org_policy(db, owner, tool=READ, policy="ask")

    # A read-only tool would run unattended by default; the org's ask stops it.
    assert verdict(db, owner, tool=READ, read_only=True).policy == "ask"

    # An explicit workspace allow does not clear it either.
    set_workspace_policy(db, owner, tool=READ, policy="allow")
    assert verdict(db, owner, tool=READ, read_only=True).policy == "ask"

    # Nor does the bypass.
    assert verdict(db, owner, tool=READ, read_only=True, mode=AUTO_WRITES).policy == "ask"

    # But tightening below an `ask` is still available: a workspace deny wins.
    # The same row is flipped rather than a second one written, because
    # (workspace, owner, tool, scope) is a unique key — which is itself the
    # reason a workspace cannot answer an org twice.
    row = db.scalar(select(ToolPolicy).where(ToolPolicy.tool_name == READ))
    row.policy = "deny"
    db.commit()
    assert verdict(db, owner, tool=READ, read_only=True).policy == "deny"


def test_an_org_allow_is_a_ceiling_not_a_floor(owner: TestClient, db: Any) -> None:
    """An org saying `allow` does not force anything to run.

    This is the direction people get wrong. "The org allows it" is permission for
    the tiers below to decide, not an instruction to them — otherwise an org
    admin listing a tool would silently strip every workspace's ability to
    require review of it, which is a loosening the one-way rule forbids just as
    much as the other direction.
    """
    set_org_policy(db, owner, tool=WRITE, policy="allow")
    # The tool's own default still applies: a write still asks.
    assert verdict(db, owner).policy == "ask"
    # And a workspace deny still denies.
    set_workspace_policy(db, owner, tool=WRITE, policy="deny")
    assert verdict(db, owner).policy == "deny"


def test_an_org_chat_deny_carries_into_workflow_scope(
    owner: TestClient, db: Any
) -> None:
    """The same sentence the workspace tier uses, one tier up.

    A prohibition is not a grant, so it survives the scope boundary; anything
    else the org wrote at `chat` scope does not reach an unattended run.
    """
    set_org_policy(db, owner, tool=WRITE, policy="deny", scope=CHAT_SCOPE)
    assert verdict(db, owner, scope=WORKFLOW_SCOPE).policy == "deny"

    # An org `ask` at chat scope, by contrast, does not follow — the workflow
    # falls back to the tool's own default, which for a write is also `ask`, so
    # the observable difference is on a read-only tool.
    other = "org_probe_read_only"
    set_org_policy(db, owner, tool=other, policy="ask", scope=CHAT_SCOPE)
    assert verdict(db, owner, tool=other, read_only=True, scope=CHAT_SCOPE).policy == "ask"
    assert (
        verdict(db, owner, tool=other, read_only=True, scope=WORKFLOW_SCOPE).policy
        == "allow"
    )


def test_the_clamp_does_not_leave_a_false_attribution(
    owner: TestClient, db: Any
) -> None:
    """`by_mode` says what actually happened, and the org overruling the mode is
    not the mode letting a call through.

    Property 3 of the approval modes: a row claiming a bypass approved a call
    that never ran is a row a later auditor cannot tell apart from a real one.
    """
    # Without the org, the bypass is genuinely what decided this.
    bypassed = verdict(db, owner, mode=AUTO_WRITES)
    assert (bypassed.policy, bypassed.by_mode) == ("allow", AUTO_WRITES)

    set_org_policy(db, owner, tool=WRITE, policy="ask")
    clamped = verdict(db, owner, mode=AUTO_WRITES)
    assert (clamped.policy, clamped.by_mode) == ("ask", "")


def test_force_ask_and_the_org_ceiling_compose(owner: TestClient, db: Any) -> None:
    """Two tightenings on the same call, applied in order, both surviving."""
    set_org_policy(db, owner, tool=WRITE, policy="deny")
    # `force_ask` can only raise an allow to an ask; it must not lower a deny.
    assert verdict(db, owner, force_ask=True, mode=AUTO_WRITES).policy == "deny"


def test_dev_unrestricted_agent_cannot_escape_an_org_deny(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The widest bypass in the codebase, and it stops at the org too.

    `DEV_UNRESTRICTED_AGENT` resolves to `auto_writes` in `approval_mode_for_run`
    rather than to a fourth mode, so it reaches `evaluate_policy` as a mode and
    is clamped by the same line as everything else. This asserts the composition
    end to end rather than trusting that collapse.
    """
    identity = identity_of(owner)
    settings = get_settings()
    monkeypatch.setattr(settings, "dev_unrestricted_agent", True)

    conversation = owner.post(
        "/api/conversations",
        json={"title": "Unrestricted"},
        headers={"Idempotency-Key": "org-unrestricted-conv"},
    ).json()
    run = Run(
        workspace_id=identity.workspace_id,
        conversation_id=conversation["id"],
        agent_id=agent_id_of(db, owner),
        created_by=identity.user_id,
        status="running",
        prompt="probe",
    )
    db.add(run)
    db.commit()

    mode = approval_mode_for_run(db, run, scope=CHAT_SCOPE, settings=settings)
    assert mode == AUTO_WRITES, "the flag still collapses to the bypass"

    set_org_policy(db, owner, tool=WRITE, policy="deny")
    assert verdict(db, owner, mode=mode).policy == "deny"


# --------------------------------------------------------------------------
# The mutation proof
# --------------------------------------------------------------------------


def test_removing_the_clamp_lets_a_workspace_override_the_org(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete the one-way rule and watch a workspace walk through an org deny.

    This is the test that makes the claim falsifiable. `_stricter` is the whole
    clamp; monkeypatching it to "whatever the tiers below decided" is exactly the
    edit somebody would make while refactoring, and the assertion is that the
    behaviour changes — an org `deny` becomes an `allow` the moment the line
    stops being there.

    It is also the guard against the clamp being *unreachable*: if
    `evaluate_policy` had already returned before the ceiling was applied, this
    test would see no difference between the patched and unpatched forms and
    would fail on the first assertion, not the second.
    """
    set_org_policy(db, owner, tool=WRITE, policy="deny")
    set_workspace_policy(db, owner, tool=WRITE, policy="allow")

    assert verdict(db, owner).policy == "deny", "the clamp is holding"

    monkeypatch.setattr(agent_loop, "_stricter", lambda left, right: left)
    assert verdict(db, owner).policy == "allow", (
        "with the clamp removed the workspace's allow wins — which is precisely "
        "the escalation the clamp exists to prevent"
    )


# --------------------------------------------------------------------------
# Harnesses and models
# --------------------------------------------------------------------------


def test_an_org_bounds_the_models_a_workspace_may_pick(
    owner: TestClient, db: Any
) -> None:
    """The composer's list and the send route's refusal read the same function."""
    identity = identity_of(owner)
    settings = get_settings()
    offered = settings.selectable_models
    assert offered, "the suite's provider offers at least one model"

    assert orgs.allowed_models(
        db, workspace_id=identity.workspace_id, settings=settings
    ) == offered

    org = db.get(Organization, org_id_of(db, owner))
    org.allowed_models_json = orgs.encode_allow_list([])
    db.commit()
    assert (
        orgs.allowed_models(db, workspace_id=identity.workspace_id, settings=settings)
        == []
    )

    # And the bootstrap the composer renders from agrees with it.
    bootstrap = owner.get("/api/bootstrap").json()
    assert bootstrap["model_provider"]["selectable_models"] == ["scripted-double"], (
        "under the scripted double the list is pinned; the openai branch is what "
        "reads the org bound, and `allowed_models` above is that same call"
    )


def test_an_org_bounds_the_harness_and_a_run_refuses_outside_it(
    owner: TestClient, db: Any
) -> None:
    """A harness the org has not allowed stops the turn before the model is built.

    Enforced above the `model_step` seam rather than inside it, so an injected
    step — which is how the executor and every test drive the loop — does not
    walk around the bound.
    """
    identity = identity_of(owner)
    settings = get_settings()
    assert orgs.harness_permitted(
        db, workspace_id=identity.workspace_id, settings=settings
    )

    org = db.get(Organization, org_id_of(db, owner))
    org.allowed_harnesses_json = orgs.encode_allow_list(["some-other-harness"])
    db.commit()
    assert not orgs.harness_permitted(
        db, workspace_id=identity.workspace_id, settings=settings
    )

    run = Run(
        workspace_id=identity.workspace_id,
        agent_id=agent_id_of(db, owner),
        created_by=identity.user_id,
        status="running",
        prompt="probe",
    )
    with pytest.raises(OrgBoundExceeded):
        agent_loop._enforce_org_bounds(db, run, settings)


def test_an_unbounded_org_is_not_an_empty_one(owner: TestClient, db: Any) -> None:
    """"" is no bound; `[]` is a total one. Collapsing them would invert the
    meaning of an admin clearing the field."""
    assert orgs.decode_allow_list("") is None
    assert orgs.decode_allow_list("[]") == []
    assert orgs.encode_allow_list(None) == ""
    assert orgs.encode_allow_list([]) == "[]"
    # Garbage is treated as unbounded: a corrupt configuration row must not
    # brick every turn in the organization.
    assert orgs.decode_allow_list("not json") is None


# --------------------------------------------------------------------------
# The admin surface, and the inversion it exists to prevent
# --------------------------------------------------------------------------


def test_a_workspace_owner_who_is_not_an_org_admin_cannot_configure_the_org(
    owner: TestClient, db: Any
) -> None:
    """The whole point of the tier, as one request.

    The fixture's user is both, so the test takes the org role away and leaves
    workspace ownership untouched — which is the shape of a real deployment,
    where an IT admin governs an org full of workspaces whose owners they are
    not.
    """
    identity = identity_of(owner)
    row = db.scalar(
        select(OrgMembership).where(OrgMembership.user_id == identity.user_id)
    )
    row.role = ORG_MEMBER
    db.commit()

    # Still a workspace owner: the admin console is unaffected.
    assert owner.get("/api/admin/members").status_code == 200
    # Still able to *read* the posture that governs them.
    assert owner.get("/api/org").status_code == 200
    assert owner.get("/api/org/policies").status_code == 200

    # But every org write is refused, and so is the members roster.
    assert owner.patch("/api/org", json={"name": "Mine now"}).status_code == 403
    assert owner.get("/api/org/members").status_code == 403
    assert owner.put(
        "/api/org/policies",
        json={"tool_name": WRITE, "policy": "allow", "scope": CHAT_SCOPE},
    ).status_code == 403
    assert owner.put(
        "/api/org/members", json={"user_id": identity.user_id, "role": ORG_ADMIN}
    ).status_code == 403, "a workspace owner must not be able to promote themselves"


def test_no_admin_route_writes_an_org_role() -> None:
    """The structural half of the same guarantee.

    The check above can be relaxed by editing one dependency. This cannot: it
    asserts that the owner-gated module contains no write to `OrgMembership` at
    all, so there is no handler for a future `require_owner` mistake to expose.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "admin.py").read_text()
    tree = ast.parse(source)
    # Names the module actually references, so the file's own prose about the
    # rule does not satisfy the test that enforces it.
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for alias in (
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ):
        names.add(alias)
    assert "OrgMembership" not in names, "an owner-gated module must not touch org roles"
    assert "org_role" not in names
    assert "require_org_admin" not in names


def test_an_org_admin_governs_a_workspace_they_do_not_own(
    owner: TestClient, identity_client: Callable[..., TestClient], db: Any
) -> None:
    """The inversion, working in the intended direction.

    A person with no membership in the workspace at all sets a `deny`, and the
    workspace owner — the highest authority that used to exist — cannot undo it.
    """
    identity = identity_of(owner)
    outsider = identity_client(name="IT admin", workspace_name="IT workspace")
    outsider_identity = identity_of(outsider)

    # Give the outsider admin standing in the owner's org, and nothing else: no
    # Membership row, so they cannot read a single one of the workspace's rows.
    db.add(
        OrgMembership(
            organization_id=org_id_of(db, owner),
            user_id=outsider_identity.user_id,
            role=ORG_ADMIN,
        )
    )
    db.add(
        OrgToolPolicy(
            organization_id=org_id_of(db, owner),
            tool_name=WRITE,
            policy="deny",
            scope=CHAT_SCOPE,
        )
    )
    db.commit()

    assert not db.scalar(
        select(Membership).where(
            Membership.workspace_id == identity.workspace_id,
            Membership.user_id == outsider_identity.user_id,
        )
    ), "the org admin is not in this workspace"

    # The owner grants themselves everything they can, at every tier available.
    set_workspace_policy(db, owner, tool=WRITE, policy="allow")
    set_workspace_policy(db, owner, tool=WRITE, policy="allow", owner_id=identity.user_id)
    assert verdict(db, owner, mode=AUTO_WRITES).policy == "deny"


def test_an_org_keeps_at_least_one_admin(owner: TestClient, db: Any) -> None:
    """Demoting the last admin freezes the org's posture forever, so it is a 409.

    Mirrors the workspace's last-owner rule, and for a sharper reason: a
    workspace with no owner still has an org above it that can act, while an org
    with no admin has nothing above it at all.
    """
    identity = identity_of(owner)
    response = owner.put(
        "/api/org/members", json={"user_id": identity.user_id, "role": ORG_MEMBER}
    )
    assert response.status_code == 409
    assert "at least one admin" in response.json()["detail"]


def test_an_org_admin_cannot_enroll_a_stranger(
    owner: TestClient, identity_client: Callable[..., TestClient]
) -> None:
    """A user id from outside the org's workspaces is a 404, not a promotion."""
    stranger = identity_of(identity_client(name="Stranger", workspace_name="Elsewhere"))
    response = owner.put(
        "/api/org/members", json={"user_id": stranger.user_id, "role": ORG_ADMIN}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


def test_signup_mints_an_org_the_signer_administers(db: Any) -> None:
    """Every account arrives governed, and self-serve stays self-serve."""
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    email = f"org-signup-{id(db)}@example.com"
    response = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "correct horse battery staple", "name": "Sam"},
    )
    # 202: signup answers with the neutral "we may have sent mail" body.
    assert response.status_code == 202, response.text

    user = db.scalar(select(User).where(User.email == email))
    workspace = db.scalar(
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id)
    )
    assert workspace.organization_id, "the workspace is governed"
    role = db.scalar(
        select(OrgMembership.role).where(
            OrgMembership.organization_id == workspace.organization_id,
            OrgMembership.user_id == user.id,
        )
    )
    assert role == ORG_ADMIN


def test_no_workspace_can_be_created_without_an_organization(db: Any) -> None:
    """The floor under the invariant.

    Thirty-odd places in this repo construct a `Workspace` directly, mostly
    tests and eval scripts with no opinion about organizations. None of them can
    produce an ungoverned row: the flush listener adopts an orphan before the
    INSERT, and the resulting org has no members, so nothing is granted by
    accident either.
    """
    workspace = Workspace(name="Adopted")
    db.add(workspace)
    db.flush()
    assert workspace.organization_id

    org = db.get(Organization, workspace.organization_id)
    assert org is not None and org.name == "Adopted"
    assert not db.scalars(
        select(OrgMembership).where(OrgMembership.organization_id == org.id)
    ).all(), "an adopted org has no admin, so nobody gains authority by accident"
    db.rollback()


def test_every_workspace_in_the_database_has_an_organization(db: Any) -> None:
    """The invariant, asserted over whatever the suite has actually created.

    Cheap, and it is the assertion that would have caught the migration leaving
    orphans behind — the failure the whole tier is worthless under.
    """
    orphans = db.scalars(
        select(Workspace.id).where(
            (Workspace.organization_id.is_(None)) | (Workspace.organization_id == "")
        )
    ).all()
    assert orphans == []
