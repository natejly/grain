from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from app.api import doc_pending
from app.database import SessionLocal
from app.main import app
from app.models import AgentToolCall, Document, Membership, Run, Workspace
from app.services.agent_loop import run_agent_turn

# main.py is a shared file wired up separately; registering here when the app
# has not picked the router up yet keeps this suite meaningful either way.
if not any(getattr(route, "path", "") == "/api/documents-pending" for route in app.routes):
    app.include_router(doc_pending.router)


@pytest.fixture
def parked(client):
    """Park proposal rows on real runs, then delete every row we created."""
    bootstrap = client.get("/api/bootstrap").json()
    identity = bootstrap["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "pending-conv-" + os.urandom(6).hex()},
        json={"title": "Pending edits"},
    ).json()
    run_ids: list[str] = []
    workspace_ids: list[str] = []

    def make(
        name: str,
        arguments: str,
        *,
        preview: str = "",
        status: str = "proposed",
        run_status: str = "waiting_for_approval",
        other_workspace: bool = False,
    ) -> str:
        db = SessionLocal()
        try:
            workspace_id = identity["workspace_id"]
            if other_workspace:
                stranger = Workspace(name="Stranger " + os.urandom(4).hex())
                db.add(stranger)
                db.flush()
                workspace_id = stranger.id
                workspace_ids.append(stranger.id)
            run = Run(
                workspace_id=workspace_id,
                conversation_id=conversation["id"],
                agent_id=bootstrap["default_agent_id"],
                created_by=identity["user_id"],
                status=run_status,
                prompt="Revise the document",
            )
            db.add(run)
            db.flush()
            call = AgentToolCall(
                workspace_id=workspace_id,
                run_id=run.id,
                name=name,
                arguments_json=arguments,
                proposal_preview=preview,
                status=status,
            )
            db.add(call)
            db.commit()
            run_ids.append(run.id)
            return call.id
        finally:
            db.close()

    yield make

    db = SessionLocal()
    try:
        db.query(AgentToolCall).filter(AgentToolCall.run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
        db.query(Run).filter(Run.id.in_(run_ids)).delete(synchronize_session=False)
        db.query(Workspace).filter(Workspace.id.in_(workspace_ids)).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def document(client):
    created = client.post(
        "/api/documents",
        json={
            "title": "Pending Notes " + os.urandom(4).hex(),
            "content": "alpha\nbeta\n",
            "kind": "markdown",
        },
    ).json()
    yield created
    client.delete(f"/api/documents/{created['id']}")


def _pending(client) -> list[dict]:
    response = client.get("/api/documents-pending")
    assert response.status_code == 200
    return response.json()


def test_pending_edit_is_tied_to_its_document(client, parked, document):
    call_id = parked(
        "edit_document",
        json.dumps({"document_id": document["id"], "find": "alpha", "replace": "omega"}),
        preview="@@ -1 +1 @@\n-alpha\n+omega",
    )
    match = [row for row in _pending(client) if row["id"] == call_id]
    assert len(match) == 1
    row = match[0]
    assert row["document_id"] == document["id"]
    assert row["title"] == document["title"]
    assert row["name"] == "edit_document"
    assert row["proposal_preview"].startswith("@@")
    assert row["run_id"]


def test_a_title_only_edit_resolves_to_the_document(client, parked, document):
    call_id = parked(
        "edit_document",
        json.dumps(
            {"title": document["title"].upper(), "find": "beta", "replace": "gamma"}
        ),
    )
    match = [row for row in _pending(client) if row["id"] == call_id]
    assert match and match[0]["document_id"] == document["id"]


def test_a_create_has_no_document_yet_but_keeps_its_title(client, parked):
    call_id = parked("create_document", '{"title": "Drafted by the agent", "content": "hi"}')
    match = [row for row in _pending(client) if row["id"] == call_id]
    assert match and match[0]["document_id"] == ""
    assert match[0]["title"] == "Drafted by the agent"


@pytest.mark.parametrize(
    "arguments",
    [
        '{"document_id": "abc", "find": ',  # truncated mid-stream
        "[1, 2, 3]",  # valid JSON, wrong shape
        '{"document_id": 17, "title": null}',  # non-string targets
        "",
        "not json at all",
    ],
)
def test_malformed_arguments_do_not_break_the_listing(client, parked, arguments):
    call_id = parked("edit_document", arguments)
    match = [row for row in _pending(client) if row["id"] == call_id]
    assert match and match[0]["document_id"] == ""
    assert match[0]["title"] == ""


def test_an_unknown_document_id_resolves_to_nothing(client, parked):
    call_id = parked("edit_document", '{"document_id": "does-not-exist"}')
    match = [row for row in _pending(client) if row["id"] == call_id]
    assert match and match[0]["document_id"] == ""


def test_calls_for_other_tools_are_excluded(client, parked):
    call_id = parked("board_add_card", '{"column": "Todo", "title": "Ship it"}')
    assert not [row for row in _pending(client) if row["id"] == call_id]


def test_decided_calls_are_excluded(client, parked, document):
    approved = parked(
        "edit_document",
        json.dumps({"document_id": document["id"]}),
        status="approved",
    )
    denied = parked(
        "edit_document",
        json.dumps({"document_id": document["id"]}),
        status="denied",
    )
    listed = {row["id"] for row in _pending(client)}
    assert approved not in listed
    assert denied not in listed


def test_proposals_on_a_cancelled_run_are_excluded(client, parked, document):
    call_id = parked(
        "edit_document",
        json.dumps({"document_id": document["id"]}),
        run_status="cancelled",
    )
    assert not [row for row in _pending(client) if row["id"] == call_id]


def test_another_workspaces_proposals_never_appear(client, parked, document):
    call_id = parked(
        "edit_document",
        json.dumps({"document_id": document["id"]}),
        other_workspace=True,
    )
    assert not [row for row in _pending(client) if row["id"] == call_id]


def test_a_title_only_edit_resolves_inside_the_acting_workspace(client):
    """The actor belongs to two workspaces holding a document with one title.

    Title lookup is the one resolution path that does not carry an id, so it is
    the one that could cross a workspace boundary if `resolve` were ever called
    unscoped. Pin it to the acting workspace's copy.
    """
    bootstrap = client.get("/api/bootstrap").json()
    identity = bootstrap["identity"]
    title = "Shared Title " + os.urandom(4).hex()
    conversation_id = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "shared-conv-" + os.urandom(6).hex()},
        json={"title": "Shared"},
    ).json()["id"]
    db = SessionLocal()
    try:
        stranger = Workspace(name="Second " + os.urandom(4).hex())
        db.add(stranger)
        db.flush()
        db.add(
            Membership(
                workspace_id=stranger.id, user_id=identity["user_id"], role="owner"
            )
        )
        mine = Document(
            workspace_id=identity["workspace_id"],
            title=title,
            kind="markdown",
            content="mine\n",
        )
        theirs = Document(
            workspace_id=stranger.id, title=title, kind="markdown", content="theirs\n"
        )
        db.add_all([mine, theirs])
        db.flush()
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation_id,
            agent_id=bootstrap["default_agent_id"],
            created_by=identity["user_id"],
            status="waiting_for_approval",
            prompt="Revise the shared doc",
        )
        db.add(run)
        db.flush()
        call = AgentToolCall(
            workspace_id=identity["workspace_id"],
            run_id=run.id,
            name="edit_document",
            arguments_json=json.dumps({"title": title}),
            status="proposed",
        )
        db.add(call)
        db.commit()
        ids = (call.id, run.id, stranger.id, mine.id, theirs.id)
    finally:
        db.close()
    call_id, run_id, stranger_id, mine_id, theirs_id = ids

    try:
        match = [row for row in _pending(client) if row["id"] == call_id]
        assert match and match[0]["document_id"] == mine_id
    finally:
        db = SessionLocal()
        try:
            db.query(AgentToolCall).filter(AgentToolCall.id == call_id).delete()
            db.query(Run).filter(Run.id == run_id).delete()
            db.query(Document).filter(Document.id.in_([mine_id, theirs_id])).delete(
                synchronize_session=False
            )
            db.query(Membership).filter(
                Membership.workspace_id == stranger_id
            ).delete()
            db.query(Workspace).filter(Workspace.id == stranger_id).delete()
            db.commit()
        finally:
            db.close()


class _FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def test_approving_a_listed_edit_applies_it_and_finishes_the_run(
    client, document, monkeypatch
):
    """The listed id must drive the same approval path the chat card uses.

    Parks a real agent turn on edit_document, reads the id back out of
    /api/documents-pending, and posts it to the existing decision endpoint —
    the write must land and the run must finish, with the row leaving the list.
    """
    monkeypatch.setattr(
        "app.services.agent_loop._default_model_step",
        lambda settings, run, evidence: (
            lambda input_items, tools, instructions: [
                ("completed", _FakeResponse(output=[], output_text="Applied."))
            ]
        ),
    )
    bootstrap = client.get("/api/bootstrap").json()
    identity = bootstrap["identity"]
    conversation_id = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "resume-conv-" + os.urandom(6).hex()},
        json={"title": "Resume"},
    ).json()["id"]

    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation_id,
            agent_id=bootstrap["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="Change alpha to omega",
        )
        db.add(run)
        db.commit()
        run_id = run.id

        def model_step(input_items, tools, instructions):
            return [
                (
                    "completed",
                    _FakeResponse(
                        output=[
                            SimpleNamespace(
                                type="function_call",
                                name="edit_document",
                                call_id="doc-pending-1",
                                arguments=json.dumps(
                                    {
                                        "document_id": document["id"],
                                        "find": "alpha",
                                        "replace": "omega",
                                    }
                                ),
                            )
                        ]
                    ),
                )
            ]

        # None means the turn parked for approval rather than answering.
        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
    finally:
        db.close()

    listed = [row for row in _pending(client) if row["run_id"] == run_id]
    assert len(listed) == 1
    assert listed[0]["document_id"] == document["id"]
    assert "@@" in listed[0]["proposal_preview"]

    decision = client.post(
        f"/api/agent-tool-calls/{listed[0]['id']}/decision",
        headers={"Idempotency-Key": "resume-decide-" + os.urandom(6).hex()},
        json={"decision": "approved"},
    )
    assert decision.status_code == 200, decision.text

    db = SessionLocal()
    try:
        assert db.get(Document, document["id"]).content == "omega\nbeta\n"
        assert db.get(Run, run_id).status == "completed"
        call = (
            db.query(AgentToolCall)
            .filter(
                AgentToolCall.run_id == run_id,
                AgentToolCall.name == "edit_document",
            )
            .one()
        )
        assert call.status == "succeeded"
    finally:
        db.close()
    assert not [row for row in _pending(client) if row["run_id"] == run_id]
