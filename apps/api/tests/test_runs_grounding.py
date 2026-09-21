"""The repair seam: one attempt, accepted only when it is strictly better.

`_repair_unsupported` is the one place in this repo where a validator is
allowed to change the thing it validates, so every branch is pinned here rather
than left to the happy path:

* it runs at most once, and exactly one `run.citations` event is appended in
  every branch — a second event would make "how often was an answer repaired" a
  question with two answers;
* that event lands BEFORE `run.completed`, which stream consumers treat as
  terminal;
* `messages.citation_report_json` equals the event payload byte for byte, so
  the stored verdict is the verdict on the stored text;
* a rewrite that scores lower, or that invents an `[n]`, is discarded with its
  reason recorded — a "repair" that made an answer worse is the one failure
  mode that would make this feature negative value.

`model.regenerate_unsupported` is monkeypatched throughout. It has no offline
double on purpose (see its docstring), so this is the only way to exercise it,
and the eval gate cannot observe it at all.
"""
from __future__ import annotations

import json

import pytest
from conftest import TEST_BASE_URL, authenticate, create_identity
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Message, Run, RunEvent
from app.services import followups as followups_service
from app.services import model, runs, usage
from app.services.retrieval import Evidence

PASSAGE = (
    "The Northstar project launches in October. Maya Chen owns the launch. "
    "Its goal is to reduce customer onboarding time by forty percent."
)

SUPPORTED = "Maya Chen owns the Northstar launch [1]."

#: Two sentences: one supported, one with a planted wrong name at high overlap.
FLAWED = SUPPORTED + " Ravi Deshpande approved the entire Northstar launch [1]."
#: The same answer with the flagged sentence corrected against the passage.
FIXED = SUPPORTED + " Maya Chen also approved the Northstar launch in October [1]."
#: Strictly worse: both sentences now unsupported.
WORSE = (
    "Ravi Deshpande owns the Northstar launch [1]. "
    "Ravi Deshpande approved the entire Northstar launch [1]."
)
#: Better prose, invented citation.
FABRICATED = SUPPORTED + " Maya Chen approved the Northstar launch in October [3]."


@pytest.fixture
def owner():
    identity = create_identity(name="Grounding owner", workspace_name="Grounding workspace")
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), identity), identity


@pytest.fixture
def run(owner) -> str:
    client, identity = owner
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "grounding-" + identity.user_id[:8]},
        json={"title": "Grounding"},
    ).json()
    db = SessionLocal()
    try:
        row = Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity.user_id,
            status="running",
            prompt="Who owns Northstar?",
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


@pytest.fixture
def openai_settings(monkeypatch):
    """A Settings that claims the openai provider, without a provider behind it.

    `regenerate_unsupported` is monkeypatched in every test that uses this, so
    nothing reaches a network; what the override buys is the `!= "openai"`
    early return in the repair path not firing.
    """
    settings = get_settings().model_copy(update={"model_provider": "openai"})
    monkeypatch.setattr(runs, "get_settings", lambda: settings)
    return settings


def evidence() -> list[Evidence]:
    return [
        Evidence(
            chunk_id="chunk-northstar",
            source_id="source-northstar",
            filename="northstar.md",
            ordinal=0,
            excerpt=PASSAGE,
            score=0.9,
        )
    ]


def events(run_id: str) -> list[str]:
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


def citation_payload(run_id: str) -> dict:
    db = SessionLocal()
    try:
        row = (
            db.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.event_type == "run.citations")
            .one()
        )
        return json.loads(row.payload_json)
    finally:
        db.close()


def stored_message(run_id: str) -> Message:
    db = SessionLocal()
    try:
        return (
            db.query(Message)
            .filter(Message.run_id == run_id, Message.role == "assistant")
            .one()
        )
    finally:
        db.close()


def finish(run_id: str, answer: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(Run, run_id)
        assert row is not None
        db.expunge(row)
    finally:
        db.close()
    runs._finish_run(row, answer=answer, evidence=evidence(), already_streamed=True)


def test_a_better_rewrite_is_applied_and_recorded(run, openai_settings, monkeypatch):
    monkeypatch.setattr(model, "regenerate_unsupported", lambda *a, **k: FIXED)
    finish(run, FLAWED)

    message = stored_message(run)
    assert message.content == FIXED
    payload = citation_payload(run)
    assert payload["repair"]["applied"] is True
    assert payload["repair"]["reason"] == ""
    assert payload["repair"]["unsupported_before"] == 1
    assert payload["repair"]["unsupported_after"] == 0
    # The verdict stored on the message is the verdict on the text that was
    # stored — not on the draft that was replaced.
    assert payload["grounding"]["cited_unsupported"] == 0
    assert json.loads(message.citation_report_json) == payload


def test_a_worse_rewrite_is_discarded(run, openai_settings, monkeypatch):
    monkeypatch.setattr(model, "regenerate_unsupported", lambda *a, **k: WORSE)
    finish(run, FLAWED)

    assert stored_message(run).content == FLAWED
    repair = citation_payload(run)["repair"]
    assert repair["attempted"] is True
    assert repair["applied"] is False
    assert repair["reason"] == "rejected_not_better"


def test_a_rewrite_that_fabricates_a_citation_is_discarded(run, openai_settings, monkeypatch):
    monkeypatch.setattr(model, "regenerate_unsupported", lambda *a, **k: FABRICATED)
    finish(run, FLAWED)

    assert stored_message(run).content == FLAWED
    repair = citation_payload(run)["repair"]
    assert repair["applied"] is False
    assert repair["reason"] == "rejected_fabricated"


def test_a_raising_rewrite_keeps_the_original_answer(run, openai_settings, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(model, "regenerate_unsupported", boom)
    finish(run, FLAWED)

    assert stored_message(run).content == FLAWED
    repair = citation_payload(run)["repair"]
    assert repair["attempted"] is True
    assert repair["applied"] is False
    assert repair["reason"] == "failed"


def test_the_scripted_provider_reports_no_provider_rather_than_a_repair(run):
    """The honest offline outcome: the stage did not run, and says so."""
    finish(run, FLAWED)

    assert stored_message(run).content == FLAWED
    repair = citation_payload(run)["repair"]
    assert repair["attempted"] is False
    assert repair["applied"] is False
    assert repair["reason"] == "no_provider"


def test_a_clean_answer_attempts_nothing(run, openai_settings, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a clean answer must not be rewritten")

    monkeypatch.setattr(model, "regenerate_unsupported", never)
    finish(run, SUPPORTED)

    repair = citation_payload(run)["repair"]
    assert repair["attempted"] is False
    assert repair["reason"] == "nothing_to_repair"
    # "Nothing to repair" here means the whole answer, and says so: the grader
    # stops at 120 sentences and the repair only ever sees what it graded, so
    # on a longer answer this flag is what separates "clean" from "clean as far
    # as anyone looked".
    assert repair["tail_ungraded"] is False


def test_repair_off_is_a_supported_posture(run, monkeypatch):
    settings = get_settings().model_copy(
        update={"model_provider": "openai", "grounding_repair_enabled": False}
    )
    monkeypatch.setattr(runs, "get_settings", lambda: settings)
    monkeypatch.setattr(
        model,
        "regenerate_unsupported",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    finish(run, FLAWED)

    assert stored_message(run).content == FLAWED
    assert citation_payload(run)["repair"]["reason"] == "disabled"


def test_exactly_one_citation_event_lands_before_completion(run, openai_settings, monkeypatch):
    monkeypatch.setattr(model, "regenerate_unsupported", lambda *a, **k: FIXED)
    finish(run, FLAWED)

    names = events(run)
    assert names.count("run.citations") == 1
    assert names[-1] == "run.completed"
    assert names.index("run.citations") < names.index("run.completed")


# --- a rewrite that keeps only the front of the answer ------------------------

#: Six cited sentences over one passage, two of them unsupported: the ordinary
#: shape the repair pass exists for, long enough that dropping the tail is a
#: visible loss rather than an edit.
LONG = " ".join(
    [
        "Maya Chen owns the Northstar launch [1].",
        "The Northstar project launches in October [1].",
        "Its goal is to reduce customer onboarding time by forty percent [1].",
        "Ravi Deshpande approved the entire Northstar launch [1].",
        "The launch budget was raised to nine million dollars in July [1].",
        "Onboarding time is measured from the first signup email [1].",
    ]
)


def test_a_rewrite_that_drops_the_tail_of_the_answer_is_discarded(
    run, openai_settings, monkeypatch
):
    """The failure the acceptance test could not see.

    Cutting an answer short removes its unsupported sentences along with
    everything else, so a truncation scores HIGHER, reports FEWER unsupported
    sentences, and validates cleanly — it passes every condition the repair had.
    `message.completed` then replaces the whole streamed answer with the
    fragment, and the user watches two thirds of what they just read disappear.
    """
    head = " ".join(LONG.split(". ")[:2]) + "."
    monkeypatch.setattr(model, "regenerate_unsupported", lambda *a, **k: head)
    finish(run, LONG)

    assert stored_message(run).content == LONG
    repair = citation_payload(run)["repair"]
    assert repair["attempted"] is True
    assert repair["applied"] is False
    assert repair["reason"] == "rejected_truncated"


def test_a_rewrite_may_still_drop_the_sentences_it_was_asked_to_fix(
    run, openai_settings, monkeypatch
):
    """The other side of the same guard: the repair's own instructions allow it
    to DROP a claim the passages do not support, so the yardstick is the
    unflagged material, not the original length."""
    kept = [
        sentence
        for sentence in LONG.split(". ")
        if "Ravi Deshpande" not in sentence and "nine million" not in sentence
    ]
    trimmed = ". ".join(part.rstrip(".") for part in kept) + "."
    monkeypatch.setattr(model, "regenerate_unsupported", lambda *a, **k: trimmed)
    finish(run, LONG)

    repair = citation_payload(run)["repair"]
    assert repair["applied"] is True, repair
    assert stored_message(run).content == trimmed


# --- what the audit trail may hold -------------------------------------------


def test_the_audit_row_carries_counts_and_no_sentence_text(run):
    """`GET /api/audit-events` is open to every workspace member and exports
    off-box; a non-shared thread is visible only to its creator. The composed
    verdict now embeds every graded sentence's own text, so passing it straight
    to `record_audit` would publish one member's private answers into a feed
    the whole workspace reads. The event and the message column keep the array.
    """
    finish(run, FLAWED)

    db = SessionLocal()
    try:
        row = (
            db.query(AuditEvent)
            .filter(
                AuditEvent.resource_id == run,
                AuditEvent.action == "run.citations_validated",
            )
            .one()
        )
        detail = json.loads(row.detail_json or "{}")
    finally:
        db.close()

    assert "sentences" not in detail.get("grounding", {})
    assert "Ravi Deshpande" not in json.dumps(detail)
    # Still a verdict, not a blank: the counts are what a triager reads.
    assert detail["grounding"]["scored"] == 2
    assert detail["grounding"]["cited_unsupported"] == 1
    assert detail["valid"] is True
    assert detail["evidence_count"] == 1
    # The thread-scoped copies are untouched.
    assert citation_payload(run)["grounding"]["sentences"]


# --- attributing what the follow-up polish spends ----------------------------


def test_follow_up_polish_runs_inside_a_usage_scope(run, monkeypatch):
    """`_finish_run` runs after the agent loop's ContextVar scope is unwound,
    so a model call made here without its own bind spends tokens that
    `record_model_usage` drops with a warning — invisible to the ledger and to
    the spend ceiling, which aggregates the same table. The repair path carries
    this bind and a comment saying why; the follow-up path did not.
    """
    seen: list[str] = []

    def capture(db, items, **kwargs):
        seen.append(usage.current_attribution().workspace_id)
        seen.append(usage.current_attribution().operation)
        return items

    monkeypatch.setattr(followups_service, "polish", capture)
    finish(run, SUPPORTED)

    db = SessionLocal()
    try:
        row = db.get(Run, run)
        workspace_id = row.workspace_id if row is not None else ""
    finally:
        db.close()
    assert seen and seen[0] == workspace_id
    assert seen[1] == usage.FOLLOWUP_POLISH
