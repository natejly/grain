"""The DAG executor: resume, approval parks, references, and failure isolation.

ADR 0007 argued that a small executor on the existing run/event/approval
machinery beats a second orchestration runtime, and it named the properties that
argument depends on. These tests are those properties, one for one:

- a resume skips committed nodes, proved by killing a run mid-DAG;
- an interrupted *write* is not retried, because "at least once" is the wrong
  number for a tool that sends email;
- an unattended write parks and waits rather than proceeding;
- a resumed run comes back through the existing decision endpoint;
- one node failing leaves a run record you can read;
- and nobody has to notice the crash: the recovery sweep resumes the run under a
  lease, refuses to touch one that is merely parked, and gives up rather than
  retrying forever.

The tools are fakes, but nothing else is: the real `execute_agent_tool_call`,
the real `resolve_policy`, the real `AgentToolCall` rows and the real decision
endpoint. What is faked is the registry, so a test can own a tool that counts its
calls or dies on command.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional

import pytest
from conftest import Identity, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import (
    AgentToolCall,
    Run,
    ToolPolicy,
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
)
from app.services import agent_loop
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec
from app.services.workflows import executor

TEST_BASE_URL = "https://testserver"


# --------------------------------------------------------------------------
# A registry a test can own
# --------------------------------------------------------------------------


@dataclass
class Probe:
    """One fake tool, plus the record of everything it was asked to do."""

    name: str
    read_only: bool = True
    reply: str = "ok"
    parameters: Dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        }
    )
    calls: List[Dict[str, Any]] = field(default_factory=list)
    #: Raised on the Nth call (1-based). A BaseException stands in for the
    #: process dying: `execute_agent_tool_call` catches Exception, so anything
    #: below it would be recorded as an ordinary tool failure instead.
    raises: Optional[Callable[[], BaseException]] = None
    raise_on: int = 1

    def spec(self) -> ToolSpec:
        def run(db: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
            self.calls.append(dict(args))
            if self.raises is not None and len(self.calls) == self.raise_on:
                raise self.raises()
            return ToolResult(content=self.reply)

        return ToolSpec(
            name=self.name,
            description=f"The {self.name} probe.",
            parameters=self.parameters,
            executor=run,
            read_only=self.read_only,
        )


class Kill(BaseException):
    """Stands in for SIGKILL: escapes every `except Exception` in the stack."""


def install(monkeypatch: pytest.MonkeyPatch, *probes: Probe) -> Dict[str, ToolSpec]:
    """Make `probes` the whole registry everywhere one is built.

    All three call sites, because they must agree: `agent_loop` builds its own
    for an `agent` node, and `api/workflows` builds one to compile and to answer
    "will this park". A test that patched only one would have a workspace whose
    tools depend on which door you came in through, which is not a state the
    product has.
    """
    from app.api import workflows as workflows_api

    registry = {probe.name: probe.spec() for probe in probes}
    for module in (executor, agent_loop, workflows_api):
        monkeypatch.setattr(
            module, "build_registry", lambda db, context: dict(registry)
        )
    return registry


# --------------------------------------------------------------------------
# Building a workflow and a run
# --------------------------------------------------------------------------


def graph(
    nodes: List[Dict[str, Any]],
    edges: Optional[List[Dict[str, str]]] = None,
    *,
    name: str = "Probe workflow",
    trigger: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "name": name,
        "description": "A workflow built by a test.",
        "nodes": nodes,
        "edges": edges or [],
    }
    if trigger is not None:
        document["trigger"] = trigger
    return document


def tool_node(
    node_id: str, tool: str, arguments: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "id": node_id,
        "kind": "tool",
        "tool": tool,
        "arguments": arguments or {},
        "description": f"Call {tool}.",
    }


def store(
    db: Any, identity: Identity, document: Mapping[str, Any], *, status: str = "draft"
) -> Workflow:
    workflow = Workflow(
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        name=str(document.get("name") or "Probe workflow"),
        graph_json=json.dumps(document),
        status=status,
        trigger_kind=str((document.get("trigger") or {}).get("kind") or "manual"),
        schedule_cron=str((document.get("trigger") or {}).get("cron") or ""),
        schedule_timezone=str((document.get("trigger") or {}).get("timezone") or "UTC"),
    )
    db.add(workflow)
    db.commit()
    return workflow


def begin(
    db: Any,
    identity: Identity,
    document: Mapping[str, Any],
    *,
    trigger: str = "manual",
    payload: Optional[Dict[str, Any]] = None,
) -> WorkflowRun:
    workflow = store(db, identity, document)
    workflow_run = executor.start_run(
        db, workflow, user_id=identity.user_id, trigger=trigger, payload=payload
    )
    db.commit()
    return workflow_run


def nodes_of(db: Any, workflow_run: WorkflowRun) -> Dict[str, WorkflowNodeRun]:
    from sqlalchemy import select

    rows = db.scalars(
        select(WorkflowNodeRun).where(
            WorkflowNodeRun.workflow_run_id == workflow_run.id
        )
    )
    return {row.node_key: row for row in rows}


def output_of(row: WorkflowNodeRun) -> Any:
    return json.loads(row.output_json) if row.output_json else ""


@pytest.fixture
def identity() -> Identity:
    """A fresh workspace per test, so one test's policy row is not another's."""
    return create_identity(name="Workflow owner", workspace_name="Workflow workspace")


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------
# The happy path, and what flows along the edges
# --------------------------------------------------------------------------


def test_a_read_only_graph_runs_to_completion_unattended(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ADR's promise: a scheduled workflow that only reads finishes at 3am."""
    first = Probe("probe_read", reply="twenty-two open PRs")
    second = Probe("probe_second")
    install(monkeypatch, first, second)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read", {"text": "open PRs"}),
                tool_node("report", "probe_second", {"text": "Found: {{ gather.output }}"}),
            ],
            [{"from": "gather", "to": "report"}],
        ),
        trigger="schedule",
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "succeeded"
    assert workflow_run.error == ""
    rows = nodes_of(db, workflow_run)
    assert [rows[key].status for key in ("gather", "report")] == [
        "succeeded",
        "succeeded",
    ]
    # The reference resolved from the upstream node's actual output, not from
    # the template that produced it.
    assert second.calls == [{"text": "Found: twenty-two open PRs"}]
    assert output_of(rows["gather"]) == "twenty-two open PRs"
    # Read-only tools carry no standing grant and still ran: the default is what
    # authorised them, and the trail says so.
    assert rows["gather"].policy == "allow"


def test_a_whole_reference_keeps_the_upstream_type(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{{ n.output }}` alone is the value; embedded in text it is a string.

    The distinction is what `validate.py` defers at compile time, so this is
    where it has to be honoured.
    """
    source = Probe("probe_json", reply=json.dumps({"count": 7, "tags": ["a", "b"]}))
    sink = Probe(
        "probe_sink",
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}, "label": {"type": "string"}},
        },
    )
    install(monkeypatch, source, sink)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("src", "probe_json"),
                tool_node(
                    "dst",
                    "probe_sink",
                    {
                        "count": "{{ src.output.count }}",
                        "label": "tag {{ src.output.tags.0 }}",
                    },
                ),
            ],
            [{"from": "src", "to": "dst"}],
        ),
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "succeeded", workflow_run.error
    assert sink.calls == [{"count": 7, "label": "tag a"}]


def test_the_trigger_payload_is_readable_as_input(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Probe("probe_read")
    install(monkeypatch, probe)
    workflow_run = begin(
        db,
        identity,
        graph([tool_node("only", "probe_read", {"text": "{{ input.repo }}"})]),
        payload={"repo": "acme/widgets"},
    )
    executor.advance_run(db, workflow_run)
    assert probe.calls == [{"text": "acme/widgets"}]


def test_a_resolved_argument_of_the_wrong_type_is_rejected_not_coerced(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0007's last unbuilt piece, stated as "never coerce".

    A compile-time deferral means nobody has typed this value yet. Turning a
    sentence into the integer field it does not fit would make the tool do
    something subtly wrong; refusing makes it do nothing and say why.
    """
    source = Probe("probe_text", reply="not a number")
    sink = Probe(
        "probe_int",
        parameters={"type": "object", "properties": {"count": {"type": "integer"}}},
    )
    install(monkeypatch, source, sink)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("src", "probe_text"),
                tool_node("dst", "probe_int", {"count": "{{ src.output }}"}),
            ],
            [{"from": "src", "to": "dst"}],
        ),
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "arguments_invalid" in workflow_run.error
    assert sink.calls == []


def test_a_reference_to_a_node_that_produced_nothing_fails_loudly(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Probe("probe_json", reply="plain text, not json")
    sink = Probe("probe_sink")
    install(monkeypatch, source, sink)
    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("src", "probe_json"),
                tool_node("dst", "probe_sink", {"text": "{{ src.output.missing }}"}),
            ],
            [{"from": "src", "to": "dst"}],
        ),
    )
    executor.advance_run(db, workflow_run)
    assert workflow_run.status == "failed"
    assert "reference_unresolved" in workflow_run.error
    assert sink.calls == []


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------


def test_a_crash_mid_dag_resumes_without_rerunning_completed_work(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the process between nodes; restart; the finished node stays finished.

    `Kill` is a BaseException, so it travels straight through the executor's own
    `except Exception` — the same way a real SIGKILL travels through everything.
    The assertion that matters is `first.calls`: one call, across two executions
    of the same run. That is the UNIQUE(workflow_run_id, node_key) constraint
    doing the job ADR 0007 chose it for.
    """
    first = Probe("probe_read", reply="gathered")
    second = Probe("probe_second", raises=Kill, raise_on=1)
    install(monkeypatch, first, second)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                tool_node("crash", "probe_second", {"text": "{{ gather.output }}"}),
            ],
            [{"from": "gather", "to": "crash"}],
        ),
    )
    with pytest.raises(Kill):
        executor.advance_run(db, workflow_run)

    rows = nodes_of(db, workflow_run)
    assert rows["gather"].status == "succeeded"
    assert rows["crash"].status == "running"
    assert len(first.calls) == 1

    # A new process picks the run back up. `probe_second` no longer dies.
    db.expire_all()
    resumed = db.get(WorkflowRun, workflow_run.id)
    executor.advance_run(db, resumed)

    assert resumed.status == "succeeded", resumed.error
    assert len(first.calls) == 1, "the completed node ran again on resume"
    assert len(second.calls) == 2, "the interrupted read-only node was retried"
    assert nodes_of(db, resumed)["crash"].attempt == 1


def test_an_interrupted_write_is_not_retried(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Probably already sent" is not a reason to send it again.

    The unique constraint proves a *committed* node ran once. It says nothing
    about one that was executing when the process died, and ADR 0007 is explicit
    that at-least-once is the wrong number for a node that sends email. So the
    run stops and a human decides.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope="workflow")

    workflow_run = begin(db, identity, graph([tool_node("send", "probe_write")]))
    db.add(
        WorkflowNodeRun(
            workspace_id=identity.workspace_id,
            workflow_run_id=workflow_run.id,
            node_key="send",
            kind="tool",
            tool_name="probe_write",
            status="running",
        )
    )
    db.commit()

    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "node_interrupted" in workflow_run.error
    assert writer.calls == [], "a write of unknown effect was repeated"


# --------------------------------------------------------------------------
# The approval park
# --------------------------------------------------------------------------


def grant(
    db: Any, identity: Identity, tool_name: str, policy: str, *, scope: str
) -> ToolPolicy:
    row = ToolPolicy(
        workspace_id=identity.workspace_id,
        tool_name=tool_name,
        policy=policy,
        scope=scope,
        created_by=identity.user_id,
    )
    db.add(row)
    db.commit()
    return row


def test_an_unattended_write_parks_the_run_and_waits(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody is present, so the workflow stops at the write. The containment."""
    reader = Probe("probe_read", reply="the digest")
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, reader, writer)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                tool_node("send", "probe_write", {"text": "{{ gather.output }}"}),
            ],
            [{"from": "gather", "to": "send"}],
        ),
        trigger="schedule",
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "waiting_for_approval"
    assert writer.calls == [], "an unattended run performed a write"

    rows = nodes_of(db, workflow_run)
    assert rows["gather"].status == "succeeded"
    assert rows["send"].status == "waiting_for_approval"
    assert rows["send"].policy == "ask"

    # The park is the same row a chat turn writes, on a Run in the same state,
    # so the approval inbox and the decision endpoint need no workflow branch.
    call = db.get(AgentToolCall, rows["send"].agent_tool_call_id)
    assert call is not None and call.status == "proposed"
    backing = db.get(Run, workflow_run.run_id)
    assert backing is not None and backing.status == "waiting_for_approval"
    # Empty agent state is the discriminator: no model turn was in flight.
    assert not backing.agent_state_json


def test_the_existing_decision_endpoint_resumes_a_parked_workflow(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One park, one resume, one row — over HTTP, through the endpoint chat uses."""
    writer = Probe("probe_write", read_only=False, reply="sent")
    after = Probe("probe_after")
    install(monkeypatch, writer, after)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("send", "probe_write", {"text": "hello"}),
                tool_node("log", "probe_after", {"text": "{{ send.output }}"}),
            ],
            [{"from": "send", "to": "log"}],
        ),
    )
    executor.advance_run(db, workflow_run)
    call_id = nodes_of(db, workflow_run)["send"].agent_tool_call_id
    assert call_id

    with TestClient(app, base_url=TEST_BASE_URL) as client:
        authorize(client, identity)
        response = client.post(
            f"/api/agent-tool-calls/{call_id}/decision",
            json={"decision": "approved", "remember": False},
            headers={"Idempotency-Key": "wf-approve-1"},
        )
    assert response.status_code == 200, response.text

    db.expire_all()
    resumed = db.get(WorkflowRun, workflow_run.id)
    assert resumed.status == "succeeded", resumed.error
    assert writer.calls == [{"text": "hello"}]
    assert after.calls == [{"text": "sent"}]


def test_a_denied_approval_ends_the_run_and_skips_the_rest(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Probe("probe_write", read_only=False)
    after = Probe("probe_after")
    install(monkeypatch, writer, after)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("send", "probe_write", {"text": "hello"}),
                tool_node("log", "probe_after", {"text": "{{ send.output }}"}),
            ],
            [{"from": "send", "to": "log"}],
        ),
    )
    executor.advance_run(db, workflow_run)
    call_id = nodes_of(db, workflow_run)["send"].agent_tool_call_id

    with TestClient(app, base_url=TEST_BASE_URL) as client:
        authorize(client, identity)
        response = client.post(
            f"/api/agent-tool-calls/{call_id}/decision",
            json={"decision": "denied", "remember": False},
            headers={"Idempotency-Key": "wf-deny-1"},
        )
    assert response.status_code == 200, response.text

    db.expire_all()
    resumed = db.get(WorkflowRun, workflow_run.id)
    # `cancelled`, not `failed`: a person said no, and the gate working is not
    # the automation breaking.
    assert resumed.status == "cancelled"
    assert "denied" in resumed.error
    assert writer.calls == []
    rows = nodes_of(db, resumed)
    assert rows["send"].status == "failed"
    assert rows["log"].status == "skipped"


def authorize(client: TestClient, identity: Identity) -> None:
    from app.config import get_settings

    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token


# --------------------------------------------------------------------------
# Failure isolation and terminal states
# --------------------------------------------------------------------------


def test_one_node_failing_leaves_a_legible_terminal_state(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = Probe("probe_read", reply="fine")
    bad = Probe("probe_bad", raises=lambda: RuntimeError("the API said no"))
    after = Probe("probe_after")
    install(monkeypatch, ok, bad, after)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                tool_node("boom", "probe_bad"),
                tool_node("log", "probe_after"),
            ],
            [{"from": "gather", "to": "boom"}, {"from": "boom", "to": "log"}],
        ),
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "the API said no" in workflow_run.error
    assert workflow_run.finished_at is not None
    rows = nodes_of(db, workflow_run)
    assert rows["gather"].status == "succeeded"
    assert rows["boom"].status == "failed"
    assert "the API said no" in rows["boom"].error
    # The node that never ran says so rather than simply being absent.
    assert rows["log"].status == "skipped"
    assert after.calls == []


def test_a_graph_that_no_longer_validates_fails_before_the_first_node(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0007: the registry changes underneath a stored graph, loudly.

    Half an automation is worse than none of it, so the check runs at run start
    and nothing executes.
    """
    present = Probe("probe_read")
    install(monkeypatch, present)
    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                tool_node("gone", "probe_from_a_disconnected_server"),
            ],
            [{"from": "gather", "to": "gone"}],
        ),
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "graph_stale" in workflow_run.error
    assert "tool_unknown" in workflow_run.error
    assert present.calls == []


def test_a_cancelled_run_stops_between_nodes(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Probe("probe_read")
    second = Probe("probe_second")
    install(monkeypatch, first, second)
    workflow_run = begin(
        db,
        identity,
        graph(
            [tool_node("a", "probe_read"), tool_node("b", "probe_second")],
            [{"from": "a", "to": "b"}],
        ),
    )
    workflow_run.cancel_requested = True
    db.commit()

    executor.advance_run(db, workflow_run)
    assert workflow_run.status == "cancelled"
    assert first.calls == []


def test_advancing_a_finished_run_changes_nothing(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotence at the outermost level: a duplicate background task is a no-op."""
    probe = Probe("probe_read")
    install(monkeypatch, probe)
    workflow_run = begin(db, identity, graph([tool_node("only", "probe_read")]))
    executor.advance_run(db, workflow_run)
    assert workflow_run.status == "succeeded"

    executor.advance_run(db, workflow_run)
    executor.process_workflow_run(workflow_run.id)
    assert len(probe.calls) == 1


# --------------------------------------------------------------------------
# Agent nodes
# --------------------------------------------------------------------------


def test_an_agent_node_runs_on_the_backing_run_and_feeds_the_next_node(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scripted model has no entry for this prompt, so it answers from
    evidence — which is enough to prove the answer becomes the node's output."""
    reader = Probe("probe_read", reply="three pull requests")
    sink = Probe("probe_sink")
    install(monkeypatch, reader, sink)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                {
                    "id": "think",
                    "kind": "agent",
                    "prompt": "Summarise: {{ gather.output }}",
                    "description": "Write the summary.",
                },
                tool_node("sink", "probe_sink", {"text": "{{ think.output }}"}),
            ],
            [
                {"from": "gather", "to": "think"},
                {"from": "think", "to": "sink"},
            ],
        ),
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "succeeded", workflow_run.error
    rows = nodes_of(db, workflow_run)
    assert rows["think"].status == "succeeded"
    assert json.loads(rows["think"].arguments_json)["prompt"] == (
        "Summarise: three pull requests"
    )
    assert len(sink.calls) == 1
    assert sink.calls[0]["text"] == output_of(rows["think"])


def test_a_recovery_sweep_cannot_start_a_chat_turn_on_a_workflow_run(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`recover_durable_work` re-queues runs left `running` by an unclean stop.

    Without a guard it would hand a workflow's backing run to `process_run` and
    staple a chat answer onto an automation. Failing loudly is the right outcome:
    the workflow run is the record that matters and it can be re-run.
    """
    probe = Probe("probe_read", read_only=False)
    install(monkeypatch, probe)
    workflow_run = begin(db, identity, graph([tool_node("send", "probe_read")]))
    executor.advance_run(db, workflow_run)
    backing = db.get(Run, workflow_run.run_id)
    assert backing is not None

    with pytest.raises(RuntimeError, match="workflow"):
        agent_loop.run_agent_turn(db, backing, evidence=[])


# --------------------------------------------------------------------------
# Recovery after an unclean stop
# --------------------------------------------------------------------------


def orphan(db: Any, workflow_run: WorkflowRun) -> None:
    """Make this run look like one whose process died and never came back.

    A crash does not tidy up after itself, so the row is left exactly as the
    executor last committed it and only the *lease* is moved into the past —
    which is the whole signal a survivor has that the holder is gone. Written
    through a Core UPDATE so `updated_at`'s `onupdate` cannot quietly refresh
    the very staleness the sweep is about to test.
    """
    stale = utcnow() - timedelta(seconds=600)
    db.execute(
        update(Run)
        .where(Run.id == workflow_run.run_id)
        .values(lease_expires_at=stale)
    )
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == workflow_run.id)
        .values(updated_at=stale)
    )
    db.commit()
    db.expire_all()


def reread(db: Any, workflow_run: WorkflowRun) -> WorkflowRun:
    """The run as another process would see it, after a sweep in its own session."""
    db.expire_all()
    row = db.get(WorkflowRun, workflow_run.id)
    assert row is not None
    return row


def test_a_crashed_run_is_recovered_without_repeating_the_work_it_finished(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline: nobody presses anything, and the run finishes correctly.

    Same `Kill` as the resume test — a BaseException travels through every
    `except Exception` the way a SIGKILL travels through everything — but here
    the second execution is started by the sweep rather than by the test. The
    assertion that carries the whole design is `len(first.calls) == 1`: the node
    that committed before the crash is read out of `workflow_node_runs`, not run
    again, across two executions of one run.
    """
    first = Probe("probe_read", reply="gathered")
    second = Probe("probe_second", raises=Kill, raise_on=1)
    install(monkeypatch, first, second)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                tool_node("crash", "probe_second", {"text": "{{ gather.output }}"}),
            ],
            [{"from": "gather", "to": "crash"}],
        ),
        trigger="schedule",
    )
    with pytest.raises(Kill):
        executor.advance_run(db, workflow_run)
    orphan(db, workflow_run)

    assert executor.recover_workflow_runs() == [workflow_run.id]

    resumed = reread(db, workflow_run)
    assert resumed.status == "succeeded", resumed.error
    assert len(first.calls) == 1, "the committed node ran a second time"
    assert len(second.calls) == 2, "the interrupted read-only node was not retried"
    assert nodes_of(db, resumed)["crash"].attempt == 1


def test_recovery_still_refuses_to_repeat_an_interrupted_write(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery must not quietly turn writes into at-least-once.

    The rule belongs to `advance_run`, and recovery gets it by *using*
    `advance_run` rather than by reimplementing the walk — which is the reason
    the sweep claims a run and then calls the ordinary entry point.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope="workflow")

    workflow_run = begin(db, identity, graph([tool_node("send", "probe_write")]))
    with pytest.raises(Kill):
        writer.raises = Kill
        executor.advance_run(db, workflow_run)
    assert len(writer.calls) == 1
    orphan(db, workflow_run)

    assert executor.recover_workflow_runs() == [workflow_run.id]

    resumed = reread(db, workflow_run)
    assert resumed.status == "failed"
    assert "node_interrupted" in resumed.error
    assert len(writer.calls) == 1, "recovery re-sent a write of unknown effect"


def test_a_run_parked_on_an_approval_is_not_recovered(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parked is not crashed. It is waiting on a person, and it must keep waiting.

    A park deliberately clears the backing run's lease — nothing is executing —
    so a sweep that looked only at leases would claim every approval in the
    inbox and run the write the approver has not agreed to yet. The status is
    what distinguishes them, and this is the test that says so.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)

    workflow_run = begin(
        db, identity, graph([tool_node("send", "probe_write")]), trigger="schedule"
    )
    executor.advance_run(db, workflow_run)
    assert workflow_run.status == "waiting_for_approval"
    backing = db.get(Run, workflow_run.run_id)
    assert backing is not None and backing.lease_expires_at is None

    assert executor.claim_orphaned_runs(db) == []
    assert executor.recover_workflow_runs() == []

    still_parked = reread(db, workflow_run)
    assert still_parked.status == "waiting_for_approval"
    assert writer.calls == [], "recovery ran a write nobody had approved"


def test_only_one_sweep_can_claim_the_same_run(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two API processes booting together must not both resume one run.

    The claim is a conditional UPDATE on the lease `runs.run_lease_seconds`
    already describes, so the loser finds out by getting `rowcount == 0` rather
    than by colliding on the unique constraint half a tool call later. Calling
    the claim twice in a row is the same race with the interleaving made
    deterministic.
    """
    probe = Probe("probe_read", raises=Kill, raise_on=1)
    install(monkeypatch, probe)

    workflow_run = begin(db, identity, graph([tool_node("only", "probe_read")]))
    with pytest.raises(Kill):
        executor.advance_run(db, workflow_run)
    orphan(db, workflow_run)

    assert executor.claim_orphaned_runs(db) == [workflow_run.id]
    assert executor.claim_orphaned_runs(db) == [], "a second process claimed it too"


def test_a_fresh_queued_run_is_not_stolen_from_the_task_about_to_start_it(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /run` returns 202 and starts the graph on a background task.

    Between the commit and the task there is a window with no backing run and so
    no lease to hold, and a sweep that ignored it would race the request that
    just created the run. One lease of quiet is the gate; after it, the run is
    genuinely abandoned and the sweep takes it.
    """
    probe = Probe("probe_read")
    install(monkeypatch, probe)

    workflow_run = begin(db, identity, graph([tool_node("only", "probe_read")]))
    assert workflow_run.run_id is None
    assert executor.claim_orphaned_runs(db) == []

    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == workflow_run.id)
        .values(updated_at=utcnow() - timedelta(seconds=600))
    )
    db.commit()
    db.expire_all()

    assert executor.recover_workflow_runs() == [workflow_run.id]
    assert reread(db, workflow_run).status == "succeeded"
    assert len(probe.calls) == 1


def test_a_node_that_keeps_killing_its_process_stops_being_retried(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, and the bound is `MAX_NODE_ATTEMPTS` against an existing counter.

    A read-only node is safe to retry, which is not the same as free: one that
    OOMs its process on every attempt would be resumed by every sweep for the
    life of the deployment. `WorkflowNodeRun.attempt` already counts the
    interruptions, so the bound is a comparison rather than new state.
    """
    probe = Probe("probe_read")
    install(monkeypatch, probe)

    workflow_run = begin(db, identity, graph([tool_node("only", "probe_read")]))
    db.add(
        WorkflowNodeRun(
            workspace_id=identity.workspace_id,
            workflow_run_id=workflow_run.id,
            node_key="only",
            kind="tool",
            tool_name="probe_read",
            status="running",
            attempt=executor.MAX_NODE_ATTEMPTS,
        )
    )
    db.commit()

    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "node_retry_exhausted" in workflow_run.error
    assert probe.calls == [], "an exhausted node was retried anyway"


def test_a_run_too_old_to_resume_is_failed_rather_than_recovered(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound of last resort, and the one the product wants anyway.

    It catches the loop the per-node bound cannot see — a process dying in
    `_prepare`, before any node row exists — and it is also the honest answer for
    a scheduled automation: finishing yesterday's 9am job this afternoon is not a
    rescue, for the same reason `schedule.CATCHUP` refuses to replay a day.
    """
    probe = Probe("probe_read", raises=Kill, raise_on=1)
    install(monkeypatch, probe)

    workflow_run = begin(
        db, identity, graph([tool_node("only", "probe_read")]), trigger="schedule"
    )
    with pytest.raises(Kill):
        executor.advance_run(db, workflow_run)
    orphan(db, workflow_run)
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == workflow_run.id)
        .values(started_at=utcnow() - executor.RECOVERY_MAX_AGE - timedelta(hours=1))
    )
    db.commit()
    db.expire_all()

    assert executor.claim_orphaned_runs(db) == []

    abandoned = reread(db, workflow_run)
    assert abandoned.status == "failed"
    assert "run_abandoned" in abandoned.error
    # And it says so on the row rather than just falling out of the query, so
    # the next sweep does not have to rediscover that it gave up.
    assert nodes_of(db, abandoned)["only"].status == "failed"
    assert len(probe.calls) == 1


def test_the_chat_sweep_leaves_a_workflows_backing_run_alone(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_agent_turn`'s guard prevents corruption; the exclusion prevents damage.

    Handing a workflow's backing run to `process_run` reaches that guard, which
    raises — and `process_run` records exceptions by failing the run. The
    conversation would be protected by destroying the record of the automation.
    So the chat sweep skips these rows and the workflow sweep, which resumes from
    the first incomplete node, owns them.
    """
    from app.services.recovery import recover_durable_work

    first = Probe("probe_read", reply="gathered")
    second = Probe("probe_second", raises=Kill, raise_on=1)
    install(monkeypatch, first, second)

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                tool_node("gather", "probe_read"),
                tool_node("crash", "probe_second", {"text": "{{ gather.output }}"}),
            ],
            [{"from": "gather", "to": "crash"}],
        ),
    )
    with pytest.raises(Kill):
        executor.advance_run(db, workflow_run)
    orphan(db, workflow_run)

    recover_durable_work()

    resumed = reread(db, workflow_run)
    assert resumed.status == "succeeded", resumed.error
    backing = db.get(Run, resumed.run_id)
    assert backing is not None
    assert backing.status == "completed"
    assert "workflow" not in backing.error
    assert len(first.calls) == 1
