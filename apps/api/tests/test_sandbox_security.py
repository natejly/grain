"""The four claims ADR 0005 makes about the execution sandbox, tested.

Every other sandbox test file asks whether a feature works. This one asks whether
the feature is safe to have shipped, and each section maps to a sentence in the
ADR that would otherwise be prose:

1. **Cross-tenant.** "A sandbox's provider-side id is reachable only through a
   `sandbox_sessions` row selected by `workspace_id`." Proved over HTTP for every
   sandbox route the app exposes — enumerated from `app.openapi()` rather than
   listed here, so a route added next quarter without a workspace filter fails
   this file instead of slipping past it — and in-process for every agent tool
   that accepts a session id, enumerated from the tool registry for the same
   reason.
2. **No secret leaks.** "Nothing from `Settings` is forwarded." Driven by
   introspecting the `Settings` model for `SecretStr` fields, so a credential
   added a year from now is covered the day it is added, without anyone
   remembering this file exists.
3. **Egress floor.** "Cloud metadata and link-local ranges are denied in every
   mode including `open` — that denial is not a policy an operator can switch
   off."
4. **Fail closed.** A provider that provides no isolation, or one that cannot
   reach its backend, must refuse at startup rather than on the turn where
   someone asks it to plot a CSV.

The positive controls matter as much as the refusals. Every one of these tests
would also pass against a sandbox feature that was simply broken, so each section
ends by proving the allowed case still works.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, get_args

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.database import SessionLocal
from app.main import app
from app.models import SandboxExecution, SandboxSession, Source, new_id
from app.services.ingestion import object_path
from app.services.llm_tools import ToolContext, build_registry
from app.services.sandbox import policy
from app.services.sandbox import session as session_module
from app.services.sandbox import tools as tools_module
from app.services.sandbox.fake import FakeProvider
from app.services.sandbox.policy import ALL_TRAFFIC, ALWAYS_DENIED_CIDRS, egress_rules
from app.services.sandbox.provider import get_provider
from app.services.sandbox.session import ensure_session
from app.services.sandbox.tools import registry_tools
from app.services.sandbox.types import NetworkPolicy, SandboxError, SandboxSpec

# --------------------------------------------------------------------------
# Two tenants, each with a sandbox


@dataclass(frozen=True)
class Tenant:
    """A workspace with a sandbox session and one execution recorded against it."""

    label: str
    identity: Identity
    client: TestClient
    session_id: str
    execution_id: str

    @property
    def workspace_id(self) -> str:
        return self.identity.workspace_id

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    def markers(self) -> List[str]:
        """Everything of this tenant's that must never appear in someone else's
        response: the ids that address its machine, and the content inside it."""
        return [
            self.workspace_id,
            self.user_id,
            self.session_id,
            self.execution_id,
            f"{self.label} secret",
        ]


def _build_tenant(label: str) -> Tenant:
    """A workspace whose sandbox row is written straight to the table.

    Creating one over HTTP would need a live provider, and the property under
    test — whether another workspace can name this row — does not depend on a
    machine existing behind it. The label and the recorded stdout carry the
    tenant's name so a cross-tenant *read* is caught by the same string scan as
    a cross-tenant id.
    """
    identity = create_identity(
        name=f"{label} owner", workspace_name=f"{label} workspace"
    )
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    db = SessionLocal()
    try:
        session = SandboxSession(
            id=new_id(),
            workspace_id=identity.workspace_id,
            # A project id no tool asks for, so `ensure_session`'s default
            # (project_id="") never reuses this row. It names no machine at any
            # provider — reusing it would make the positive controls below fail
            # for a reason that has nothing to do with what they test.
            project_id="security-fixture",
            created_by=identity.user_id,
            provider="fake",
            external_id=f"{label.lower()}-{new_id()}",
            label=f"{label} secret sandbox",
            status="running",
            network_policy="none",
            allow_hosts_json="[]",
        )
        db.add(session)
        db.flush()
        execution = SandboxExecution(
            id=new_id(),
            workspace_id=identity.workspace_id,
            session_id=session.id,
            kind="code",
            source=f"print('{label} secret code')",
            stdout=f"{label} secret output",
            exit_code=0,
        )
        db.add(execution)
        db.commit()
        return Tenant(
            label=label,
            identity=identity,
            client=client,
            session_id=session.id,
            execution_id=execution.id,
        )
    finally:
        db.close()


@pytest.fixture(scope="module")
def attacker() -> Tenant:
    return _build_tenant("Attacker")


@pytest.fixture(scope="module")
def victim() -> Tenant:
    return _build_tenant("Victim")


def _sandbox_digest(workspace_id: str) -> str:
    """A hash of every sandbox row a workspace owns, for the tamper check.

    The per-response leak assertions catch a route that *returns* someone else's
    machine. They cannot catch one that refuses in its body after having already
    run something in it, which is the more expensive failure.
    """
    digest = hashlib.sha256()
    db = SessionLocal()
    try:
        for model in (SandboxSession, SandboxExecution):
            rows = (
                db.query(model)
                .filter(model.workspace_id == workspace_id)
                .order_by(model.id)
                .all()
            )
            for row in rows:
                digest.update(
                    repr(
                        {
                            column.name: getattr(row, column.name)
                            for column in model.__table__.columns
                        }
                    ).encode()
                )
    finally:
        db.close()
    return digest.hexdigest()


def _assert_no_leak(text: str, target: Tenant, label: str, supplied: str = "") -> None:
    """No marker of `target`'s may appear in `text`.

    A value the caller itself passed in is exempt: a route that says "no such
    session <the id you just sent me>" is echoing the attacker's own argument,
    which teaches nothing. Anything the caller did not supply, it must not have
    learned. Same rule as `test_tenant_isolation.assert_tool_leaked_nothing`.
    """
    for marker in target.markers():
        if marker in supplied:
            continue
        assert marker not in text, f"{label} leaked {marker!r}"


# --------------------------------------------------------------------------
# 1. Cross-tenant: the HTTP surface


def _sandbox_operations() -> List[Tuple[str, str]]:
    """Every sandbox operation the app exposes, from the app's own route table.

    Enumerated rather than listed so this file cannot fall behind the router: a
    route added later is probed the day it is added, and one that forgets its
    workspace filter fails here rather than shipping.
    """
    spec = app.openapi()
    return sorted(
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        if path == "/api/sandbox" or path.startswith("/api/sandbox/")
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    )


#: A body wide enough to satisfy every sandbox route's schema at once. Extra
#: fields are ignored by pydantic, so one dict serves them all, and a route whose
#: required fields are missing from it answers 422 — which the sweep treats as a
#: failure rather than a pass, because a body rejected before the lookup proves
#: nothing about the lookup.
PROBE_BODY: Dict[str, Any] = {
    "source": "print(open('/etc/passwd').read())",
    "kind": "code",
    "language": "python",
    "project_id": "",
    "label": "",
}


def _fill(path: str, target: Tenant) -> str:
    """Point a route template at the victim's rows."""
    filled = path
    for name in re.findall(r"{([^}]+)}", path):
        value = (
            target.execution_id if "execution" in name.lower() else target.session_id
        )
        filled = filled.replace("{" + name + "}", value)
    return filled


def _probe(method: str, path: str, caller: Tenant, target: Tenant):
    url = _fill(path, target)
    return caller.client.request(
        method,
        url,
        json=PROBE_BODY if method in {"POST", "PUT", "PATCH"} else None,
        headers={
            "Idempotency-Key": "sandbox-security-"
            + hashlib.sha1(f"{method} {path}".encode()).hexdigest()
        },
    )


def test_the_route_enumeration_actually_found_the_routes():
    """A filter that matched nothing would make every sweep below vacuous."""
    operations = _sandbox_operations()
    assert operations, "no sandbox routes found in the app's route table"
    assert any("{" in path for _method, path in operations), (
        "no parameterised sandbox route found — the cross-tenant sweep below "
        "would then be testing nothing"
    )


@pytest.mark.parametrize(
    "method,path", _sandbox_operations(), ids=lambda value: str(value)
)
def test_no_sandbox_route_serves_another_workspace(
    method: str, path: str, attacker: Tenant, victim: Tenant
):
    response = _probe(method, path, attacker, victim)
    label = f"{method} {path}"

    # 500 is the one status nothing may produce: it means an attacker-supplied
    # id reached an unhandled exception rather than a filter.
    assert response.status_code != 500, f"{label} raised: {response.text[:400]}"

    if "{" in path:
        assert response.status_code == 404, (
            f"{label} answered {response.status_code} for another tenant's "
            f"sandbox; expected 404. A 422 means PROBE_BODY above is missing a "
            f"field this route requires, so the body was rejected before the "
            f"workspace filter ran and this case proved nothing. "
            f"Body: {response.text[:400]}"
        )
    _assert_no_leak(
        response.text or "", victim, label, supplied=_fill(path, victim)
    )


def test_a_foreign_sandbox_is_indistinguishable_from_a_missing_one(
    attacker: Tenant, victim: Tenant
):
    """404, and the *same* 404. A 403 on the foreign id would confirm that the
    id names a real machine in someone else's workspace, which is precisely the
    fact worth guessing ids to learn."""
    absent = "00000000-0000-4000-8000-0000000000ff"
    for method, path in _sandbox_operations():
        if "{" not in path:
            continue
        foreign = _probe(method, path, attacker, victim)
        missing = attacker.client.request(
            method,
            path.replace("{session_id}", absent).replace("{execution_id}", absent),
            json=PROBE_BODY if method in {"POST", "PUT", "PATCH"} else None,
        )
        assert foreign.status_code == missing.status_code == 404, (
            f"{method} {path}: foreign={foreign.status_code} "
            f"missing={missing.status_code}"
        )
        assert foreign.json() == missing.json(), (
            f"{method} {path} tells a foreign id apart from a missing one"
        )


def test_the_sweep_changed_nothing_of_the_victims(attacker: Tenant, victim: Tenant):
    """Runs the whole sweep again and digests the victim's rows either side.

    A route that refuses in its response but paused, killed or ran something on
    the way there would pass every assertion above.
    """
    before = _sandbox_digest(victim.workspace_id)
    for method, path in _sandbox_operations():
        _probe(method, path, attacker, victim)
    assert _sandbox_digest(victim.workspace_id) == before, (
        "the sandbox route sweep changed the victim's sessions or executions"
    )


def test_the_listing_route_answers_only_from_the_callers_own_rows(
    attacker: Tenant, victim: Tenant
):
    """The negative controls above would pass if the router were broken; this is
    the positive one. A's own session must be there, and only A's."""
    response = attacker.client.get("/api/sandbox")
    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()}
    assert attacker.session_id in ids
    assert victim.session_id not in ids
    _assert_no_leak(response.text, victim, "GET /api/sandbox")


def test_the_external_id_never_leaves_the_server(attacker: Tenant):
    """The provider-side id is the one value that addresses a machine without a
    row, so it is the one value a leaked response must not carry."""
    db = SessionLocal()
    try:
        row = db.get(SandboxSession, attacker.session_id)
        assert row is not None
        external_id = row.external_id
    finally:
        db.close()
    assert external_id
    for response in (
        attacker.client.get("/api/sandbox"),
        attacker.client.get(f"/api/sandbox/{attacker.session_id}/executions"),
    ):
        assert response.status_code == 200, response.text
        assert external_id not in response.text


# --------------------------------------------------------------------------
# 1b. Cross-tenant: the agent tools, which never touch HTTP


def _sandbox_settings(**overrides: Any) -> Settings:
    """Sandbox-enabled settings on the fake provider.

    `_env_file=None` because the repo's `.env` carries a real key and a real
    provider selection; reading it would make this suite assert the developer's
    laptop rather than the code.
    """
    base: Dict[str, Any] = dict(
        _env_file=None,
        app_env="development",
        model_provider="openai",
        openai_api_key="test-key",
        sandbox_enabled=True,
        sandbox_provider="fake",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def agent(monkeypatch, attacker: Tenant):
    """The attacker's tool registry, on a fake provider that really does run.

    `get_provider` is lru_cached on a settings tuple, so patching the name each
    module reached for is cleaner than fighting the cache — and it keeps one
    provider instance behind both the tools and session creation, which is what
    makes the positive control below meaningful.
    """
    fake = FakeProvider()
    settings = _sandbox_settings()
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    monkeypatch.setattr(tools_module, "get_provider", lambda _settings: fake)
    monkeypatch.setattr(session_module, "get_provider", lambda _settings: fake)
    db = SessionLocal()
    context = ToolContext(
        workspace_id=attacker.workspace_id,
        user_id=attacker.user_id,
        conversation_id="",
    )
    try:
        yield db, context, registry_tools(db, context)
    finally:
        db.rollback()
        # Sessions these tools created, and only those: the fixture row above is
        # module-scoped and the tests after this one still name it.
        db.query(SandboxExecution).filter(
            SandboxExecution.workspace_id == attacker.workspace_id,
            SandboxExecution.id != attacker.execution_id,
        ).delete()
        db.query(SandboxSession).filter(
            SandboxSession.workspace_id == attacker.workspace_id,
            SandboxSession.project_id != "security-fixture",
        ).delete()
        db.commit()
        db.close()


#: Arguments that carry each session-taking tool all the way to the point where
#: it resolves a session. A probe that fails earlier — on a missing `code`, or an
#: unknown filename — never reaches the workspace filter and so proves nothing,
#: which is why these are written out per tool rather than synthesised. The test
#: below asserts this map covers every session-taking tool in the registry, so a
#: new one cannot join the registry without one.
TOOL_PROBES: Dict[str, Dict[str, Any]] = {
    "run_python": {"code": "print('stolen')"},
    "run_command": {"command": "cat /home/user/*.csv"},
    "sandbox_upload": {"paths": ["attacker.csv"]},
    "sandbox_download": {"path": "victim.csv"},
}


def _session_taking_tools(registry: Dict[str, Any]) -> List[str]:
    return sorted(
        name
        for name, spec in registry.items()
        if "session" in spec.parameters.get("properties", {})
    )


@pytest.fixture
def uploaded_source(attacker: Tenant):
    """A real file in the attacker's workspace, so `sandbox_upload`'s probe gets
    past "no source named that" and reaches the session lookup."""
    db = SessionLocal()
    try:
        source = Source(
            id=new_id(),
            workspace_id=attacker.workspace_id,
            created_by=attacker.user_id,
            filename="attacker.csv",
            media_type="text/csv",
            object_key="",
            byte_size=6,
            status="ready",
        )
        path = object_path(attacker.workspace_id, source.id, source.filename)
        path.write_bytes(b"a,b\n1,2")
        source.object_key = str(path)
        db.add(source)
        db.commit()
        yield source.id
        db.query(Source).filter(Source.id == source.id).delete()
        db.commit()
    finally:
        db.close()


def test_every_session_taking_tool_has_a_probe(agent):
    """Introspection is the point: a tool added later must be probed, and the
    only way to notice it was not is to compare the registry against this map."""
    _db, _context, registry = agent
    covered = set(TOOL_PROBES)
    live = set(_session_taking_tools(registry))
    assert live, "no session-taking sandbox tools found in the registry"
    assert live - covered == set(), (
        "sandbox tools accept a session id but have no cross-tenant probe: "
        f"{sorted(live - covered)}. Add one to TOOL_PROBES."
    )
    assert covered - live == set(), f"probes for tools that no longer exist: "\
        f"{sorted(covered - live)}"


@pytest.mark.parametrize("argument", ["session", "session_id"])
@pytest.mark.parametrize("tool", sorted(TOOL_PROBES))
def test_no_agent_tool_reaches_another_workspaces_sandbox(
    agent, uploaded_source, victim: Tenant, tool: str, argument: str
):
    """Both spellings, because `_session_for` accepts either and a filter applied
    to only one of them is a filter that is not there."""
    db, context, registry = agent
    before = _sandbox_digest(victim.workspace_id)
    args = dict(TOOL_PROBES[tool])
    args[argument] = victim.session_id

    output = registry[tool].executor(db, context, args).content

    assert "No sandbox session" in output, (
        f"{tool} did not refuse another workspace's session: {output[:300]}"
    )
    _assert_no_leak(output, victim, tool, supplied=json.dumps(args))
    assert _sandbox_digest(victim.workspace_id) == before, (
        f"{tool} changed the victim's sandbox rows"
    )


def test_the_tool_previews_describe_no_foreign_session(agent, victim: Tenant):
    """The approval card renders before anyone approves anything, so it runs on
    unvalidated arguments — including a session id from another workspace. It
    must still not read that row's policy out."""
    db, context, registry = agent
    for name in _session_taking_tools(registry):
        spec = registry[name]
        if spec.preview is None:
            continue
        args = dict(TOOL_PROBES[name])
        args["session"] = victim.session_id
        rendered = spec.preview(db, context, args)
        _assert_no_leak(rendered, victim, f"{name} preview", supplied=json.dumps(args))


def test_list_sandboxes_shows_only_the_callers_machines(agent, victim: Tenant):
    db, context, registry = agent
    output = registry["list_sandboxes"].executor(db, context, {}).content
    _assert_no_leak(output, victim, "list_sandboxes")


def test_the_tools_do_run_in_the_callers_own_sandbox(agent, attacker: Tenant):
    """The positive control for this whole section.

    Every refusal above would also be produced by a registry that simply cannot
    run anything, so prove the allowed path works: no session named, a sandbox is
    created in the caller's own workspace, and the code runs in it.
    """
    db, context, registry = agent
    output = registry["run_python"].executor(
        db, context, {"code": "print('mine')"}
    ).content
    assert "mine" in output, output
    owned = (
        db.query(SandboxSession)
        .filter(SandboxSession.workspace_id == attacker.workspace_id)
        .count()
    )
    assert owned >= 1


# --------------------------------------------------------------------------
# 2. No secret leaks


def _mentions_secret(annotation: Any) -> bool:
    """True if `SecretStr` appears anywhere in a type annotation.

    Recursive over `get_args` rather than an equality check, because every secret
    on `Settings` today is `Optional[SecretStr]` and the next one may be a list
    or a union of something else again.
    """
    if annotation is SecretStr:
        return True
    return any(_mentions_secret(arg) for arg in get_args(annotation))


def _secret_fields() -> List[str]:
    return sorted(
        name
        for name, field in Settings.model_fields.items()
        if _mentions_secret(field.annotation)
    )


def _sentinel(name: str) -> str:
    """A value that cannot occur by accident, so a substring hit is a real leak
    rather than a collision with a default."""
    return f"SENTINEL-{name.upper()}-e3b0c44298fc"


def _settings_with_every_secret_set() -> Settings:
    overrides = {name: _sentinel(name) for name in _secret_fields()}
    return _sandbox_settings(**overrides)


def test_the_introspection_finds_the_secrets_it_is_meant_to():
    """A `_secret_fields` that silently returned nothing would make both tests
    below pass while checking nothing at all."""
    fields = _secret_fields()
    assert "openai_api_key" in fields
    assert "sandbox_api_key" in fields
    # Not an exhaustive list on purpose — the point of the introspection is that
    # nobody has to maintain one — but enough to prove it is looking.
    assert len(fields) >= 5, fields


def test_no_settings_secret_reaches_the_sandbox_environment():
    """`sandbox_env` is built, never filtered. This is the test the module's
    docstring promises, and it is why adding a credential to Settings next year
    cannot quietly hand it to generated code."""
    settings = _settings_with_every_secret_set()
    env = policy.sandbox_env(settings)
    haystack = json.dumps(env)

    for name in _secret_fields():
        assert _sentinel(name) not in haystack, f"{name} leaked into the sandbox env"
    assert settings.database_url not in haystack, "DATABASE_URL leaked into the env"
    # The URL is anchored to an absolute path by a validator, so also check the
    # unanchored form nobody would think to look for.
    assert "workspace.db" not in haystack


def test_no_sandbox_environment_variable_is_even_named_like_a_credential():
    """A weaker check than the one above and a more general one: it catches a
    secret that arrives under a value we did not plant."""
    env = policy.sandbox_env(_settings_with_every_secret_set())
    for key in env:
        upper = key.upper()
        assert not any(
            word in upper
            for word in ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "CREDENTIAL", "DSN")
        ), f"{key} is named like a credential"


class _RecordingProvider(FakeProvider):
    """A fake that keeps every spec it was handed, so a test can read the exact
    object `ensure_session` builds rather than a reconstruction of it."""

    def __init__(self) -> None:
        super().__init__()
        self.specs: List[SandboxSpec] = []

    def create(self, spec: SandboxSpec):
        self.specs.append(spec)
        return super().create(spec)


def test_no_settings_secret_reaches_the_sandbox_spec(monkeypatch, attacker: Tenant):
    """The env is only half of it: the spec also carries a template, metadata and
    an allowlist, and any of those is somewhere a secret could be pasted."""
    settings = _settings_with_every_secret_set()
    recorder = _RecordingProvider()
    monkeypatch.setattr(session_module, "get_provider", lambda _settings: recorder)

    db = SessionLocal()
    try:
        session = ensure_session(
            db,
            workspace_id=attacker.workspace_id,
            user_id=attacker.user_id,
            settings=settings,
            project_id=f"secret-probe-{new_id()}",
        )
        assert recorder.specs, "ensure_session created no sandbox"
        spec = recorder.specs[-1]
        haystack = "\n".join(
            [repr(spec), json.dumps(dict(spec.env)), json.dumps(dict(spec.metadata))]
        )
        for name in _secret_fields():
            assert _sentinel(name) not in haystack, f"{name} leaked into the spec"
        assert settings.database_url not in haystack
        # Metadata is provider-side labelling and shows up in a vendor console,
        # so it is held to the same rule and pinned to exactly what it needs.
        assert set(spec.metadata) == {"workspace_id", "user_id", "app"}
    finally:
        db.query(SandboxSession).filter(
            SandboxSession.workspace_id == attacker.workspace_id,
            SandboxSession.id == session.id,
        ).delete()
        db.commit()
        db.close()


# --------------------------------------------------------------------------
# 3. The egress floor


ALL_POLICIES: Tuple[NetworkPolicy, ...] = get_args(NetworkPolicy)


def test_the_policy_enumeration_matches_the_setting():
    """The floor is only a floor if it covers every value an operator can set."""
    field = Settings.model_fields["sandbox_network_policy"]
    assert set(get_args(field.annotation)) == set(ALL_POLICIES)


@pytest.mark.parametrize("network_policy", ALL_POLICIES)
def test_the_denied_ranges_are_unreachable_under_every_policy(
    network_policy: NetworkPolicy,
):
    """Metadata and private space are denied in every mode, `open` included.

    Two shapes satisfy that and both are accepted here, because the property is
    "no route to those addresses", not "a particular list of strings":
    `open`/`allowlist` deny the ranges explicitly, and `none` disables egress
    outright. What is *not* accepted is a policy that does neither.
    """
    internet, _allow, deny = egress_rules(network_policy, ["pypi.org"])
    if internet:
        for cidr in ALWAYS_DENIED_CIDRS:
            assert cidr in deny, f"{network_policy} does not deny {cidr}"
    else:
        assert deny == [ALL_TRAFFIC] or all(
            cidr in deny for cidr in ALWAYS_DENIED_CIDRS
        ), f"{network_policy} disables internet but denies nothing"


def test_open_denies_the_metadata_endpoint_specifically():
    """169.254.169.254 is the address this whole list exists for: it is the cloud
    metadata endpoint on AWS, GCP and Azure alike, and the standard first move of
    an SSRF into credential theft. Asserted by containment rather than by string
    match, so widening 169.254.0.0/16 to a narrower range would fail here."""
    _internet, _allow, deny = egress_rules("open", [])
    metadata = ipaddress.ip_address("169.254.169.254")
    networks = [ipaddress.ip_network(cidr) for cidr in deny if "/" in cidr]
    assert any(
        metadata in network
        for network in networks
        if network.version == metadata.version
    ), f"the metadata endpoint is reachable under `open`: {deny}"


def test_the_denied_ranges_cover_both_address_families():
    """An IPv4-only deny list is how this kind of control usually fails: the
    host has an IPv6 address, the metadata service answers on fd00::/8, and the
    allowlist everyone reviewed never mentioned it."""
    versions = {ipaddress.ip_network(cidr).version for cidr in ALWAYS_DENIED_CIDRS}
    assert versions == {4, 6}, f"denied ranges cover only IPv{versions}"


def test_an_empty_allowlist_permits_nothing():
    """The failure mode this ordering exists to prevent: an operator switches to
    `allowlist`, leaves SANDBOX_HOST_ALLOWLIST empty, and gets full egress
    because "no rules" was read as "no restrictions"."""
    internet, allow, deny = egress_rules("allowlist", [])
    assert allow == [], "an empty allowlist produced permit rules"
    assert deny[0] == ALL_TRAFFIC, (
        "deny-all must come first, so the named hosts are exceptions to a "
        f"closed default rather than the only rules: {deny}"
    )
    assert internet is True  # the flag stays on; the allowlist is what restricts.


def test_an_allowlist_permits_only_what_it_names():
    internet, allow, deny = egress_rules("allowlist", ["pypi.org", "files.example"])
    assert internet is True
    assert allow == ["pypi.org", "files.example"]
    assert deny[0] == ALL_TRAFFIC
    for cidr in ALWAYS_DENIED_CIDRS:
        assert cidr in deny


def test_policy_none_disables_the_internet_outright():
    """The default, and the reason this feature has no exfiltration story: with
    no route out, prompt-injected code has nowhere to send what it can read."""
    internet, allow, deny = egress_rules("none", ["pypi.org"])
    assert internet is False
    assert allow == [], "policy `none` produced permit rules"
    assert ALL_TRAFFIC in deny
    # Even a host named in the allowlist setting is not reachable: the policy,
    # not the list, is what decides.
    assert "pypi.org" not in allow


def test_the_shipped_default_is_the_closed_one():
    """A default is a decision. If this ever flips to `open`, it should flip in a
    diff that also edits this line."""
    assert Settings.model_fields["sandbox_network_policy"].default == "none"


# --------------------------------------------------------------------------
# 4. Fail closed


def _production(**overrides: Any) -> Settings:
    base: Dict[str, Any] = dict(
        _env_file=None,
        app_env="production",
        model_provider="openai",
        openai_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize("driver", ["fake", "subprocess"])
def test_a_driver_that_isolates_nothing_cannot_boot_in_production(driver: str):
    """`fake` executes nothing and would silently tell users their code ran;
    `subprocess` runs generated code as this process's own user, with this
    process's access to `.env`, the database file and `~/.aws`. Neither is
    merely defaulted off — the same structural gate that stops
    MODEL_PROVIDER=scripted and DEV_AUTO_LOGIN stops them."""
    with pytest.raises(ValidationError) as caught:
        _production(sandbox_enabled=True, sandbox_provider=driver)
    assert "development or test" in str(caught.value)


@pytest.mark.parametrize("environment", ["production", "development"])
def test_e2b_without_a_key_refuses_to_construct(environment: str):
    """A sandbox that cannot reach its provider says so at startup, not on the
    turn where someone asks it to plot a CSV."""
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            app_env=environment,
            model_provider="openai",
            openai_api_key="test-key",
            sandbox_enabled=True,
            sandbox_provider="e2b",
        )
    assert "SANDBOX_API_KEY" in str(caught.value)


def test_a_configured_provider_does_boot_in_production():
    """The positive control: the gate refuses two named drivers, not everything.
    A guard that refused every configuration would pass both tests above."""
    for driver, extra in (
        ("e2b", {"sandbox_api_key": "sk-test"}),
        ("container", {}),
    ):
        settings = _production(sandbox_enabled=True, sandbox_provider=driver, **extra)
        assert settings.sandbox_ready is True


def test_the_default_configuration_is_off_and_stays_off():
    """`SANDBOX_ENABLED` is the only relaxation in config.py that is not a
    fail-closed inversion, because "off" and "safe" are the same state here. It
    still has to actually be off."""
    settings = _production()
    assert settings.sandbox_enabled is False
    assert settings.sandbox_ready is False


def test_a_disabled_sandbox_hands_out_no_provider():
    """Even if a route were reached with the sandbox off, nothing can be created:
    the refusal is at the driver seam, not only in the registry."""
    with pytest.raises(SandboxError) as caught:
        get_provider(_production())
    assert "turned off" in str(caught.value) or "SANDBOX_ENABLED" in str(caught.value)


def test_a_disabled_sandbox_puts_no_execution_tool_in_front_of_the_model(
    monkeypatch, attacker: Tenant
):
    """The whole registry, not just `registry_tools`: a sandbox tool wired in
    from somewhere else would still be a tool the model can call."""
    db = SessionLocal()
    context = ToolContext(
        workspace_id=attacker.workspace_id,
        user_id=attacker.user_id,
        conversation_id="",
    )
    try:
        monkeypatch.setattr(tools_module, "get_settings", lambda: _sandbox_settings())
        enabled = set(registry_tools(db, context))
        assert enabled, "the enabled registry is empty; this test would be vacuous"

        monkeypatch.setattr(
            tools_module,
            "get_settings",
            lambda: _sandbox_settings(sandbox_enabled=False, sandbox_provider="fake"),
        )
        assert registry_tools(db, context) == {}
        assert enabled & set(build_registry(db, context)) == set()
    finally:
        db.close()
