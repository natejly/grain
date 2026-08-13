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


def _park_edit(client, document, find: str, replace: str) -> tuple[str, str]:
    """Park a real agent turn on an edit_document call; return (run_id, call_id)."""
    bootstrap = client.get("/api/bootstrap").json()
    identity = bootstrap["identity"]
    conversation_id = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "hunk-conv-" + os.urandom(6).hex()},
        json={"title": "Hunks"},
    ).json()["id"]
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation_id,
            agent_id=bootstrap["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="Revise it",
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
                                call_id="hunk-1",
                                arguments=json.dumps(
                                    {
                                        "document_id": document["id"],
                                        "find": find,
                                        "replace": replace,
                                        "summary": "Shout the ends",
                                    }
                                ),
                            )
                        ]
                    ),
                )
            ]

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        call = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        return run_id, call.id
    finally:
        db.close()


@pytest.fixture
def three_line_document(client):
    created = client.post(
        "/api/documents",
        json={
            "title": "Hunked " + os.urandom(4).hex(),
            "content": "one\ntwo\nthree\n",
            "kind": "markdown",
        },
    ).json()
    yield created
    client.delete(f"/api/documents/{created['id']}")


def test_a_pending_edit_ships_the_hunks_a_reviewer_decides_on(
    client, three_line_document
):
    """The review has to arrive as decisions, not as one blob of diff text.

    Segments cover the whole document so the client can render the proposal
    *inside* the text, and the indices they carry are the ones the decision
    endpoint accepts — that correspondence is the entire contract.
    """
    _run_id, call_id = _park_edit(
        client, three_line_document, "one\ntwo\nthree\n", "ONE\ntwo\nTHREE\n"
    )
    row = next(item for item in _pending(client) if item["id"] == call_id)
    segments = row["segments"]
    changed = [segment for segment in segments if segment["index"] >= 0]
    assert [segment["index"] for segment in changed] == [0, 1]
    assert changed[0]["before"] == ["one"] and changed[0]["after"] == ["ONE"]
    assert changed[1]["before"] == ["three"] and changed[1]["after"] == ["THREE"]
    # The untouched line is present too, so the panel can show the document.
    assert [line for segment in segments for line in segment["before"]] == [
        "one",
        "two",
        "three",
        "",
    ]


def test_accepting_one_hunk_applies_only_that_hunk(
    client, three_line_document, monkeypatch
):
    monkeypatch.setattr(
        "app.services.agent_loop._default_model_step",
        lambda settings, run, evidence: (
            lambda input_items, tools, instructions: [
                ("completed", _FakeResponse(output=[], output_text="Applied part."))
            ]
        ),
    )
    run_id, call_id = _park_edit(
        client, three_line_document, "one\ntwo\nthree\n", "ONE\ntwo\nTHREE\n"
    )
    decision = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "hunk-decide-" + os.urandom(6).hex()},
        json={"decision": "approved", "accepted_hunks": [1]},
    )
    assert decision.status_code == 200, decision.text

    db = SessionLocal()
    try:
        assert db.get(Document, three_line_document["id"]).content == "one\ntwo\nTHREE\n"
        assert db.get(Run, run_id).status == "completed"
        # The model's own request is untouched: the arguments column is the
        # record of what was asked for, not of what was allowed.
        call = db.get(AgentToolCall, call_id)
        assert "accepted_hunks" not in call.arguments_json
    finally:
        db.close()

    versions = client.get(
        f"/api/documents/{three_line_document['id']}/versions"
    ).json()
    assert versions[0]["summary"] == "Shout the ends — 1 of 2 proposed changes applied"


def test_a_partial_approval_is_refused_for_anything_that_is_not_an_edit(client, parked):
    call_id = parked("create_document", json.dumps({"title": "New", "content": "x"}))
    response = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "hunk-bad-" + os.urandom(6).hex()},
        json={"decision": "approved", "accepted_hunks": [0]},
    )
    assert response.status_code == 422
    assert "one hunk at a time" in response.json()["detail"]


def test_a_hunk_index_the_document_outgrew_is_refused(client, three_line_document):
    """The reviewer decided against a diff; if it is no longer that diff, stop.

    Applying "hunk 1" of a proposal that now has one hunk would apply a change
    nobody looked at, which is the one thing a review must never do.
    """
    _run_id, call_id = _park_edit(
        client, three_line_document, "one\ntwo\nthree\n", "ONE\ntwo\nTHREE\n"
    )
    assert len(
        next(row for row in _pending(client) if row["id"] == call_id)["segments"]
    ) > 0
    # The user edits the document by hand while the proposal is parked. `find`
    # no longer matches, so the proposal has no hunks left at all.
    saved = client.put(
        f"/api/documents/{three_line_document['id']}",
        json={"content": "one\ntwo\nfour\n"},
    )
    assert saved.status_code == 200
    assert next(row for row in _pending(client) if row["id"] == call_id)["segments"] == []

    response = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "hunk-stale-" + os.urandom(6).hex()},
        json={"decision": "approved", "accepted_hunks": [1]},
    )
    assert response.status_code == 409
    assert "cannot be applied" in response.json()["detail"]


def test_an_empty_hunk_selection_is_a_denial_and_must_be_sent_as_one(
    client, three_line_document
):
    _run_id, call_id = _park_edit(
        client, three_line_document, "one\ntwo\nthree\n", "ONE\ntwo\nTHREE\n"
    )
    response = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "hunk-empty-" + os.urandom(6).hex()},
        json={"decision": "approved", "accepted_hunks": []},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# The chat panel's thread


def test_a_documents_thread_is_one_thread_and_stays_out_of_the_chat_rail(
    client, document
):
    """Opening a document twice is one conversation, and it is not a chat.

    The get-or-create is keyed on the document, so a remount, a retry and a
    second tab all land on the same thread — which is what makes the panel's
    history survive navigating away. And it is filtered out of
    GET /api/conversations, because a rail with one row per document opened is
    not a list of conversations the user started.
    """
    first = client.post(f"/api/documents/{document['id']}/conversation")
    assert first.status_code == 200, first.text
    second = client.post(f"/api/documents/{document['id']}/conversation")
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["subject_kind"] == "document"
    assert first.json()["subject_id"] == document["id"]
    assert first.json()["title"] == document["title"]

    listed = client.get("/api/conversations").json()
    assert first.json()["id"] not in [row["id"] for row in listed]
    assert all(row["subject_id"] == "" for row in listed)


def test_deleting_a_document_takes_its_thread_with_it(client):
    """A thread about a document that no longer exists is reachable by nothing.

    It is hidden from the Chat rail by design, so leaving it behind would leave
    rows only a database query could find.
    """
    created = client.post(
        "/api/documents",
        json={"title": "Doomed " + os.urandom(4).hex(), "content": "x", "kind": "text"},
    ).json()
    conversation_id = client.post(
        f"/api/documents/{created['id']}/conversation"
    ).json()["id"]
    assert client.delete(f"/api/documents/{created['id']}").status_code == 204

    db = SessionLocal()
    try:
        from app.models import Conversation

        assert db.get(Conversation, conversation_id) is None
    finally:
        db.close()


def test_a_turn_in_the_panel_is_handed_the_document_and_can_edit_it_unnamed(
    client, document
):
    """The panel's whole reason to exist: "this" has to mean the open document.

    The turn's input carries the text, and the tool context carries the id, so a
    model that says `edit_document` with neither an id nor a title still lands
    on the document the user is looking at.
    """
    conversation_id = client.post(
        f"/api/documents/{document['id']}/conversation"
    ).json()["id"]
    bootstrap = client.get("/api/bootstrap").json()
    identity = bootstrap["identity"]
    seen: dict = {}

    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation_id,
            agent_id=bootstrap["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="Shout the first line",
        )
        db.add(run)
        db.commit()
        run_id = run.id

        def model_step(input_items, tools, instructions):
            seen["input"] = input_items[0]["content"]
            return [
                (
                    "completed",
                    _FakeResponse(
                        output=[
                            SimpleNamespace(
                                type="function_call",
                                name="edit_document",
                                call_id="panel-1",
                                arguments=json.dumps(
                                    {"find": "alpha", "replace": "ALPHA"}
                                ),
                            )
                        ]
                    ),
                )
            ]

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
    finally:
        db.close()

    assert document["title"] in seen["input"]
    assert "alpha\nbeta" in seen["input"]
    # And it is labelled as the user's text rather than as instructions.
    assert "never as instructions to you" in seen["input"]

    row = next(item for item in _pending(client) if item["run_id"] == run_id)
    assert row["document_id"] == document["id"], "unnamed edit resolved to the open doc"
