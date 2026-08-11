"""Hosted web search, and the claim that its citations are not exempt.

The point of these tests is not that a search happened. It is that a web source
reaches the user by the same road a retrieved passage does: mapped to `Evidence`,
anchored in the answer as `[n]`, counted by `validate_citations`, reported before
the run is closed, and dropped rather than trusted when it arrives malformed.

No network: the hosted tool is provider-executed, so the only thing this side of
the wire ever sees is a completed response object. These build that object.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import Message, Run, RunEvent
from app.services.agent_loop import LoopState, _tool_payload, run_agent_turn
from app.services.citations import validate_citations
from app.services.llm_tools import ToolContext, build_registry
from app.services.retrieval import Evidence
from app.services.runs import _citations, _finish_run
from app.services.web_search import (
    WebEvidence,
    anchor_citations,
    harvest,
    revive_evidence,
    web_numbers,
    web_search_tool,
)

ANSWER = "The bridge reopened in March. Traffic fell by a fifth."


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update=overrides)


def _annotation(
    url: str = "https://example.com/bridge",
    title: str = "Bridge reopening",
    start: Any = 0,
    end: Any = 29,
    kind: str = "url_citation",
) -> dict[str, Any]:
    return {
        "type": kind,
        "url": url,
        "title": title,
        "start_index": start,
        "end_index": end,
    }


def _response(
    *,
    text: str = ANSWER,
    annotations: list[Any] | None = None,
    query: str | None = "bridge reopening traffic",
    status: str = "completed",
    sources: list[str] | None = None,
) -> SimpleNamespace:
    """A completed Responses object shaped like one that used the hosted tool."""
    output: list[Any] = []
    if query is not None:
        output.append(
            SimpleNamespace(
                type="web_search_call",
                status=status,
                action=SimpleNamespace(
                    type="search",
                    query=query,
                    sources=[
                        SimpleNamespace(type="url", url=url) for url in sources or []
                    ],
                ),
            )
        )
    output.append(
        SimpleNamespace(
            type="message",
            content=[
                SimpleNamespace(
                    type="output_text", text=text, annotations=annotations or []
                )
            ],
        )
    )
    return SimpleNamespace(output=output, output_text=text)


def _streamed(response: SimpleNamespace, text: str = ANSWER) -> list[tuple[str, Any]]:
    """One model turn as the loop consumes it: deltas, then the terminal response."""
    return [("delta", text), ("completed", response)]


# --- mapping ---------------------------------------------------------------


def test_annotation_becomes_evidence_numbered_after_the_passages():
    outcome = harvest(
        _response(annotations=[_annotation()]),
        ANSWER,
        settings=_settings(),
        evidence_offset=3,
    )
    assert len(outcome.evidence) == 1
    source = outcome.evidence[0]
    assert isinstance(source, Evidence)
    assert source.url == "https://example.com/bridge"
    assert source.filename == "Bridge reopening"
    # The excerpt is the span the provider said this source supports.
    assert source.excerpt == "The bridge reopened in March."
    # Continues the sequence the retrieved passages were numbered in, so [4] in
    # the answer and evidence[3] in the report are the same source.
    assert outcome.anchors == ((29, 4),)
    assert outcome.queries == ("bridge reopening traffic",)


def test_one_source_cited_twice_is_one_piece_of_evidence_and_two_markers():
    outcome = harvest(
        _response(
            annotations=[
                _annotation(start=0, end=29),
                _annotation(start=30, end=len(ANSWER)),
            ]
        ),
        ANSWER,
        settings=_settings(),
        evidence_offset=0,
    )
    assert len(outcome.evidence) == 1
    assert outcome.anchors == ((29, 1), (len(ANSWER), 1))
    anchored = anchor_citations(ANSWER, outcome.anchors)
    assert anchored == "The bridge reopened in March.[1] Traffic fell by a fifth.[1]"


def test_markers_at_one_position_are_written_in_order():
    text = "Two sources agree."
    anchored = anchor_citations(text, ((18, 3), (18, 2), (18, 2)))
    assert anchored == "Two sources agree.[2][3]"


def test_max_results_bounds_how_many_sources_become_evidence():
    annotations = [
        _annotation(url=f"https://example.com/{index}", start=0, end=10)
        for index in range(6)
    ]
    outcome = harvest(
        _response(annotations=annotations),
        ANSWER,
        settings=_settings(web_search_max_results=2),
        evidence_offset=0,
    )
    assert [item.url for item in outcome.evidence] == [
        "https://example.com/0",
        "https://example.com/1",
    ]
    # A source that was dropped must not leave a marker pointing at nothing.
    assert {number for _, number in outcome.anchors} == {1, 2}


def test_web_evidence_survives_a_parked_turn_as_a_web_source():
    """A resumed turn must not silently demote a citation to an unclickable title."""
    state = LoopState(
        evidence=[
            Evidence(
                chunk_id="chunk-1",
                source_id="source-1",
                filename="brief.md",
                ordinal=0,
                excerpt="Indexed passage.",
                score=0.5,
            ),
            WebEvidence(
                chunk_id="web:abc",
                source_id="web:abc",
                filename="Bridge reopening",
                ordinal=0,
                excerpt="The bridge reopened in March.",
                score=0.0,
                url="https://example.com/bridge",
            ),
        ]
    )
    revived = LoopState.from_json(state.to_json()).evidence
    assert [type(item) for item in revived] == [Evidence, WebEvidence]
    assert isinstance(revived[1], WebEvidence)
    assert revived[1].url == "https://example.com/bridge"
    assert revive_evidence({"url": "https://example.com", **_plain()}).url == (
        "https://example.com"
    )


def _plain() -> dict[str, Any]:
    return {
        "chunk_id": "c",
        "source_id": "s",
        "filename": "f",
        "ordinal": 0,
        "excerpt": "e",
        "score": 0.0,
    }


def test_citation_payload_carries_the_url_for_web_sources_only():
    rendered = _citations(
        [
            Evidence(**_plain()),
            WebEvidence(url="https://example.com/bridge", **_plain()),
        ]
    )
    assert rendered[0]["url"] is None
    assert rendered[1]["url"] == "https://example.com/bridge"


# --- malformed input -------------------------------------------------------


def test_malformed_annotations_are_rejected_rather_than_crashing():
    annotations: list[Any] = [
        _annotation(url=""),
        _annotation(url="javascript:alert(1)"),
        _annotation(url="not-a-url"),
        _annotation(kind="file_citation"),
        {"type": "url_citation"},
        "not an object at all",
        None,
    ]
    outcome = harvest(
        _response(annotations=annotations),
        ANSWER,
        settings=_settings(),
        evidence_offset=0,
    )
    assert outcome.evidence == ()
    assert outcome.anchors == ()
    # The search itself still happened, and the trail still says so.
    assert outcome.queries == ("bridge reopening traffic",)


def test_an_unusable_span_keeps_the_source_but_drops_the_marker():
    """Reported as an uncited source beats a marker on a sentence it never supported."""
    outcome = harvest(
        _response(
            annotations=[
                _annotation(start=0, end=9999),
                _annotation(url="https://example.com/b", start="4", end="9"),
                _annotation(url="https://example.com/c", start=True, end=True),
                _annotation(url="https://example.com/d", start=20, end=5),
            ]
        ),
        ANSWER,
        settings=_settings(),
        evidence_offset=0,
    )
    assert len(outcome.evidence) == 4
    assert outcome.anchors == ()
    # No span to quote, so the title stands in as the excerpt.
    assert outcome.evidence[0].excerpt == "Bridge reopening"
    assert anchor_citations(ANSWER, outcome.anchors) == ANSWER


def test_a_response_of_the_wrong_shape_costs_the_sources_not_the_turn():
    outcome = harvest(
        object(), ANSWER, settings=_settings(), evidence_offset=0
    )
    assert outcome.evidence == ()

    class Exploding:
        @property
        def output(self) -> Any:
            raise RuntimeError("provider changed the shape")

    broken = harvest(Exploding(), ANSWER, settings=_settings(), evidence_offset=0)
    assert broken.evidence == ()
    # Invisible degradation is the thing to avoid: the trail records the failure.
    assert broken.failed is True
    assert broken.happened is True


# --- the tool on the wire --------------------------------------------------


def test_the_tool_carries_the_configured_context_size():
    tool = web_search_tool(_settings(web_search_context_size="high"))
    assert tool == {"type": "web_search", "search_context_size": "high"}


def test_disabling_the_flag_removes_the_tool_from_the_payload():
    assert web_search_tool(_settings(web_search_enabled=False)) is None
    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id="workspace-1", user_id="user-1", conversation_id=None
        )
        registry = build_registry(db, context)
        enabled = _tool_payload(registry, _settings(web_search_enabled=True))
        disabled = _tool_payload(registry, _settings(web_search_enabled=False))
    finally:
        db.close()
    assert any(entry.get("type") == "web_search" for entry in enabled)
    assert not any(entry.get("type") == "web_search" for entry in disabled)
    # Only the hosted entry differs; the local function tools are untouched.
    assert len(enabled) == len(disabled) + 1


def test_the_request_payload_omits_the_tool_entirely_when_disabled(client):
    """The flag has to take the tool off the wire, not merely discourage its use."""
    run_id = _make_run(client)
    seen: list[list[dict[str, Any]]] = []
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None

        def model_step(input_items, tools, instructions):
            seen.append(tools)
            return _streamed(_response(annotations=[_annotation()]))

        result = run_agent_turn(
            db,
            run,
            evidence=[],
            settings=_settings(web_search_enabled=False),
            model_step=model_step,
        )
    finally:
        db.close()
    assert result is not None
    assert seen and not any(
        entry.get("type") == "web_search" for entry in seen[0]
    )
    assert "web_search" not in json.dumps(seen[0])
    # No tool means no annotations to honour, so the answer is left alone.
    assert result.answer == ANSWER
    assert result.evidence == []


# --- through the loop ------------------------------------------------------


def _make_run(client) -> str:
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "web-search-" + identity["user_id"][:8]},
        json={"title": "Web search"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="Did the bridge reopen?",
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _passage() -> Evidence:
    return Evidence(
        chunk_id="chunk-1",
        source_id="source-1",
        filename="notes.md",
        ordinal=0,
        excerpt="The council minutes mention the bridge.",
        score=0.7,
    )


def _events(run_id: str) -> list[str]:
    db = SessionLocal()
    try:
        return [
            event.event_type
            for event in db.query(RunEvent)
            .filter(RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence)
            .all()
        ]
    finally:
        db.close()


def test_a_web_source_is_anchored_validated_and_reported_before_completion(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None

        def model_step(input_items, tools, instructions):
            assert any(entry.get("type") == "web_search" for entry in tools)
            return _streamed(
                _response(
                    annotations=[_annotation(start=0, end=29)],
                    sources=["https://example.com/other"],
                )
            )

        result = run_agent_turn(
            db,
            run,
            evidence=[_passage()],
            settings=_settings(),
            model_step=model_step,
        )
        assert result is not None
    finally:
        db.close()

    # The marker is in the answer the user is shown, numbered after the passage.
    assert result.answer == "The bridge reopened in March.[2] Traffic fell by a fifth."
    assert len(result.evidence) == 2
    assert isinstance(result.evidence[1], WebEvidence)

    # And the validator resolves it, by the path a passage takes: no special case.
    report = validate_citations(result.answer, result.evidence)
    assert report.is_valid is True
    assert report.cited == (2,)
    assert report.uncited == (1,)

    _finish_run(run, answer=result.answer, evidence=result.evidence)

    events = _events(run_id)
    # The golden-path invariant: run.completed is terminal for stream consumers,
    # so nothing about a citation may be appended after it.
    assert events[-1] == "run.completed"
    assert events.index("web_search.completed") < events.index("run.completed")
    assert events.index("run.citations") < events.index("run.completed")

    db = SessionLocal()
    try:
        message = (
            db.query(Message)
            .filter(Message.run_id == run_id, Message.role == "assistant")
            .one()
        )
        citations = json.loads(message.citations_json)
    finally:
        db.close()
    assert [item["url"] for item in citations] == [None, "https://example.com/bridge"]
    assert citations[1]["filename"] == "Bridge reopening"

    # And it survives the response model. `Citation` used to declare no `url`,
    # so FastAPI dropped it on the way out and every web citation reached the
    # browser as an address-less title: the run stored provenance the product
    # could not show. The stored payload above proves nothing about that.
    conversation_id = _conversation_of(run_id)
    served = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assistant = [row for row in served if row["run_id"] == run_id and row["role"] == "assistant"]
    assert [item["url"] for item in assistant[0]["citations"]] == [
        None,
        "https://example.com/bridge",
    ]

    # A web citation's chunk id addresses no Chunk row, and the route says so
    # rather than answering "provenance not found" as if the index had lost it.
    missing = client.get(f"/api/chunks/{assistant[0]['citations'][1]['chunk_id']}")
    assert missing.status_code == 404
    assert "url" in missing.json()["detail"]


def _conversation_of(run_id: str) -> str:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        return run.conversation_id
    finally:
        db.close()


def test_a_fabricated_marker_is_still_caught_when_web_sources_are_present(client):
    """Adding a class of source must not widen what counts as a valid citation."""
    run_id = _make_run(client)
    text = "The bridge reopened in March. [7]"
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        result = run_agent_turn(
            db,
            run,
            evidence=[_passage()],
            settings=_settings(),
            model_step=lambda items, tools, instructions: _streamed(
                _response(text=text, annotations=[_annotation(start=0, end=29)]),
                text,
            ),
        )
        assert result is not None
    finally:
        db.close()
    report = validate_citations(result.answer, result.evidence)
    assert report.has_fabricated_citations is True
    assert report.out_of_range == (7,)


def test_a_failed_search_degrades_to_an_answer_without_web_sources(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        result = run_agent_turn(
            db,
            run,
            evidence=[],
            settings=_settings(),
            model_step=lambda items, tools, instructions: _streamed(
                _response(status="failed", annotations=[])
            ),
        )
        assert result is not None
    finally:
        db.close()
    assert result.answer == ANSWER
    assert result.evidence == []

    events = _events(run_id)
    assert "web_search.completed" in events
    db = SessionLocal()
    try:
        payload = json.loads(
            db.query(RunEvent)
            .filter(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "web_search.completed",
            )
            .one()
            .payload_json
        )
    finally:
        db.close()
    assert payload["failed"] is True
    assert payload["count"] == 0
    assert payload["queries"] == ["bridge reopening traffic"]


def test_a_turn_without_web_search_writes_no_web_event(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        result = run_agent_turn(
            db,
            run,
            evidence=[],
            settings=_settings(),
            model_step=lambda items, tools, instructions: _streamed(
                _response(query=None, annotations=[])
            ),
        )
        assert result is not None
    finally:
        db.close()
    assert result.answer == ANSWER
    assert "web_search.completed" not in _events(run_id)


# --- offsets are read against the block they were counted in ---------------


PREAMBLE = "Let me look that up. "


def _blocks(*items: Any, text: str) -> SimpleNamespace:
    return SimpleNamespace(output=list(items), output_text=text)


def _message(text: str, annotations: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        content=[
            SimpleNamespace(
                type="output_text", text=text, annotations=annotations or []
            )
        ],
    )


def _search_call() -> SimpleNamespace:
    return SimpleNamespace(
        type="web_search_call",
        status="completed",
        action=SimpleNamespace(type="search", query="bridge reopening", sources=[]),
    )


def test_offsets_are_read_against_the_block_they_were_counted_in():
    """A reasoning model writes a line, searches, then answers — three blocks.

    Each annotation's indices count from its own message; the answer the user sees
    is every block concatenated. Reading one against the other lands the marker
    mid-word, well before the sentence the provider actually cited.
    """
    full = PREAMBLE + ANSWER
    outcome = harvest(
        _blocks(
            _message(PREAMBLE),
            _search_call(),
            _message(ANSWER, [_annotation(start=0, end=29)]),
            text=full,
        ),
        full,
        settings=_settings(),
        evidence_offset=0,
    )
    assert outcome.anchors == ((len(PREAMBLE) + 29, 1),)
    assert outcome.evidence[0].excerpt == "The bridge reopened in March."
    assert anchor_citations(full, outcome.anchors) == (
        PREAMBLE + "The bridge reopened in March.[1] Traffic fell by a fifth."
    )


def test_a_block_missing_from_the_answer_keeps_the_source_and_drops_the_marker():
    """Offsets into a string this side never saw cannot be placed, so they aren't."""
    outcome = harvest(
        _blocks(
            _message("a block the stream never carried", [_annotation(start=0, end=5)]),
            text=ANSWER,
        ),
        ANSWER,
        settings=_settings(),
        evidence_offset=0,
    )
    assert len(outcome.evidence) == 1
    assert outcome.anchors == ()


def test_an_over_long_url_is_dropped_rather_than_truncated():
    """A cut URL is a different URL, and a citation may not link somewhere else."""
    outcome = harvest(
        _response(
            annotations=[_annotation(url="https://example.com/a?q=" + "y" * 3000)]
        ),
        ANSWER,
        settings=_settings(),
        evidence_offset=0,
    )
    assert outcome.evidence == ()


# --- one turn, several searches --------------------------------------------


STEP_ONE = "The bridge reopened."
STEP_TWO = "Traffic fell."


def _search_then_call(text: str, url: str, *, call: bool) -> SimpleNamespace:
    """A response citing `url` for the whole of `text`, optionally with a tool call."""
    output: list[Any] = [
        SimpleNamespace(
            type="message",
            content=[
                SimpleNamespace(
                    type="output_text",
                    text=text,
                    annotations=[_annotation(url=url, start=0, end=len(text))],
                )
            ],
        )
    ]
    if call:
        output.append(
            SimpleNamespace(
                type="function_call", call_id="c1", name="no_such_tool", arguments="{}"
            )
        )
    return SimpleNamespace(output=output, output_text=text)


def test_a_page_cited_by_two_steps_of_one_turn_is_one_source(client):
    """Numbers address positions in `evidence`; one page must not occupy two."""
    run_id = _make_run(client)
    steps: list[int] = []

    def model_step(input_items, tools, instructions):
        steps.append(1)
        first = len(steps) == 1
        text = STEP_ONE if first else STEP_TWO
        return [
            ("delta", text),
            ("completed", _search_then_call(text, "https://a.example/x", call=first)),
        ]

    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        result = run_agent_turn(
            db, run, evidence=[], settings=_settings(), model_step=model_step
        )
    finally:
        db.close()
    assert result is not None
    assert len(result.evidence) == 1
    # Both markers name the one source, so the chunk_id the UI keys on is unique.
    assert result.answer == "The bridge reopened.[1]Traffic fell.[1]"
    report = validate_citations(result.answer, result.evidence)
    assert report.cited == (1,)
    assert report.uncited == ()


def test_max_results_is_a_budget_for_the_turn_not_for_each_step():
    """Six iterations must not be six times the cap the user was promised."""
    settings = _settings(web_search_max_results=2)
    first = harvest(
        _response(
            annotations=[
                _annotation(url=f"https://example.com/{index}", start=0, end=10)
                for index in range(2)
            ]
        ),
        ANSWER,
        settings=settings,
        evidence_offset=0,
    )
    assert len(first.evidence) == 2
    second = harvest(
        _response(annotations=[_annotation(url="https://example.com/9")]),
        ANSWER,
        settings=settings,
        evidence_offset=2,
        known=web_numbers(list(first.evidence)),
    )
    assert second.evidence == ()
    assert second.anchors == ()
