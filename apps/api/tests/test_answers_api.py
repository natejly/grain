"""The grounded-answer API: the door, the scope, the filter, and the receipt.

The claims worth pinning, in the order a reviewer would ask them:

- it is a BEARER door. No token, a revoked token and a cookie-only caller are
  the same uniform 401, before any tenant lookup;
- a valid token answers from its OWN workspace's sources, with citations and a
  `grounding` block, and leaves exactly one receipt;
- `space_id` scopes retrieval, and `source_ids` is an allow-list that DROPS an
  id this workspace does not own rather than 404ing it — an allow-list must not
  double as an existence probe;
- the receipts are owner-read and workspace-filtered: another tenant's owner
  gets a 404 that is indistinguishable from a receipt that never existed;
- the whole call is metered under one operation, asserted at the model call
  itself rather than by looking for a `ModelUsage` row — the scripted provider
  reports no usage, so asserting a row would be asserting the double.
"""
from __future__ import annotations

import uuid

import pytest
from conftest import TEST_BASE_URL, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    ApiToken,
    Chunk,
    GroundedReceipt,
    Membership,
    Source,
    Space,
    User,
)
from app.services import grounded, usage

NORTHSTAR = (
    "The Northstar project launches in October. Maya Chen owns the launch. "
    "Its goal is to reduce customer onboarding time by forty percent."
)
JUNIPER = "Project Juniper uses a violet deployment ring and ships on Thursdays."


def make_member(workspace_id: str) -> str:
    """A second, non-owner member of an existing workspace. Returns their id."""
    db = SessionLocal()
    try:
        member = User(email=f"member-{uuid.uuid4().hex[:10]}@example.com", name="M")
        db.add(member)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=member.id, role="member"))
        db.commit()
        return member.id
    finally:
        db.close()


def key() -> dict[str, str]:
    return {"Idempotency-Key": "answers-" + uuid.uuid4().hex}


@pytest.fixture
def tenant():
    identity = create_identity(name="Answers owner", workspace_name="Answers workspace")
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    return client, identity


def mint(client: TestClient) -> dict:
    response = client.post("/api/api-tokens", headers=key(), json={"name": "CI"})
    assert response.status_code == 201, response.text
    return response.json()


def machine(secret: str) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.headers["Authorization"] = f"Bearer {secret}"
    return client


def seed(workspace_id: str, user_id: str, text: str, *, filename: str, space_id: str = "") -> str:
    """One ready source of one chunk. Returns the source id.

    Nothing indexes it: `search_evidence` reconciles the term index at search
    time, which is the same path a freshly ingested file takes.
    """
    db = SessionLocal()
    try:
        source = Source(
            workspace_id=workspace_id,
            created_by=user_id,
            filename=filename,
            media_type="text/markdown",
            object_key="/tmp/not-used-" + uuid.uuid4().hex,
            byte_size=len(text),
            status="ready",
            chunk_count=1,
            space_id=space_id,
        )
        db.add(source)
        db.flush()
        db.add(
            Chunk(
                workspace_id=workspace_id,
                source_id=source.id,
                ordinal=0,
                content=text,
                char_start=0,
                char_end=len(text),
                token_count=len(text.split()),
            )
        )
        db.commit()
        return source.id
    finally:
        db.close()


def ask(bearer: TestClient, **body):
    body.setdefault("question", "Who owns the Northstar launch?")
    return bearer.post("/api/answers/grounded", json=body)


# -- the door ---------------------------------------------------------------


def test_no_bearer_is_401():
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    assert anonymous.post("/api/answers/grounded", json={"question": "hi"}).status_code == 401


def test_a_revoked_token_is_the_same_401(tenant):
    client, _identity = tenant
    token = mint(client)
    assert client.delete(f"/api/api-tokens/{token['id']}").status_code in (200, 204)
    assert ask(machine(token["secret"])).status_code == 401


def test_a_cookie_only_caller_is_401(tenant):
    """The cookie door is not this door. `get_token_actor` looks at one header."""
    client, _identity = tenant
    assert client.post("/api/answers/grounded", json={"question": "hi"}).status_code == 401


# -- the answer -------------------------------------------------------------


def test_a_valid_token_gets_an_answer_its_citations_and_a_grounding_block(tenant):
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)

    response = ask(machine(token["secret"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"]
    assert body["citations"]
    assert body["citations"][0]["filename"] == "northstar.md"
    # `url` is None for an indexed passage and the field must survive the
    # response model — the same property the transcript's citations have.
    assert "url" in body["citations"][0]
    assert body["report"]["grounding"] is not None
    assert body["report"]["grounding"]["floor"] > 0
    assert body["receipt_id"]

    db = SessionLocal()
    try:
        receipt = db.get(GroundedReceipt, body["receipt_id"])
        assert receipt is not None
        assert receipt.workspace_id == identity.workspace_id
        assert receipt.answer == body["answer"]
        # The credential that made the call, so "revoke the token that did
        # this" is answerable.
        assert receipt.token_id == token["id"]
    finally:
        db.close()


def test_an_off_corpus_question_is_answered_from_whatever_ranked_highest(tenant):
    """Documents the shipped posture rather than an aspiration.

    `retrieval_fused_floor` defaults to 0.0 — it admits everything — so a
    question this corpus does not cover still comes back with the top-ranked
    passage attached. That is the floor's default, not this route's choice, and
    it is exactly what `config.retrieval_fused_floor` is flagged for re-baseline
    against the answerability gate. What this route adds is the honest part: the
    `grounding` block is computed over the passages that were actually handed
    over, so a caller can tell a supported answer from a decorated one.
    """
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)

    body = ask(machine(token["secret"]), question="What is the tallest mountain?").json()
    assert body["report"]["evidence_count"] == len(body["citations"])
    assert body["report"]["grounding"] is not None


def test_space_id_scopes_retrieval(tenant):
    client, identity = tenant
    db = SessionLocal()
    try:
        space = Space(
            workspace_id=identity.workspace_id,
            name="Launches",
            created_by=identity.user_id,
        )
        db.add(space)
        db.commit()
        space_id = space.id
    finally:
        db.close()
    seed(
        identity.workspace_id,
        identity.user_id,
        JUNIPER,
        filename="juniper.md",
        space_id=space_id,
    )
    token = mint(client)
    bearer = machine(token["secret"])

    inside = ask(bearer, question="violet deployment ring", space_id=space_id).json()
    assert [item["filename"] for item in inside["citations"]] == ["juniper.md"]

    # A different space cannot see it, and the global scope is not a space.
    other = ask(
        bearer,
        question="violet deployment ring",
        space_id="00000000-0000-4000-8000-0000000000ff",
    ).json()
    assert other["citations"] == []


def test_an_unknown_source_id_is_dropped_rather_than_refused(tenant):
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)
    bearer = machine(token["secret"])

    response = ask(bearer, source_ids=["00000000-0000-4000-8000-0000000000ff"])
    # 200, not 404: a foreign or missing id must not be confirmable through an
    # allow-list. It simply allows nothing, which is what the caller asked for.
    assert response.status_code == 200
    assert response.json()["citations"] == []


def test_a_known_source_id_narrows_the_answer(tenant):
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    juniper_id = seed(identity.workspace_id, identity.user_id, JUNIPER, filename="juniper.md")
    token = mint(client)

    body = ask(
        machine(token["secret"]),
        question="Which project ships on Thursdays with a violet ring?",
        source_ids=[juniper_id],
    ).json()
    assert [item["filename"] for item in body["citations"]] == ["juniper.md"]


def test_a_json_schema_fills_structured_or_says_why_it_could_not(tenant):
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)

    body = ask(
        machine(token["secret"]),
        json_schema={"type": "object", "properties": {"owner": {"type": "string"}}},
    ).json()
    # The scripted double answers in prose, so this asserts the honest half of
    # the contract: exactly one of the two fields is filled, never neither.
    assert bool(body["structured"]) != bool(body["schema_error"])
    assert body["answer"]


def test_the_call_is_metered_under_one_operation(tenant, monkeypatch):
    """Asserted at the model call, not by looking for a ledger row.

    The scripted provider reports no usage at all, so `record_model_usage`
    writes nothing — asserting a `ModelUsage` row here would be asserting the
    double rather than the scope.
    """
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)
    seen = {}

    original = grounded.model.answer_from_passages

    def capture(*args, **kwargs):
        seen["attribution"] = usage.current_attribution()
        return original(*args, **kwargs)

    monkeypatch.setattr(grounded.model, "answer_from_passages", capture)
    assert ask(machine(token["secret"])).status_code == 200

    attribution = seen["attribution"]
    assert attribution.operation == usage.GROUNDED_ANSWER
    assert attribution.workspace_id == identity.workspace_id
    assert attribution.user_id == identity.user_id


# -- the receipts -----------------------------------------------------------


def test_the_owner_reads_the_ledger_and_one_receipt(tenant):
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)
    receipt_id = ask(machine(token["secret"])).json()["receipt_id"]

    listing = client.get("/api/answers/receipts")
    assert listing.status_code == 200
    rows = listing.json()
    assert [row["id"] for row in rows] == [receipt_id]
    # The list is a ledger, not a transcript: no answer body rides it.
    assert "answer" not in rows[0]

    detail = client.get(f"/api/answers/receipts/{receipt_id}")
    assert detail.status_code == 200
    assert detail.json()["answer"]
    assert detail.json()["report"]["grounding"] is not None


def test_another_tenants_owner_gets_a_404(tenant):
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)
    receipt_id = ask(machine(token["secret"])).json()["receipt_id"]

    stranger = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        create_identity(name="Stranger", workspace_name="Stranger workspace"),
    )
    assert stranger.get(f"/api/answers/receipts/{receipt_id}").status_code == 404
    assert stranger.get("/api/answers/receipts").json() == []


def test_a_member_cannot_read_the_ledger(tenant):
    """Deliberate scope: the receipts sit beside the tokens, which are
    owner-minted, on an owner-gated surface. A member who USES the API cannot
    see their own receipts — a choice, not an oversight."""
    from conftest import Identity, issue_session

    _client, identity = tenant
    member_id = make_member(identity.workspace_id)
    session_token, csrf = issue_session(member_id)
    member = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=member_id,
            workspace_id=identity.workspace_id,
            token=session_token,
            csrf_token=csrf,
        ),
    )
    assert member.get("/api/answers/receipts").status_code == 403


def test_the_token_workspace_is_the_only_corpus_it_can_reach(tenant):
    client, identity = tenant
    stranger = create_identity(name="Other", workspace_name="Other workspace")
    seed(stranger.workspace_id, stranger.user_id, JUNIPER, filename="juniper.md")
    token = mint(client)

    body = ask(machine(token["secret"]), question="violet deployment ring").json()
    assert body["citations"] == []

    db = SessionLocal()
    try:
        rows = list(
            db.scalars(
                select(ApiToken).where(ApiToken.workspace_id == identity.workspace_id)
            )
        )
        assert [row.id for row in rows] == [token["id"]]
    finally:
        db.close()


# -- the allow-list ranks INSIDE itself --------------------------------------


def test_an_allow_listed_source_survives_a_crowded_corpus(tenant):
    """The filter has to be part of the query, not a sieve over the top-k.

    With the allow-list applied after ranking, a named source that loses the
    global ranking race is dropped — and the response says `citations: []`,
    `evidence_count: 0`, `grounding_score: 0.0`, byte-identical to a corpus
    that holds no answer. A caller who names the one document that answers
    their question is told nothing answers it, and a receipt records the lie.

    The decoys are written to out-rank the handbook on the query's own words,
    which is exactly the situation a `source_ids` caller is in: they name the
    document BECAUSE the ranking does not find it first.
    """
    client, identity = tenant
    question = "Which project ships on Thursdays with a violet deployment ring?"
    for index in range(60):
        seed(
            identity.workspace_id,
            identity.user_id,
            "Which project ships on Thursdays with a violet deployment ring? "
            "Thursdays, violet deployment ring, ships, project — decoy "
            f"{index} repeats the violet deployment ring and Thursdays again.",
            filename=f"decoy-{index}.md",
        )
    handbook = seed(
        identity.workspace_id,
        identity.user_id,
        "Juniper leaves the station each Thursday under a violet ring.",
        filename="handbook.md",
    )
    token = mint(client)

    body = ask(
        machine(token["secret"]),
        question=question,
        source_ids=[handbook],
        limit=5,
    ).json()

    assert [item["filename"] for item in body["citations"]] == ["handbook.md"]
    assert body["report"]["evidence_count"] == 1


def test_an_allow_list_that_resolves_to_nothing_answers_from_nothing(tenant):
    """The empty-set subtlety of pushing the filter down: `SourceFilter` reads
    an empty tuple as "no narrowing", so collapsing "none of these ids exist
    here" into "no filter" would answer from the whole library — a scope
    widening out of an allow-list the caller wrote to narrow."""
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)

    body = ask(
        machine(token["secret"]),
        source_ids=["00000000-0000-4000-8000-0000000000ff"],
    ).json()
    assert body["citations"] == []
    assert body["report"]["evidence_count"] == 0


# -- what a citation and a receipt row carry ---------------------------------


def test_a_citation_carries_the_fingerprint_of_what_it_quoted(tenant):
    """Emitted by `runs._citations`, stored in `citations_json`, and declared
    in the TS client — but undeclared on the response model, so FastAPI
    stripped it from every read and the re-verification story it exists for
    was unreachable through the API."""
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)

    body = ask(machine(token["secret"])).json()
    assert body["citations"], body
    assert body["citations"][0]["fingerprint"]

    receipt = client.get(f"/api/answers/receipts/{body['receipt_id']}").json()
    assert receipt["citations"][0]["fingerprint"] == body["citations"][0]["fingerprint"]


def test_a_receipt_row_says_how_much_was_gradable(tenant):
    """`scored == 0` is "nothing here made a checkable claim", which the schema
    says must not be rendered as 0%. The row carried only the score, so the
    ledger had no field left to tell that apart from "graded and failed" — and
    it rendered both as "0% grounded" while the drawer below it, reading the
    same verdict, rendered nothing at all."""
    client, identity = tenant
    seed(identity.workspace_id, identity.user_id, NORTHSTAR, filename="northstar.md")
    token = mint(client)
    receipt_id = ask(machine(token["secret"])).json()["receipt_id"]

    row = next(
        row
        for row in client.get("/api/answers/receipts").json()
        if row["id"] == receipt_id
    )
    detail = client.get(f"/api/answers/receipts/{receipt_id}").json()
    assert row["scored"] == detail["report"]["grounding"]["scored"]
