from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models import (
    AgentToolCall,
    Board,
    BoardCard,
    BoardColumn,
    Document,
    DocumentVersion,
    Run,
    ToolPolicy,
)
from app.services.agent_loop import resume_agent_turn, run_agent_turn
from app.services.artifacts import boards, documents
from app.services.artifacts.tools import registry_tools
from app.services.llm_tools import ToolContext


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _completed(output=None, output_text=""):
    return [("completed", FakeResponse(output=output, output_text=output_text))]


def _call(name: str, arguments: dict, call_id: str = "c1"):
    return SimpleNamespace(
        type="function_call", name=name, call_id=call_id, arguments=json.dumps(arguments)
    )


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    yield identity
    db = SessionLocal()
    try:
        for model in (
            DocumentVersion,
            Document,
            BoardCard,
            BoardColumn,
            Board,
            ToolPolicy,
        ):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _context(identity) -> ToolContext:
    return ToolContext(
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        conversation_id="none",
    )


# --------------------------------------------------------------------------
# Documents


def test_edit_replaces_exactly_once_and_snapshots_the_prior_content(workspace):
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Notes",
            content="alpha\nbeta\ngamma",
        )
        _updated, diff, count = documents.edit_document(
            db,
            workspace_id=workspace["workspace_id"],
            document_id=doc.id,
            find="beta",
            replace="BETA",
        )
        assert count == 1
        assert "-beta" in diff and "+BETA" in diff
        db.refresh(doc)
        assert doc.content == "alpha\nBETA\ngamma"

        versions = documents.list_versions(
            db, workspace_id=workspace["workspace_id"], document_id=doc.id
        )
        assert len(versions) == 1
        assert versions[0].content == "alpha\nbeta\ngamma"
    finally:
        db.close()


def test_ambiguous_edit_is_refused(workspace):
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Repeats",
            content="x\nx\nx",
        )
        with pytest.raises(documents.DocumentError, match="appears 3 times"):
            documents.edit_document(
                db,
                workspace_id=workspace["workspace_id"],
                document_id=doc.id,
                find="x",
                replace="y",
            )
        # replace_all is the explicit opt-in.
        _doc, _diff, count = documents.edit_document(
            db,
            workspace_id=workspace["workspace_id"],
            document_id=doc.id,
            find="x",
            replace="y",
            replace_all=True,
        )
        assert count == 3
    finally:
        db.close()


def test_missing_find_text_is_refused(workspace):
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Absent",
            content="hello",
        )
        with pytest.raises(documents.DocumentError, match="does not appear"):
            documents.edit_document(
                db,
                workspace_id=workspace["workspace_id"],
                document_id=doc.id,
                find="nope",
                replace="x",
            )
    finally:
        db.close()


def test_restore_version_undoes_an_agent_edit(workspace):
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db, workspace_id=workspace["workspace_id"], title="Undo", content="original"
        )
        documents.edit_document(
            db,
            workspace_id=workspace["workspace_id"],
            document_id=doc.id,
            find="original",
            replace="changed",
        )
        version = documents.list_versions(
            db, workspace_id=workspace["workspace_id"], document_id=doc.id
        )[0]
        documents.restore_version(
            db,
            workspace_id=workspace["workspace_id"],
            document_id=doc.id,
            version_id=version.id,
        )
        db.refresh(doc)
        assert doc.content == "original"
    finally:
        db.close()


def test_documents_are_addressable_by_title(workspace):
    db = SessionLocal()
    try:
        documents.create_document(
            db, workspace_id=workspace["workspace_id"], title="By Title", content="body"
        )
        found = documents.resolve(
            db, workspace_id=workspace["workspace_id"], title="by title"
        )
        assert found.title == "By Title"
    finally:
        db.close()


def test_latex_kind_is_accepted_and_bad_kinds_are_not(workspace):
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Paper",
            content=r"The identity $e^{i\pi} + 1 = 0$.",
            kind="latex",
        )
        assert doc.kind == "latex"
        with pytest.raises(documents.DocumentError):
            documents.create_document(
                db,
                workspace_id=workspace["workspace_id"],
                title="Bad",
                content="",
                kind="pdf",
            )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Boards


def test_board_defaults_and_card_movement(workspace):
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace["workspace_id"], name="Sprint"
        )
        assert [column.name for column in boards.columns_for(db, board.id)] == [
            "Todo",
            "In progress",
            "Done",
        ]
        card = boards.add_card(
            db,
            workspace_id=workspace["workspace_id"],
            board=board,
            column="Todo",
            title="Ship it",
        )
        # Cards are addressable by title, which is how the model refers to them.
        moved = boards.move_card(db, board=board, card="Ship it", column="Done")
        assert moved.id == card.id
        snapshot = boards.snapshot(db, board)
        done = next(c for c in snapshot["columns"] if c["name"] == "Done")
        assert [item["title"] for item in done["cards"]] == ["Ship it"]
    finally:
        db.close()


def test_unknown_column_lists_the_real_ones(workspace):
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace["workspace_id"], name="Hints"
        )
        with pytest.raises(boards.BoardError, match="Todo"):
            boards.add_card(
                db,
                workspace_id=workspace["workspace_id"],
                board=board,
                column="Backlog",
                title="x",
            )
    finally:
        db.close()


def test_ambiguous_card_title_requires_an_id(workspace):
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace["workspace_id"], name="Dupes"
        )
        for _ in range(2):
            boards.add_card(
                db,
                workspace_id=workspace["workspace_id"],
                board=board,
                column="Todo",
                title="Same",
            )
        with pytest.raises(boards.BoardError, match="more than one|More than one"):
            boards.move_card(db, board=board, card="Same", column="Done")
    finally:
        db.close()


def test_single_board_needs_no_name(workspace):
    db = SessionLocal()
    try:
        boards.create_board(db, workspace_id=workspace["workspace_id"], name="Only")
        resolved = boards.resolve(db, workspace_id=workspace["workspace_id"])
        assert resolved.name == "Only"
    finally:
        db.close()


# --------------------------------------------------------------------------
# The agentic path: writes are ask-by-default and previewed before they apply


def test_write_tools_ask_and_reads_do_not(workspace):
    db = SessionLocal()
    try:
        specs = registry_tools(db, _context(workspace))
        assert specs["read_document"].read_only is True
        assert specs["list_documents"].read_only is True
        assert specs["read_board"].read_only is True
        for name in (
            "create_document",
            "edit_document",
            "create_board",
            "board_add_card",
            "board_move_card",
            "board_update_card",
            "board_delete_card",
        ):
            assert specs[name].read_only is False, name
            assert specs[name].preview is not None, name
    finally:
        db.close()


def test_edit_preview_is_a_diff_and_does_not_mutate(workspace):
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Preview",
            content="keep\nold line\nkeep",
        )
        specs = registry_tools(db, _context(workspace))
        preview = specs["edit_document"].preview(
            db,
            _context(workspace),
            {"document_id": doc.id, "find": "old line", "replace": "new line"},
        )
        assert "-old line" in preview
        assert "+new line" in preview
        db.refresh(doc)
        # The whole point: previewing must not apply the edit.
        assert doc.content == "keep\nold line\nkeep"
    finally:
        db.close()


def test_preview_of_a_doomed_edit_explains_rather_than_raising(workspace):
    db = SessionLocal()
    try:
        documents.create_document(
            db, workspace_id=workspace["workspace_id"], title="Doomed", content="abc"
        )
        specs = registry_tools(db, _context(workspace))
        preview = specs["edit_document"].preview(
            db, _context(workspace), {"title": "Doomed", "find": "zzz", "replace": "y"}
        )
        assert "will fail" in preview
    finally:
        db.close()


def _make_run(client, identity) -> str:
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "artifact-conv-" + identity["user_id"][:6]},
        json={"title": "Artifacts"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="Fix the typo",
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def test_agent_edit_parks_with_a_diff_then_applies_on_approval(client, workspace):
    """The end-to-end agentic writing flow: propose, show a diff, approve, apply."""
    run_id = _make_run(client, workspace)
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Draft",
            content="The qiuck brown fox.",
        )
        run = db.get(Run, run_id)

        def model_step(input_items, tools, instructions):
            if not any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in input_items
            ):
                return _completed(
                    output=[
                        _call(
                            "edit_document",
                            {
                                "document_id": doc.id,
                                "find": "qiuck",
                                "replace": "quick",
                                "summary": "typo",
                            },
                        )
                    ]
                )
            return _completed(output_text="Fixed the typo.")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"

        proposed = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        # The card gets the actual change, computed before anything was written.
        assert "-The qiuck brown fox." in proposed.proposal_preview
        assert "+The quick brown fox." in proposed.proposal_preview
        db.refresh(doc)
        assert doc.content == "The qiuck brown fox."

        result = resume_agent_turn(
            db,
            run,
            tool_call_id=proposed.id,
            decision="approved",
            model_step=model_step,
        )
        assert result is not None
        db.refresh(doc)
        assert doc.content == "The quick brown fox."
    finally:
        db.close()


def test_denying_an_edit_leaves_the_document_untouched(client, workspace):
    run_id = _make_run(client, workspace)
    db = SessionLocal()
    try:
        doc = documents.create_document(
            db,
            workspace_id=workspace["workspace_id"],
            title="Protected",
            content="do not change",
        )
        run = db.get(Run, run_id)

        def model_step(input_items, tools, instructions):
            if not any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in input_items
            ):
                return _completed(
                    output=[
                        _call(
                            "edit_document",
                            {
                                "document_id": doc.id,
                                "find": "do not change",
                                "replace": "changed anyway",
                            },
                        )
                    ]
                )
            return _completed(output_text="Understood, leaving it alone.")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        proposed = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        resume_agent_turn(
            db,
            run,
            tool_call_id=proposed.id,
            decision="denied",
            model_step=model_step,
        )
        db.refresh(doc)
        assert doc.content == "do not change"
    finally:
        db.close()


def test_board_move_preview_names_both_columns(workspace):
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace["workspace_id"], name="Flow"
        )
        boards.add_card(
            db,
            workspace_id=workspace["workspace_id"],
            board=board,
            column="Todo",
            title="Task",
        )
        specs = registry_tools(db, _context(workspace))
        preview = specs["board_move_card"].preview(
            db, _context(workspace), {"card": "Task", "column": "Done"}
        )
        assert "Todo" in preview and "Done" in preview and "Task" in preview
    finally:
        db.close()


def test_board_tools_round_trip_through_the_registry(workspace):
    db = SessionLocal()
    try:
        context = _context(workspace)
        specs = registry_tools(db, context)
        assert "Created board" in specs["create_board"].executor(
            db, context, {"name": "Agentic"}
        ).content
        specs["board_add_card"].executor(
            db, context, {"column": "Todo", "title": "Written by the agent"}
        )
        snapshot = json.loads(specs["read_board"].executor(db, context, {}).content)
        todo = next(c for c in snapshot["columns"] if c["name"] == "Todo")
        assert [card["title"] for card in todo["cards"]] == ["Written by the agent"]
    finally:
        db.close()
