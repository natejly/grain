"""The agent-facing execution tools (ADR 0005).

Everything here runs on `SANDBOX_PROVIDER=fake` with scripted results. Two
properties get most of the attention, because they are the two that decide
whether this feature is safe and whether the model can use it at all:

* a failure is a *result*. A traceback, an oversize download, a rejected path
  and a spent quota all come back as text the model can read and act on. None of
  them raises, because an exception out of an executor is rendered to the model
  as "tool failed" and costs a turn.
* a write tool is approval-gated, and its preview never dies. The approval card
  is the only place a human sees what is about to run, so a preview that raises
  on a missing argument silently downgrades the whole review to a blank box.
"""
from __future__ import annotations

import base64
import json
import struct

import pytest
from conftest import create_identity

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    Conversation,
    Run,
    SandboxExecution,
    SandboxSecret,
    SandboxSession,
    Source,
    new_id,
)
from app.services.ingestion import object_path
from app.services.llm_tools import ToolContext
from app.services.projects import store
from app.services.sandbox import session as session_module
from app.services.sandbox import tools as tools_module
from app.services.sandbox.fake import FakeProvider
from app.services.sandbox.outputs import MAX_ARTIFACTS_PER_EXECUTION, persist_artifacts
from app.services.sandbox.session import ensure_session, handle_for
from app.services.sandbox.tools import SANDBOX_HOME, registry_tools
from app.services.sandbox.types import Artifact, ExecResult


def _settings(**overrides) -> Settings:
    """A sandbox-enabled Settings on the fake provider.

    `_env_file=None` because the repo's .env carries a real key and a real
    provider selection; a test that reads it asserts the developer's laptop
    rather than the code.
    """
    base = dict(
        _env_file=None,
        app_env="development",
        model_provider="openai",
        openai_api_key="test-key",
        sandbox_enabled=True,
        sandbox_provider="fake",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _png(width: int, height: int) -> str:
    """A PNG header, base64'd. Enough for the artifact summary to read a size
    off it without dragging an image library into the test suite."""
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    return base64.b64encode(header + struct.pack(">II", width, height) + b"\x08\x02").decode()


@pytest.fixture(scope="module")
def identity():
    return create_identity(workspace_name="Sandbox tools")


@pytest.fixture
def db(identity):
    """A session, with every row these tests wrote removed afterwards."""
    handle = SessionLocal()
    try:
        yield handle
    finally:
        handle.query(SandboxExecution).delete()
        handle.query(SandboxSession).delete()
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
            prompt="analyse this",
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
    """One in-memory provider behind both the tools and session creation.

    `get_provider` is lru_cached on the settings triple, so patching the name
    each module reached for is cleaner than fighting the cache.
    """
    fake = FakeProvider()
    monkeypatch.setattr(tools_module, "get_provider", lambda settings: fake)
    monkeypatch.setattr(session_module, "get_provider", lambda settings: fake)
    return fake


@pytest.fixture
def tools(monkeypatch, db, context, provider):
    """The registry, on sandbox-enabled fake settings."""
    settings = _settings()
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    return registry_tools(db, context)


def _run(tools, db, context, name: str, **args) -> str:
    return tools[name].executor(db, context, args).content


# --- run_python -----------------------------------------------------------


def test_run_python_renders_stdout(tools, db, context):
    output = _run(tools, db, context, "run_python", code='print("hello from the vm")')
    assert "hello from the vm" in output
    assert "stdout" in output


def test_run_python_reuses_one_session_across_calls(tools, db, context, provider):
    """The interpreter is persistent, and that only helps if consecutive calls
    land in the same machine — otherwise turn two cannot see turn one's dataframe."""
    _run(tools, db, context, "run_python", code='print("a")')
    _run(tools, db, context, "run_python", code='print("b")')
    assert len([call for call in provider.calls if call[0] == "create"]) == 1


def test_a_traceback_is_a_result_the_model_can_act_on(tools, db, context, provider):
    """User code failing is not an infrastructure failure. It must not raise: an
    exception out of an executor reaches the model as "tool failed", which tells
    it nothing about the NameError it can actually fix."""
    provider.script(
        ExecResult(
            stdout="loading\n",
            error="NameError: name 'df' is not defined",
            traceback="Traceback (most recent call last):\n  File <cell>, line 1",
            exit_code=1,
        )
    )
    output = _run(tools, db, context, "run_python", code="df.head()")
    assert "NameError" in output
    assert "Traceback (most recent call last)" in output
    assert "loading" in output


def test_a_dead_provider_is_a_sentence_not_a_stack_trace(tools, db, context, monkeypatch):
    class _Broken:
        name = "fake"

        def create(self, spec):
            raise RuntimeError("connect ECONNREFUSED https://api.e2b.dev?key=sk-secret")

    monkeypatch.setattr(session_module, "get_provider", lambda settings: _Broken())
    output = _run(tools, db, context, "run_python", code='print("hi")')
    assert "sk-secret" not in output and "api.e2b.dev" not in output
    assert "Traceback" not in output


def test_missing_code_is_refused_without_touching_the_provider(tools, db, context, provider):
    assert "Error" in _run(tools, db, context, "run_python", code="   ")
    assert provider.calls == []


# --- artifacts ------------------------------------------------------------


def test_artifacts_are_persisted_and_referenced(tools, db, context, identity, provider):
    provider.script(
        ExecResult(
            stdout="plotted\n",
            artifacts=(
                Artifact(kind="png", mime="image/png", data=_png(480, 320), is_main=True),
                Artifact(
                    kind="chart",
                    mime="application/json",
                    data="",
                    chart_json=json.dumps({"type": "line", "elements": [{}, {}]}),
                ),
            ),
        )
    )
    output = _run(tools, db, context, "run_python", code="plot()")

    assert "image: png 480x320" in output
    assert "chart: line, 2 series" in output
    stored = db.query(Source).filter(Source.workspace_id == identity.workspace_id).all()
    assert len(stored) == 2
    for source in stored:
        # The id is in the result so the model can hand the user something to
        # open, and the bytes are really on disk, not just described.
        assert source.id in output
        assert source.byte_size > 0


def test_artifacts_are_stored_not_indexed(db, identity):
    """`status="ready"` means "ingested and indexed" everywhere else in this
    codebase. A figure never went through ingest, and retrieval must not be led
    to believe it can quote one."""
    session = SandboxSession(
        id=new_id(),
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        provider="fake",
        external_id="fake-1",
    )
    db.add(session)
    db.commit()
    descriptors = persist_artifacts(
        db,
        workspace_id=identity.workspace_id,
        session_id=session.id,
        result=ExecResult(artifacts=(Artifact(kind="png", mime="image/png", data=_png(8, 8)),)),
        settings=_settings(),
    )
    source = db.get(Source, descriptors[0]["id"])
    assert source is not None and source.status == "stored"
    assert source.created_by == identity.user_id


def test_a_plotting_loop_cannot_fill_the_workspace(db, identity):
    session = SandboxSession(
        id=new_id(),
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        provider="fake",
        external_id="fake-2",
    )
    db.add(session)
    db.commit()
    hundreds = tuple(
        Artifact(kind="png", mime="image/png", data=_png(4, 4)) for _ in range(200)
    )
    descriptors = persist_artifacts(
        db,
        workspace_id=identity.workspace_id,
        session_id=session.id,
        result=ExecResult(artifacts=hundreds),
        settings=_settings(),
    )
    assert len(descriptors) == MAX_ARTIFACTS_PER_EXECUTION


def test_an_undecodable_artifact_does_not_lose_the_run(tools, db, context, provider):
    provider.script(
        ExecResult(
            stdout="the number is 42\n",
            artifacts=(Artifact(kind="png", mime="image/png", data="not base64 at all!"),),
        )
    )
    output = _run(tools, db, context, "run_python", code="plot()")
    assert "the number is 42" in output
    assert "1 further artifact" in output


# --- run_command ----------------------------------------------------------


def test_run_command_reports_output_and_exit_code(tools, db, context, provider):
    provider.script(ExecResult(stdout="Successfully installed polars\n", exit_code=0))
    output = _run(tools, db, context, "run_command", command="pip install polars")
    assert "Successfully installed polars" in output
    assert [call for call in provider.calls if call[0] == "run_command"]


def test_a_failing_command_shows_its_exit_code(tools, db, context, provider):
    provider.script(ExecResult(stderr="No matching distribution\n", exit_code=1))
    output = _run(tools, db, context, "run_command", command="pip install nope")
    assert "No matching distribution" in output
    assert "Exit code: 1" in output


# --- upload ---------------------------------------------------------------


def _upload_source(db, identity, *, filename: str, body: bytes) -> Source:
    source = Source(
        id=new_id(),
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        filename=filename,
        media_type="text/csv",
        object_key="",
        byte_size=len(body),
        status="ready",
    )
    path = object_path(identity.workspace_id, source.id, filename)
    path.write_bytes(body)
    source.object_key = str(path)
    db.add(source)
    db.commit()
    return source


def test_upload_materialises_a_source_into_the_sandbox(tools, db, context, identity, provider):
    _upload_source(db, identity, filename="sales.csv", body=b"a,b\n1,2\n")
    output = _run(tools, db, context, "sandbox_upload", paths=["sales.csv"])
    assert f"{SANDBOX_HOME}/sales.csv" in output
    writes = [call for call in provider.calls if call[0] == "write_files"]
    assert writes and writes[0][1]["paths"] == [f"{SANDBOX_HOME}/sales.csv"]


@pytest.mark.parametrize(
    "path", ["../../etc/passwd", "/etc/passwd", "a/../../b.csv", "data\\win.csv"]
)
def test_upload_refuses_a_traversal_path(tools, db, context, provider, path):
    """Path safety is `store.normalize_path`'s job and this tool must not have a
    second opinion about it — the test is that the refusal happens before any
    file is read or written."""
    output = _run(tools, db, context, "sandbox_upload", paths=[path])
    assert output.startswith("Error:")
    assert not [call for call in provider.calls if call[0] == "write_files"]


def test_upload_copies_a_project_filesystem(tools, db, context, identity, provider):
    project = store.create_project(
        db, workspace_id=identity.workspace_id, name="Sandbox demo", created_by=identity.user_id
    )
    try:
        output = _run(tools, db, context, "sandbox_upload", project="Sandbox demo")
        assert f"{SANDBOX_HOME}/index.tsx" in output
        writes = [call for call in provider.calls if call[0] == "write_files"]
        assert f"{SANDBOX_HOME}/App.tsx" in writes[0][1]["paths"]
    finally:
        store.delete_project(db, workspace_id=identity.workspace_id, project_id=project.id)


def test_upload_without_a_target_is_refused(tools, db, context):
    assert _run(tools, db, context, "sandbox_upload").startswith("Error:")


# --- download -------------------------------------------------------------


def _plant(db, provider, workspace_id: str, path: str, body: bytes) -> None:
    """Put a file inside the fake sandbox, as generated code would have."""
    row = (
        db.query(SandboxSession)
        .filter(SandboxSession.workspace_id == workspace_id)
        .order_by(SandboxSession.created_at.desc())
        .first()
    )
    provider.box(handle_for(row)).files[path] = body


def test_download_saves_a_file_to_the_workspace(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/report.csv", b"a,b\n1,2\n")
    output = _run(tools, db, context, "sandbox_download", path="report.csv")

    stored = db.query(Source).filter(Source.filename == "report.csv").one()
    assert stored.id in output
    assert stored.byte_size == 8
    assert stored.status == "stored"


def test_download_refuses_an_oversize_file(tools, db, context, identity, monkeypatch, provider):
    """A sandbox is untrusted and can hand back a 2 GB file. The refusal is a
    plain sentence, and nothing is written to the workspace."""
    monkeypatch.setattr(tools_module, "get_settings", lambda: _settings(max_upload_bytes=64))
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/huge.bin", b"x" * 4096)
    output = _run(tools, db, context, "sandbox_download", path="huge.bin")

    assert "limit" in output and "Traceback" not in output
    assert db.query(Source).filter(Source.filename == "huge.bin").count() == 0
    # And it never reached this process's memory: the listing was enough to say no.
    assert not [call for call in provider.calls if call[0] == "read_file"]


def test_download_refuses_an_oversize_file_a_listing_did_not_declare(
    tools, db, context, monkeypatch, provider
):
    """The pre-read check is best effort — a provider may report no size, or a
    stale one. The size that decides is the one actually in hand."""
    monkeypatch.setattr(tools_module, "get_settings", lambda: _settings(max_upload_bytes=64))
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    monkeypatch.setattr(provider, "list_files", lambda handle, path, depth=1: ())
    monkeypatch.setattr(provider, "read_file", lambda handle, path: b"x" * 4096)

    output = _run(tools, db, context, "sandbox_download", path="sneaky.bin")
    assert "4,096 bytes" in output and "not saved" in output
    assert db.query(Source).filter(Source.filename == "sneaky.bin").count() == 0


def test_download_refuses_a_traversal_path(tools, db, context):
    assert _run(tools, db, context, "sandbox_download", path="../../etc/shadow").startswith(
        "Error:"
    )


def test_a_missing_file_is_reported_not_raised(tools, db, context):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    output = _run(tools, db, context, "sandbox_download", path="never-written.txt")
    assert "could not return that file" in output


# --- reading, writing and editing files -----------------------------------


def test_write_then_read_round_trips_a_file(tools, db, context, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    wrote = _run(tools, db, context, "sandbox_write", path="app.py", content="print(1)\n")
    assert "Wrote" in wrote
    back = _run(tools, db, context, "sandbox_read", path="app.py")
    assert "print(1)" in back


def test_write_preview_shows_a_diff_the_card_can_colour(tools, db, context, provider):
    """The write is approval-gated, and ProposalDiff colours everything from the
    first @@ hunk header down — so a create must render one."""
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    rendered = tools["sandbox_write"].preview(
        db, context, {"path": "notes.md", "content": "line one\nline two\n"}
    )
    assert "@@" in rendered and "+line one" in rendered
    assert "Create" in rendered


def test_overwrite_preview_diffs_against_the_current_file(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/cfg.txt", b"debug = false\n")
    rendered = tools["sandbox_write"].preview(
        db, context, {"path": "cfg.txt", "content": "debug = true\n"}
    )
    assert "-debug = false" in rendered and "+debug = true" in rendered
    assert "Overwrite" in rendered


def test_edit_replaces_exact_text(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/app.py", b"x = 1\ny = 2\n")
    out = _run(tools, db, context, "sandbox_edit", path="app.py", find="x = 1", replace="x = 42")
    assert "1 replacement" in out
    assert "x = 42" in _run(tools, db, context, "sandbox_read", path="app.py")


def test_edit_refuses_an_ambiguous_match(tools, db, context, identity, provider):
    """The Edit contract: a non-unique `find` is refused rather than guessing
    which occurrence the model meant."""
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/dup.txt", b"a\na\n")
    out = _run(tools, db, context, "sandbox_edit", path="dup.txt", find="a", replace="b")
    assert "matches 2 places" in out
    # Nothing was written — the file is untouched.
    assert _run(tools, db, context, "sandbox_read", path="dup.txt").count("a") == 2


def test_edit_all_replaces_every_occurrence(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/dup.txt", b"a\na\n")
    out = _run(tools, db, context, "sandbox_edit", path="dup.txt", find="a", replace="b", all=True)
    assert "2 replacement" in out


def test_edit_reports_missing_find_text(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/app.py", b"x = 1\n")
    out = _run(tools, db, context, "sandbox_edit", path="app.py", find="nope", replace="y")
    assert out.startswith("Error:") and "not in" in out


def test_read_refuses_binary_and_points_at_download(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/img.png", b"\x89PNG\x00\x00")
    out = _run(tools, db, context, "sandbox_read", path="img.png")
    assert "binary" in out and "sandbox_download" in out


def test_write_refuses_a_traversal_path(tools, db, context):
    out = _run(tools, db, context, "sandbox_write", path="../../etc/cron", content="evil")
    assert out.startswith("Error:")


def test_list_shows_what_the_sandbox_holds(tools, db, context, identity, provider):
    _run(tools, db, context, "run_python", code='print("make the sandbox")')
    _plant(db, provider, identity.workspace_id, f"{SANDBOX_HOME}/a.txt", b"hi")
    out = _run(tools, db, context, "sandbox_list")
    assert "a.txt" in out


def test_file_tools_do_not_spend_the_execution_cap(monkeypatch, db, context, provider):
    """Reads and writes are not code executions — they must not count against the
    per-turn runaway guard, or a few edits would lock the agent out of running."""
    settings = _settings(sandbox_max_execs_per_run=1)
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    tools = registry_tools(db, context)
    _run(tools, db, context, "sandbox_write", path="a.py", content="print(1)\n")
    _run(tools, db, context, "sandbox_write", path="b.py", content="print(2)\n")
    # The one code execution is still available afterwards.
    assert "limit" not in _run(tools, db, context, "run_python", code='print("still ok")')


# --- quotas ---------------------------------------------------------------


def test_the_per_run_execution_cap_fires(monkeypatch, db, context, provider):
    """The runaway-loop guard: a model that misreads an error will retry the
    same cell forever, and every retry is billed microVM time."""
    settings = _settings(sandbox_max_execs_per_run=2)
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    tools = registry_tools(db, context)

    for _ in range(2):
        assert "limit" not in _run(tools, db, context, "run_python", code='print("ok")')
    refused = _run(tools, db, context, "run_python", code='print("ok")')
    assert "Sandbox execution limit reached" in refused
    assert "2 executions per turn" in refused
    # Refused before the provider was asked to do anything.
    assert len([call for call in provider.calls if call[0] == "run_code"]) == 2


def test_a_quota_refusal_is_a_sentence_not_a_retry_suggestion(monkeypatch, db, context, provider):
    settings = _settings(sandbox_max_concurrent_per_workspace=1)
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    tools = registry_tools(db, context)
    # One session for a project, then a second unrelated one is over the limit.
    ensure_session(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        settings=settings,
        project_id="some-project",
    )
    output = _run(tools, db, context, "run_python", code='print("hi")')
    assert "limit 1" in output
    assert "Traceback" not in output and "retry" not in output.lower()


def test_a_named_session_from_another_workspace_is_refused(monkeypatch, db, context, provider):
    """The tenancy chokepoint, seen from the tool the model actually calls: a
    session id in a context window must not be a way into someone else's VM."""
    settings = _settings()
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    tools = registry_tools(db, context)
    outsider = create_identity(workspace_name="Someone else")
    theirs = ensure_session(
        db,
        workspace_id=outsider.workspace_id,
        user_id=outsider.user_id,
        settings=settings,
    )
    output = _run(tools, db, context, "run_python", code="open('/etc/passwd')", session=theirs.id)
    assert output.startswith("Error:")
    assert not [call for call in provider.calls if call[0] == "run_code"]


# --- the approval path ----------------------------------------------------


def test_every_write_tool_is_approval_gated(tools):
    """read_only=False is what lands a tool on the "ask" policy, which is the
    entire approval UX. Getting this wrong runs generated code unattended."""
    for name in (
        "run_python",
        "run_command",
        "sandbox_upload",
        "sandbox_download",
        "sandbox_write",
        "sandbox_edit",
    ):
        assert tools[name].read_only is False
        assert tools[name].preview is not None
    for name in ("list_sandboxes", "sandbox_read", "sandbox_list"):
        assert tools[name].read_only is True


@pytest.mark.parametrize(
    "name",
    [
        "run_python",
        "run_command",
        "sandbox_upload",
        "sandbox_download",
        "sandbox_write",
        "sandbox_edit",
    ],
)
def test_previews_survive_missing_arguments(tools, db, context, name):
    """A preview is a courtesy that must never be fatal (`_describe_proposal` in
    agent_loop.py swallows the failure, and the user is left approving a blank
    card). Missing args are the shape models actually get wrong."""
    for args in ({}, {"code": None}, {"paths": "not-a-list"}, {"session": "no-such-session"}):
        rendered = tools[name].preview(db, context, args)
        assert isinstance(rendered, str) and rendered


@pytest.mark.parametrize("policy", ["open", "allowlist", "none"])
def test_the_preview_shows_the_code_and_what_it_can_reach(monkeypatch, db, context, policy):
    """Both, because they are one fact: `requests.post(url, data=df)` is a bug on
    `none` and an exfiltration on `open`, and an approver shown only the code
    cannot tell which one they are approving."""
    monkeypatch.setattr(
        tools_module, "get_settings", lambda: _settings(sandbox_network_policy=policy)
    )
    tools = registry_tools(db, context)
    rendered = tools["run_python"].preview(db, context, {"code": "requests.post(url, data=df)"})
    assert "requests.post(url, data=df)" in rendered
    assert f"network: {policy}" in rendered


def test_the_preview_names_the_secrets_the_run_can_read(monkeypatch, db, context):
    """A run's credentials are half the risk the egress line describes: code that
    can reach the internet *and* read STRIPE_API_KEY is the exfiltration the
    approver is weighing. The name shows on the card; the value never does."""
    monkeypatch.setattr(tools_module, "get_settings", lambda: _settings())
    db.add(
        SandboxSecret(
            id=new_id(),
            workspace_id=context.workspace_id,
            name="STRIPE_API_KEY",
            value_enc="ciphertext-never-shown",
            created_by=context.user_id,
        )
    )
    db.commit()
    tools = registry_tools(db, context)
    rendered = tools["run_python"].preview(db, context, {"code": "1"})
    assert "secrets: it can read STRIPE_API_KEY" in rendered
    assert "ciphertext-never-shown" not in rendered
    db.query(SandboxSecret).filter(
        SandboxSecret.workspace_id == context.workspace_id
    ).delete()
    db.commit()


def test_the_preview_reports_a_named_sessions_own_policy(monkeypatch, db, context, provider):
    """A session records the policy it was created under. Relaxing the workspace
    default must not make an approval card describe a live sandbox as more
    restricted than it is — or less."""
    settings = _settings(sandbox_network_policy="allowlist", sandbox_host_allowlist="pypi.org")
    monkeypatch.setattr(tools_module, "get_settings", lambda: settings)
    tools = registry_tools(db, context)
    locked = ensure_session(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        settings=settings,
    )
    monkeypatch.setattr(tools_module, "get_settings", lambda: _settings())
    rendered = tools["run_python"].preview(db, context, {"code": "1", "session": locked.id})
    assert "allowlist" in rendered and "pypi.org" in rendered


# --- listing and the off switch -------------------------------------------


def test_list_sandboxes_reports_the_workspaces_sessions(tools, db, context):
    assert "No sandboxes yet" in _run(tools, db, context, "list_sandboxes")
    _run(tools, db, context, "run_python", code='print("hi")')
    listed = json.loads(_run(tools, db, context, "list_sandboxes"))["sandboxes"]
    assert len(listed) == 1
    assert listed[0]["status"] == "running"
    assert listed[0]["executions"] == 1


def test_no_tools_at_all_when_the_sandbox_is_off(monkeypatch, db, context):
    """Not "tools that fail on first use": a deployment without an execution
    provider should have no run tools, so the model never spends a turn
    discovering that they do not work."""
    monkeypatch.setattr(tools_module, "get_settings", lambda: _settings(sandbox_enabled=False))
    assert registry_tools(db, context) == {}
