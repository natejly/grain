"""Run presets: the catalogue, the deterministic router, and the send-time
expansion that turns a name into explicit per-turn facts on a row.

THE DOCTRINE UNDER TEST is that a preset is a composer seed, not a run-path
layer. Nothing downstream asks "which preset is this workspace on"; the send
endpoint expands the name into `Run.preset`, `Run.retrieval_budget` and
`Run.step_plan`, and those three columns are the whole transport. The pins that
matter most are therefore: `run.preset` is never the literal "auto", an
explicit per-turn control always beats the preset that would have set it, and
sending no preset at all leaves a run byte-identical to a pre-presets one.
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import Conversation, Run, RunEvent
from app.services import run_presets, subjects
from app.services.llm_tools import ToolContext
from app.services.run_presets import (
    AUTO,
    DEEP_RESEARCH,
    DELIVERABLE,
    GROUNDED,
    QUICK_LOOKUP,
)

# --- (a) the catalogue -------------------------------------------------------


def test_the_catalogue_is_auto_first_then_the_four_policies():
    names = [policy.name for policy in run_presets.catalogue()]
    assert names == [AUTO, QUICK_LOOKUP, GROUNDED, DEEP_RESEARCH, DELIVERABLE]
    for policy in run_presets.catalogue():
        assert policy.label and policy.description


def test_resolve_returns_none_for_unset_unknown_and_auto():
    """"" and a retired name degrade to today's behaviour rather than failing a
    turn — the rule `styles.py` already applies to an unknown style. `auto` is
    None for a different reason: it is a question, and a question is never
    applicable policy."""
    assert run_presets.resolve("") is None
    assert run_presets.resolve("nonsense") is None
    assert run_presets.resolve(AUTO) is None
    assert run_presets.resolve(QUICK_LOOKUP) is not None


def test_grounded_answer_is_the_identity_preset():
    """Picking "the normal turn" must change nothing at all, which is only true
    if every field it pins already equals the default."""
    policy = run_presets.resolve(GROUNDED)
    assert policy is not None
    assert policy.effort == ""
    assert policy.budget == "medium"
    assert policy.families is None
    assert policy.step_plan is False


# --- (b)-(c) the deterministic router ----------------------------------------


@pytest.mark.parametrize(
    "prompt,expected",
    [
        # Deliverable markers win outright, and are checked FIRST: the artefact
        # is the part a user is visibly disappointed not to get.
        ("Write a report on the vendor landscape", DELIVERABLE),
        ("draft a memo for the board", DELIVERABLE),
        ("Build me a dashboard of signups", DELIVERABLE),
        # ...even inside a long, comparative question.
        (
            "Compare our three vendors across price, support and reliability "
            "and then write up a memo the board can read before Thursday, "
            "covering what we should do about the renewal and why, with the "
            "trade-offs spelled out in enough detail that nobody has to ask a "
            "follow-up question about any of it afterwards at all whatsoever",
            DELIVERABLE,
        ),
        # Research: markers, length, or question count.
        ("Compare Postgres and MySQL for this", DEEP_RESEARCH),
        ("What are the pros and cons here?", DEEP_RESEARCH),
        ("Why did the migration slip?", DEEP_RESEARCH),
        ("What? Who? When?", DEEP_RESEARCH),
        # Lookups: short AND opening with an interrogative.
        ("Who owns the launch?", QUICK_LOOKUP),
        ("How many seats do we have", QUICK_LOOKUP),
        ("Define the violet ring", QUICK_LOOKUP),
        # Everything else is the normal turn.
        ("Summarise the launch plan", GROUNDED),
        ("Owner of the launch?", GROUNDED),
        # An artefact NOUN is not a request for one. Each of these routed to
        # the deliverable preset — high effort, ten passages, and a twelve-round
        # plan turn on the API path — for a lookup, because the markers were
        # matched as bare substrings against unpadded nouns.
        ("What is in my memory about the deploy host?", QUICK_LOOKUP),
        ("Who signed the charter?", QUICK_LOOKUP),
        ("When was the deck rebuilt?", QUICK_LOOKUP),
        ("Which slides did legal approve?", QUICK_LOOKUP),
        ("Which chart shows revenue?", QUICK_LOOKUP),
        ("What did the memorandum say about notice periods?", QUICK_LOOKUP),
        ("Summarize my memories from last week", GROUNDED),
        ("What did I memorize yesterday?", QUICK_LOOKUP),
    ],
)
def test_the_router_table(prompt, expected):
    assert run_presets.classify(prompt) == expected


def test_the_router_needs_a_verb_and_an_artefact_not_a_noun():
    """The substring boundary, pinned the way the word-count boundary already
    is. "memo" lives inside "memory", a first-class product noun here, and
    "chart" inside "charter"; word boundaries fix those two, and the verb
    requirement fixes the rest ("When was the deck rebuilt?" is a lookup with a
    perfectly word-bounded "deck" in it).
    """
    assert run_presets.classify("Make me some slides") == DELIVERABLE
    assert run_presets.classify("Update the charts") == DELIVERABLE
    assert run_presets.classify("Write up what we found") == DELIVERABLE
    # The noun alone, the verb alone: neither is a request for an artefact.
    assert run_presets.classify("Which deck is on the shared drive?") == QUICK_LOOKUP
    assert run_presets.classify("Who can build in this workspace?") == QUICK_LOOKUP


def test_the_router_boundaries():
    """12 vs 13 words, 59 vs 60 words, 2 vs 3 question marks. Boundaries are
    where a deterministic classifier earns the word deterministic."""
    twelve = "What is the current owner of record for the launch plan today"
    assert len(twelve.split()) == 12
    assert run_presets.classify(twelve) == QUICK_LOOKUP
    thirteen = twelve + " please"
    assert len(thirteen.split()) == 13
    assert run_presets.classify(thirteen) == GROUNDED

    fifty_nine = "who " + " ".join(f"word{index}" for index in range(58))
    assert len(fifty_nine.split()) == 59
    assert run_presets.classify(fifty_nine) == GROUNDED
    sixty = fifty_nine + " more"
    assert len(sixty.split()) == 60
    assert run_presets.classify(sixty) == DEEP_RESEARCH

    assert run_presets.classify("Is it ready? Really?") == GROUNDED
    assert run_presets.classify("Is it ready? Really? Truly?") == DEEP_RESEARCH


def test_route_resolves_auto_and_leaves_every_other_name_alone():
    assert run_presets.route(AUTO, "Who owns the launch?") == QUICK_LOOKUP
    assert run_presets.route(DEEP_RESEARCH, "Who owns the launch?") == DEEP_RESEARCH
    assert run_presets.route("", "anything") == ""


# --- (d) tool narrowing composes by intersection -----------------------------


def _context(workspace_id: str) -> ToolContext:
    return ToolContext(
        workspace_id=workspace_id,
        user_id="tester",
        conversation_id="",
        run_id="",
    )


def test_a_preset_with_no_families_has_no_opinion(client):
    db = SessionLocal()
    try:
        context = _context("workspace-does-not-matter")
        assert run_presets.allowed_tools_for_preset(db, context, GROUNDED) is None
        assert run_presets.allowed_tools_for_preset(db, context, "") is None
        assert run_presets.allowed_tools_for_preset(db, context, "nonsense") is None
    finally:
        db.close()


def test_quick_lookup_is_exactly_the_core_family(client):
    db = SessionLocal()
    try:
        context = _context("workspace-does-not-matter")
        core = {
            name
            for family, names in run_presets.registry_families(db, context)
            if family == "core"
            for name in names
        }
        assert run_presets.allowed_tools_for_preset(db, context, QUICK_LOOKUP) == core
        assert "search_sources" in core
    finally:
        db.close()


def test_deep_research_admits_delegation_and_not_file_writes(client):
    db = SessionLocal()
    try:
        context = _context("workspace-does-not-matter")
        allowed = run_presets.allowed_tools_for_preset(db, context, DEEP_RESEARCH)
        assert allowed is not None
        assert "delegate" in allowed
        assert not any(name.startswith("fs_") for name in allowed)
    finally:
        db.close()


def test_narrowing_can_only_ever_shrink(client):
    """THE SECURITY PROPERTY. `subjects.narrow` is an intersection, so a preset
    can make a tool absent and never present — it can neither widen an agent's
    provisioned subset nor a subject's families."""
    db = SessionLocal()
    try:
        context = _context("workspace-does-not-matter")
        preset_set = run_presets.allowed_tools_for_preset(db, context, QUICK_LOOKUP)
        assert preset_set is not None
        # An agent that has a tool the preset does not list loses it.
        agent_subset = frozenset({"search_sources", "delegate"})
        assert subjects.narrow(agent_subset, preset_set) == frozenset(
            {"search_sources"}
        )
        # A preset that lists a tool the agent lacks does not hand it over.
        assert "delegate" not in subjects.narrow(
            frozenset({"search_sources"}),
            frozenset({"search_sources", "delegate"}),
        )
    finally:
        db.close()


# --- (e) the endpoint expansion ----------------------------------------------


def _conversation(client, suffix: str) -> str:
    return client.post(
        "/api/conversations",
        headers={"Idempotency-Key": f"preset-conv-{suffix}"},
        json={"title": "Presets"},
    ).json()["id"]


def _send(client, conversation_id: str, suffix: str, content: str, **body):
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": f"preset-msg-{suffix}"},
        json={"content": content, **body},
    )


def _run(run_id: str) -> Run:
    db = SessionLocal()
    try:
        return db.get(Run, run_id)
    finally:
        db.close()


def test_no_preset_leaves_all_three_columns_unset(client):
    """The compatibility pin: a client that never learned about presets sends
    what it always sent and gets the run it always got."""
    conversation = _conversation(client, "none")
    response = _send(client, conversation, "none", "Who owns the launch?")
    assert response.status_code == 202
    run = _run(response.json()["run"]["id"])
    assert run.preset == ""
    assert run.retrieval_budget == ""
    assert run.step_plan is False


def test_a_named_preset_lands_expanded_on_the_row(client):
    conversation = _conversation(client, "quick")
    response = _send(
        client, conversation, "quick", "Who owns the launch?", preset=QUICK_LOOKUP
    )
    assert response.status_code == 202
    run = _run(response.json()["run"]["id"])
    assert run.preset == QUICK_LOOKUP
    assert run.retrieval_budget == "low"
    assert run.step_plan is False
    assert run.requested_effort == "low"


def test_auto_is_routed_before_the_row_is_written(client):
    """`Run.preset` records the ANSWER, never the question. A row saying "auto"
    could not tell anyone afterwards which policy the turn actually ran."""
    conversation = _conversation(client, "auto")
    response = _send(client, conversation, "auto", "Who owns the launch?", preset=AUTO)
    assert response.status_code == 202
    run_id = response.json()["run"]["id"]
    run = _run(run_id)
    assert run.preset == QUICK_LOOKUP
    assert run.preset != AUTO
    db = SessionLocal()
    try:
        queued = (
            db.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.event_type == "run.queued")
            .one()
        )
        import json

        payload = json.loads(queued.payload_json)
        assert payload["preset"] == QUICK_LOOKUP
        assert payload["routed_from"] == AUTO
    finally:
        db.close()


def test_explicit_per_turn_controls_beat_the_preset(client):
    """`step_plan` is Optional on the request precisely so an explicit false can
    outrank a preset that pins it on. Without that distinction "off" and
    "unspecified" would be the same wire value and the user could not say no."""
    conversation = _conversation(client, "explicit")
    response = _send(
        client,
        conversation,
        "explicit",
        "Compare the vendors and evaluate them",
        preset=DEEP_RESEARCH,
        effort="medium",
        step_plan=False,
        retrieval_budget="low",
    )
    assert response.status_code == 202
    run = _run(response.json()["run"]["id"])
    assert run.preset == DEEP_RESEARCH
    assert run.requested_effort == "medium"
    assert run.step_plan is False
    assert run.retrieval_budget == "low"


def test_an_unknown_preset_is_refused(client):
    """A name the composer cannot have offered is a client bug, and a silent
    fallback would run a turn under a policy nobody chose."""
    conversation = _conversation(client, "bogus")
    response = _send(
        client, conversation, "bogus", "Who owns the launch?", preset="nope"
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Preset is not available"


def test_an_off_ladder_retrieval_budget_is_refused_by_pydantic(client):
    conversation = _conversation(client, "bad-budget")
    response = _send(
        client,
        conversation,
        "bad-budget",
        "Who owns the launch?",
        retrieval_budget="enormous",
    )
    assert response.status_code == 422


# --- (f)-(g) the wire surfaces -----------------------------------------------


def test_bootstrap_carries_the_catalogue(client):
    rows = client.get("/api/bootstrap").json()["run_presets"]
    assert [row["name"] for row in rows] == [
        AUTO,
        QUICK_LOOKUP,
        GROUNDED,
        DEEP_RESEARCH,
        DELIVERABLE,
    ]
    quick = next(row for row in rows if row["name"] == QUICK_LOOKUP)
    assert quick["budget"] == "low"
    assert quick["step_plan"] is False
    # NO approval mode rides the catalogue, on any row. The field existed as
    # "a client-side seed only" and the client seeded it straight into the
    # conversation's persistent approval mode — picking "Quick lookup", whose
    # description names effort and passages and nothing else, moved a thread
    # out of `plan` and left it there. A policy field no client may apply is a
    # field the API should not publish.
    assert all("approval_mode" not in row for row in rows)


def test_the_conversation_default_persists_and_is_validated(client):
    conversation = _conversation(client, "defaults")
    response = client.patch(
        f"/api/conversations/{conversation}/defaults",
        json={"default_preset": DEEP_RESEARCH},
    )
    assert response.status_code == 200
    assert response.json()["default_preset"] == DEEP_RESEARCH
    db = SessionLocal()
    try:
        assert db.get(Conversation, conversation).default_preset == DEEP_RESEARCH
    finally:
        db.close()
    # "auto" is a storable pick: the routing happens at send time.
    assert (
        client.patch(
            f"/api/conversations/{conversation}/defaults",
            json={"default_preset": AUTO},
        ).json()["default_preset"]
        == AUTO
    )
    # And "" clears it.
    assert (
        client.patch(
            f"/api/conversations/{conversation}/defaults", json={"default_preset": ""}
        ).json()["default_preset"]
        == ""
    )
    assert (
        client.patch(
            f"/api/conversations/{conversation}/defaults",
            json={"default_preset": "nonsense"},
        ).status_code
        == 422
    )
