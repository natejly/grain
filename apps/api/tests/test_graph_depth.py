"""Typed relations and multi-hop walks over the graph projection.

The projection is disposable, so every test here builds its own workspace and
tears it down: nothing in the graph is a system of record.
"""
from __future__ import annotations

import inspect
import json
import uuid

import pytest

from app.database import SessionLocal
from app.models import (
    Chunk,
    GraphEdge,
    GraphEntity,
    GraphProjection,
    MemoryItem,
    Source,
    User,
    Workspace,
)
from app.services import graph
from app.services.graph_tools import registry_tools
from app.services.llm_tools import ToolContext
from app.services.model import (
    GRAPH_RELATION_KINDS,
    normalize_relation,
    parse_graph_facts,
)

PASSAGE = (
    "Project Northstar is owned by Maya Chen at Atlas Labs. "
    "Maya Chen coordinates Project Northstar with Atlas Labs."
)


@pytest.fixture
def workspace(client):
    """A private workspace, so a rebuild here cannot disturb the demo graph."""
    workspace_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(Workspace(id=workspace_id, name="Graph depth"))
        db.add(User(id=user_id, email=f"{user_id}@example.com", name="Walker"))
        db.commit()
    finally:
        db.close()
    yield workspace_id, user_id
    db = SessionLocal()
    try:
        for model in (GraphEdge, GraphEntity, GraphProjection, Chunk, MemoryItem, Source):
            db.query(model).filter(model.workspace_id == workspace_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.query(Workspace).filter(Workspace.id == workspace_id).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _ingest(workspace_id: str, user_id: str, text: str) -> str:
    db = SessionLocal()
    try:
        source = Source(
            workspace_id=workspace_id,
            created_by=user_id,
            filename="walk.md",
            media_type="text/markdown",
            object_key="/test/walk.md",
            byte_size=len(text),
            status="ready",
            chunk_count=1,
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


def _entity(db, workspace_id: str, name: str) -> GraphEntity:
    entity = GraphEntity(
        workspace_id=workspace_id,
        name=name,
        normalized_name=graph._normalized(name),
        entity_type="concept",
        mention_count=1,
    )
    db.add(entity)
    db.flush()
    return entity


def _edge(db, workspace_id, left, right, relation="works_on", weight=1, confidence=0.9):
    edge = GraphEdge(
        workspace_id=workspace_id,
        from_entity_id=left.id,
        to_entity_id=right.id,
        relation=relation,
        weight=weight,
        confidence=confidence,
        source_ids_json=json.dumps(["source-1"]),
        chunk_ids_json=json.dumps(["chunk-1"]),
    )
    db.add(edge)
    db.flush()
    return edge


def _chain(db, workspace_id: str, names) -> dict:
    entities = {name: _entity(db, workspace_id, name) for name in names}
    for left, right in zip(names, names[1:], strict=False):
        _edge(db, workspace_id, entities[left], entities[right])
    db.commit()
    return entities


def _context(workspace_id: str, user_id: str) -> ToolContext:
    return ToolContext(
        workspace_id=workspace_id, user_id=user_id, conversation_id="none"
    )


# --------------------------------------------------------------------------
# Extraction


def test_rebuild_without_an_extractor_keeps_regex_entities_and_co_occurrence(
    workspace, db
):
    workspace_id, user_id = workspace
    source_id = _ingest(workspace_id, user_id, PASSAGE)
    graph.rebuild_graph(workspace_id, user_id)

    entities = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    names = {entity.name for entity in entities}
    assert "Maya Chen" in names and "Atlas Labs" in names
    assert all(source_id in json.loads(e.source_ids_json) for e in entities)

    edges = db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()
    assert edges, "co-occurrence remains the fallback with no typed relations"
    assert {edge.relation for edge in edges} == {graph.CO_OCCURRENCE_RELATION}
    assert all(edge.confidence == graph.CO_OCCURRENCE_CONFIDENCE for edge in edges)
    assert all(json.loads(edge.chunk_ids_json) for edge in edges)


def test_regex_backbone_drops_calendar_words_and_merges_articles(workspace, db):
    """Capitalization is the only evidence the regex pass has, so it is tested alone.

    'September'/'October' are time references, not things; 'The Atlas' and
    'Atlas' are one node; 'RFC' is not a company just because it is shouted.
    """
    workspace_id, user_id = workspace
    first = _ingest(
        workspace_id, user_id, "Atlas shipped in September. Priya Shah owns Atlas. RFC 14 applies."
    )
    second = _ingest(
        workspace_id,
        user_id,
        "The Atlas review happened in October. Priya Shah leads The Atlas. RFC 14 again.",
    )
    graph.rebuild_graph(workspace_id, user_id)

    entities = {
        entity.normalized_name: entity
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    assert "september" not in entities and "october" not in entities
    assert "the atlas" not in entities, "an article-prefixed duplicate is one node"
    atlas = entities["atlas"]
    assert atlas.name == "Atlas", "the bare spelling names the merged node"
    assert atlas.mention_count == 2
    # The merge has to carry the provenance of both spellings, not just one.
    assert set(json.loads(atlas.source_ids_json)) == {first, second}
    assert entities["rfc"].entity_type != "organization"

    assert graph.resolve_entity(db, workspace_id, "the atlas").entity is not None


def test_article_merge_needs_the_bare_name_to_exist(workspace, db):
    """'the guardian' the newspaper must not be folded into an absent 'guardian'."""
    workspace_id, user_id = workspace
    _ingest(workspace_id, user_id, "The Guardian reported it. The Guardian confirmed it.")
    graph.rebuild_graph(workspace_id, user_id)

    names = {
        entity.normalized_name
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    assert "the guardian" in names and "guardian" not in names


def test_the_merged_node_is_reachable_from_either_spelling(workspace, db):
    """The merge must not hide the node from the lookups that feed the model.

    resolve_entity was taught both spellings, but graph_lookup and the memory
    recall digest key on the normalized name directly, so a question about
    "The Atlas" reached nothing once the rebuild folded it into "Atlas".
    """
    from app.services.llm_tools import build_registry
    from app.services.memory import _graph_digest

    workspace_id, user_id = workspace
    _ingest(workspace_id, user_id, "Atlas shipped. Priya Shah owns Atlas.")
    _ingest(workspace_id, user_id, "The Atlas review ran. Priya Shah leads The Atlas.")
    graph.rebuild_graph(workspace_id, user_id)

    assert "the atlas" not in {
        entity.normalized_name
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    bare = _graph_digest(db, workspace_id, "What is Atlas?")
    assert bare, "sanity: the bare spelling reaches the node"
    assert _graph_digest(db, workspace_id, "What is The Atlas?") == bare

    lookup = build_registry(db, _context(workspace_id, user_id))["graph_lookup"]
    result = lookup.executor(db, _context(workspace_id, user_id), {"entity": "The Atlas"})
    assert "No graph entity" not in result.content
    assert json.loads(result.content)["entity"] == "Atlas"


def test_curated_and_typed_names_outrank_the_calendar_filter(workspace, db, monkeypatch):
    """The filter guesses from capitalization; better evidence overrules it.

    A person really called May, or a product really called Friday, is named by a
    human curating a memory or by an extractor that read the sentence and typed
    it. Only an unspecific kind ('event', 'concept') still loses to the filter.
    """
    workspace_id, user_id = workspace
    db.add(
        MemoryItem(
            workspace_id=workspace_id,
            kind="fact",
            content="May owns the Friday release train.",
            normalized_key="may owns the friday release train",
            entity_names_json=json.dumps(["May", "Friday", "Priya Shah"]),
            status="active",
        )
    )
    db.commit()
    graph.rebuild_graph(workspace_id, user_id)
    curated = {
        entity.normalized_name
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    assert {"may", "friday", "priya shah"} <= curated

    def fake_facts(text, *, user_id, settings=None):
        return {
            "entities": [
                {"name": "May", "type": "person"},
                {"name": "Friday", "type": "product"},
                {"name": "October", "type": "event"},
                {"name": "Tuesday", "type": "concept"},
            ],
            "relations": [
                {"from": "May", "to": "Friday", "relation": "created", "confidence": 0.9}
            ],
        }

    monkeypatch.setattr(graph, "extract_graph_facts", fake_facts)
    facts = graph.analyze_passage(
        "May built Friday in October, on a Tuesday.",
        user_id=user_id,
        settings=graph.get_settings(),
        use_llm=True,
    )
    assert {"may", "friday"} <= set(facts.displays)
    assert "october" not in facts.displays and "tuesday" not in facts.displays
    # The relation survives only because both endpoints did.
    assert facts.relations == [("may", "friday", "created", 0.9)]

    # The regex backbone, which has only capitalization to go on, still drops them.
    assert graph.extract_entities("Friday is here. May follows.") == []


def test_the_co_occurrence_bound_never_orphans_an_entity(workspace, db):
    """Thinning the weight-1 tail must not leave a graph with no edges at all.

    Interview notes and meeting minutes name five or more things per passage that
    never recur together, so *every* pair sits at weight 1 and the minimum-weight
    rule alone deletes the entire edge set.
    """
    workspace_id, user_id = workspace
    letters = "ABCDEFGHIJKLMNOPQRSTUVWX"
    for index in range(4):
        _ingest(
            workspace_id,
            user_id,
            ", ".join(
                f"Kestrel{letters[index * 6 + slot]} Group" for slot in range(6)
            )
            + " reviewed the migration plan.",
        )
    graph.rebuild_graph(workspace_id, user_id)

    entities = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    edges = db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()
    assert len(entities) == 24
    touched = {edge.from_entity_id for edge in edges} | {
        edge.to_entity_id for edge in edges
    }
    assert touched == {entity.id for entity in entities}, "no entity is orphaned"
    # Still far below the C(6,2) x 4 = 60 pairs the unbounded projection emitted.
    assert len(edges) < 30
    for edge in edges:
        assert json.loads(edge.chunk_ids_json), "reinstated edges keep provenance"

    first = entities[0]
    reached, _truncated = graph.neighbors(db, workspace_id, first)
    assert reached, "every entity is walkable"


def test_the_per_passage_cap_does_not_starve_the_end_of_the_alphabet(workspace, db):
    """A frequently named entity must not lose every edge to its first letter.

    The cap took the head of an alphabetically sorted list, so in a workspace
    whose passages each name more than MAX_ENTITIES_PER_CHUNK things, the same
    late-alphabet names were excluded from co-occurrence in *every* passage —
    'Zulu Corp' could be named in all of them and still have degree zero.
    """
    workspace_id, user_id = workspace
    names = [f"{letter}ulu Corp" for letter in "ABCDEFGHIJKLMNOP"]
    assert len(names) > graph.MAX_ENTITIES_PER_CHUNK
    for _ in range(3):
        _ingest(workspace_id, user_id, ", ".join(names) + " attended the review.")
    graph.rebuild_graph(workspace_id, user_id)

    entities = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    edges = db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()
    assert len(entities) == len(names)
    touched = {edge.from_entity_id for edge in edges} | {
        edge.to_entity_id for edge in edges
    }
    starved = sorted(
        entity.name for entity in entities if entity.id not in touched
    )
    assert starved == [], "every named entity earns at least one edge"
    # The bound still holds: nowhere near C(16, 2) x 3 pairings.
    assert len(edges) <= graph.MAX_ENTITIES_PER_CHUNK * len(names)
    last = next(entity for entity in entities if entity.name == "Pulu Corp")
    reached, _truncated = graph.neighbors(db, workspace_id, last)
    assert reached, "the last name in the passage is walkable"


def test_article_aliases_follow_a_chain_to_one_node():
    """'the a team' must land on 'team', not on the intermediate spelling."""
    assert graph._article_aliases(["the a team", "a team", "team"]) == {
        "the a team": "team",
        "a team": "team",
    }


def test_co_occurrence_needs_repetition_or_a_specific_passage(workspace, db):
    """One crowded passage must not emit C(n,2) meaningless edges."""
    workspace_id, user_id = workspace
    _ingest(
        workspace_id,
        user_id,
        "Alpha Corp, Beta Corp, Gamma Corp, Delta Corp, Epsilon Corp and Zeta Corp met.",
    )
    _ingest(workspace_id, user_id, "Alpha Corp works with Beta Corp.")
    graph.rebuild_graph(workspace_id, user_id)

    names = {
        entity.id: entity.name
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    assert len(names) == 6
    edges = db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()
    pairs = [(names[edge.from_entity_id], names[edge.to_entity_id]) for edge in edges]
    # 15 pairs from the crowded passage collapse to the one the second passage
    # repeats, plus the single reinstated pairing each remaining name needs to
    # stay reachable at all — never the combinatorial tail.
    assert ("Alpha Corp", "Beta Corp") in pairs
    repeated = [edge for edge in edges if edge.weight == 2]
    assert len(repeated) == 1
    assert len(pairs) == 5
    touched = {edge.from_entity_id for edge in edges} | {
        edge.to_entity_id for edge in edges
    }
    assert touched == set(names)


def test_rebuild_is_idempotent(workspace, db):
    workspace_id, user_id = workspace
    _ingest(workspace_id, user_id, PASSAGE)
    graph.rebuild_graph(workspace_id, user_id)
    first = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    version, entity_count, edge_count = first.version, first.entity_count, first.edge_count

    graph.rebuild_graph(workspace_id, user_id)
    db.expire_all()
    second = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    assert (second.version, second.entity_count, second.edge_count) == (
        version,
        entity_count,
        edge_count,
    )
    assert db.query(GraphEntity).filter_by(workspace_id=workspace_id).count() == entity_count


def test_llm_pass_adds_lowercase_entities_and_typed_relations(workspace, db, monkeypatch):
    workspace_id, user_id = workspace
    _ingest(
        workspace_id,
        user_id,
        "Maya Chen runs the onboarding rewrite on kubernetes at Atlas Labs.",
    )

    def fake_facts(text, *, user_id, settings=None):
        return {
            "entities": [
                {"name": "Maya Chen", "type": "person"},
                {"name": "the onboarding rewrite", "type": "project"},
                {"name": "kubernetes", "type": "product"},
            ],
            "relations": [
                {
                    "from": "Maya Chen",
                    "to": "the onboarding rewrite",
                    "relation": "works_on",
                    "confidence": 0.9,
                },
                {
                    "from": "the onboarding rewrite",
                    "to": "kubernetes",
                    "relation": "uses",
                    "confidence": 0.6,
                },
            ],
        }

    monkeypatch.setattr(graph, "extract_graph_facts", fake_facts)
    monkeypatch.setattr(
        graph, "get_settings", lambda: _settings_with_provider("openai")
    )
    graph.rebuild_graph(workspace_id, user_id)

    entities = {
        entity.name: entity
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    # A lowercase name the capitalization regex could never see.
    assert "kubernetes" in entities
    assert entities["kubernetes"].entity_type == "product"
    assert entities["Maya Chen"].entity_type == "person"

    edges = db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()
    typed = {edge.relation for edge in edges}
    assert "works_on" in typed and "uses" in typed
    works_on = next(edge for edge in edges if edge.relation == "works_on")
    assert works_on.confidence == pytest.approx(0.9)
    assert json.loads(works_on.chunk_ids_json), "typed edges keep passage provenance"

    named_pairs = {
        frozenset((edge.from_entity_id, edge.to_entity_id))
        for edge in edges
        if edge.relation != graph.CO_OCCURRENCE_RELATION
    }
    duplicated = [
        edge
        for edge in edges
        if edge.relation == graph.CO_OCCURRENCE_RELATION
        and frozenset((edge.from_entity_id, edge.to_entity_id)) in named_pairs
    ]
    assert not duplicated, "a named relation replaces its co-occurrence twin"


def test_extraction_failure_falls_back_to_the_regex_path(workspace, db, monkeypatch):
    workspace_id, user_id = workspace
    _ingest(workspace_id, user_id, PASSAGE)

    def exploding(text, *, user_id, settings=None):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(graph, "extract_graph_facts", exploding)
    monkeypatch.setattr(
        graph, "get_settings", lambda: _settings_with_provider("openai")
    )
    graph.rebuild_graph(workspace_id, user_id)

    projection = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    assert projection.status == "ready", "an outage must not fail the rebuild"
    names = {
        entity.name
        for entity in db.query(GraphEntity).filter_by(workspace_id=workspace_id)
    }
    assert "Maya Chen" in names


def _settings_with_provider(provider: str):
    from app.config import get_settings

    return get_settings().model_copy(update={"model_provider": provider})


def test_analyze_passage_keeps_the_vocabulary_closed(monkeypatch):
    """Kinds are re-checked where they become stored columns, not only on parse."""

    def rogue(text, *, user_id, settings=None):
        return {
            "entities": [
                {"name": "Atlas Labs", "type": "starship"},
                {"name": "the", "type": "concept"},
            ],
            "relations": [
                {
                    "from": "Atlas Labs",
                    "to": "Atlas Labs",
                    "relation": "owns",
                    "confidence": 1,
                },
                {
                    "from": "Atlas Labs",
                    "to": "Ghost Corp",
                    "relation": "owns",
                    "confidence": 1,
                },
            ],
        }

    monkeypatch.setattr(graph, "extract_graph_facts", rogue)
    facts = graph.analyze_passage(
        "Atlas Labs ships things.",
        user_id="u",
        settings=_settings_with_provider("openai"),
        use_llm=True,
    )
    assert facts.types["atlas labs"] == "concept"
    assert "the" not in facts.trusted, "stop words never become entities"
    assert facts.relations == [], "self and undeclared endpoints are dropped"


def test_relation_vocabulary_normalises_instead_of_flattening():
    """A plural or a tense should not cost a relation its name."""
    assert normalize_relation("creates") == "created"
    assert normalize_relation("depends on") == "depends_on"
    assert normalize_relation("acquires") == "acquired"
    assert normalize_relation("reporting to") == "reports_to"
    assert normalize_relation("BELONGS_TO") == "part_of"
    # Direction-reversing forms are not silently flipped onto the forward term.
    assert normalize_relation("created by") == "related_to"
    assert normalize_relation("owned by") == "related_to"
    # And the set stays closed: nothing invented reaches the column.
    for raw in ("assassinates", "", "???", "reticulates_splines"):
        assert normalize_relation(raw) == "related_to"
    assert {normalize_relation(term) for term in GRAPH_RELATION_KINDS} == set(
        GRAPH_RELATION_KINDS
    )


def test_a_named_relation_replaces_related_to_for_the_same_pair(
    workspace, db, monkeypatch
):
    workspace_id, user_id = workspace
    _ingest(workspace_id, user_id, "Atlas Labs and Juniper Systems, again and again.")

    def fake_facts(text, *, user_id, settings=None):
        return {
            "entities": [
                {"name": "Atlas Labs", "type": "organization"},
                {"name": "Juniper Systems", "type": "organization"},
            ],
            "relations": [
                {"from": "Atlas Labs", "to": "Juniper Systems", "relation": "acquires",
                 "confidence": 0.9},
                {"from": "Atlas Labs", "to": "Juniper Systems", "relation": "mentions",
                 "confidence": 0.2},
            ],
        }

    monkeypatch.setattr(graph, "extract_graph_facts", fake_facts)
    monkeypatch.setattr(graph, "get_settings", lambda: _settings_with_provider("openai"))
    graph.rebuild_graph(workspace_id, user_id)

    relations = [
        edge.relation
        for edge in db.query(GraphEdge).filter_by(workspace_id=workspace_id)
    ]
    assert relations == ["acquired"], "the null relation adds nothing to a named pair"


def test_parse_graph_facts_rejects_untrusted_shapes():
    parsed = parse_graph_facts(
        json.dumps(
            {
                "entities": [
                    {"name": "Atlas Labs", "type": "organization"},
                    {"name": "kubernetes", "type": "spaceship"},
                    "not-an-object",
                ],
                "relations": [
                    {"from": "Atlas Labs", "to": "kubernetes", "relation": "owns"},
                    {"from": "Atlas Labs", "to": "Nowhere Inc", "relation": "owns"},
                    {"from": "Atlas Labs", "to": "Atlas Labs", "relation": "owns"},
                    {
                        "from": "kubernetes",
                        "to": "Atlas Labs",
                        "relation": "assassinates",
                        "confidence": 7,
                    },
                ],
            }
        )
    )
    assert [item["type"] for item in parsed["entities"]] == ["organization", "concept"]
    # Endpoints the response never declared, and self-relations, are dropped.
    assert len(parsed["relations"]) == 2
    assert parsed["relations"][0]["confidence"] == 0.5
    assert parsed["relations"][1]["relation"] == "related_to"
    assert parsed["relations"][1]["confidence"] == 1.0
    assert parse_graph_facts("not json at all") == {"entities": [], "relations": []}


# --------------------------------------------------------------------------
# Walking


def test_neighbors_reach_two_hops_by_default(workspace, db):
    workspace_id, user_id = workspace
    entities = _chain(db, workspace_id, ["Alpha", "Beta", "Gamma", "Delta"])
    found, truncated = graph.neighbors(db, workspace_id, entities["Alpha"])
    by_name = {item.name: item for item in found}
    assert set(by_name) == {"Beta", "Gamma"}
    assert by_name["Beta"].distance == 1 and by_name["Gamma"].distance == 2
    assert by_name["Beta"].relation == "works_on"
    assert by_name["Gamma"].via == "Beta"
    assert truncated is False

    deeper, _ = graph.neighbors(db, workspace_id, entities["Alpha"], max_hops=3)
    assert {item.name for item in deeper} == {"Beta", "Gamma", "Delta"}


def test_neighbors_bound_a_hub(workspace, db):
    workspace_id, user_id = workspace
    hub = _entity(db, workspace_id, "Hub")
    for index in range(40):
        _edge(db, workspace_id, hub, _entity(db, workspace_id, f"Spoke {index}"))
    db.commit()

    found, truncated = graph.neighbors(db, workspace_id, hub, limit=5)
    assert len(found) == 5
    assert truncated is True

    capped, _ = graph.neighbors(db, workspace_id, hub, limit=10_000)
    assert len(capped) <= graph.MAX_NEIGHBOR_RESULTS


def test_shortest_path_reports_relations_and_no_path(workspace, db):
    workspace_id, user_id = workspace
    entities = _chain(db, workspace_id, ["Alpha", "Beta", "Gamma"])
    island = _entity(db, workspace_id, "Island")
    db.commit()

    path = graph.shortest_path(db, workspace_id, entities["Alpha"], entities["Gamma"])
    assert path is not None
    assert [(step.from_name, step.relation, step.to_name) for step in path] == [
        ("Alpha", "works_on", "Beta"),
        ("Beta", "works_on", "Gamma"),
    ]
    assert path[0].chunk_ids == ["chunk-1"], "a path step keeps its provenance"

    assert graph.shortest_path(db, workspace_id, entities["Alpha"], island) is None
    assert graph.shortest_path(
        db, workspace_id, entities["Alpha"], entities["Gamma"], max_hops=1
    ) is None
    assert graph.shortest_path(db, workspace_id, island, island) == []


def test_shortest_path_ignores_edge_direction(workspace, db):
    workspace_id, user_id = workspace
    left = _entity(db, workspace_id, "Left")
    middle = _entity(db, workspace_id, "Middle")
    right = _entity(db, workspace_id, "Right")
    _edge(db, workspace_id, middle, left, relation="owns")
    _edge(db, workspace_id, middle, right, relation="owns")
    db.commit()

    path = graph.shortest_path(db, workspace_id, left, right)
    assert path is not None and len(path) == 2
    # Steps stay in their stored direction so "owns" still reads correctly.
    assert path[0].from_name == "Middle" and path[0].to_name == "Left"


# --------------------------------------------------------------------------
# Tools


def test_walk_tools_are_read_only(workspace, db):
    workspace_id, user_id = workspace
    tools = registry_tools(db, _context(workspace_id, user_id))
    assert set(tools) == {"graph_neighbors", "graph_path"}
    assert all(spec.read_only and spec.preview is None for spec in tools.values())


def test_graph_neighbors_tool_bounds_output(workspace, db):
    workspace_id, user_id = workspace
    _chain(db, workspace_id, ["Alpha", "Beta", "Gamma", "Delta"])
    tools = registry_tools(db, _context(workspace_id, user_id))
    result = tools["graph_neighbors"].executor(
        db, _context(workspace_id, user_id), {"entity": "alpha", "hops": 99}
    )
    payload = json.loads(result.content)
    assert payload["hops"] == graph.MAX_NEIGHBOR_HOPS
    assert {item["name"] for item in payload["neighbors"]} == {"Beta", "Gamma", "Delta"}
    assert len(result.bounded_content()) <= 4000


def test_graph_tools_report_unknown_entities(workspace, db):
    workspace_id, user_id = workspace
    _chain(db, workspace_id, ["Alpha", "Beta"])
    context = _context(workspace_id, user_id)
    tools = registry_tools(db, context)

    unknown = tools["graph_neighbors"].executor(db, context, {"entity": "Zeta"})
    assert "No graph entity" in unknown.content
    suggested = tools["graph_neighbors"].executor(db, context, {"entity": "lph"})
    assert "Alpha" in suggested.content
    assert "Error" in tools["graph_neighbors"].executor(db, context, {}).content
    assert "Error" in tools["graph_path"].executor(db, context, {"from_entity": "Alpha"}).content


def test_graph_path_tool_answers_both_ways(workspace, db):
    workspace_id, user_id = workspace
    entities = _chain(db, workspace_id, ["Alpha", "Beta", "Gamma"])
    _entity(db, workspace_id, "Island")
    db.commit()
    context = _context(workspace_id, user_id)
    tools = registry_tools(db, context)

    found = json.loads(
        tools["graph_path"]
        .executor(db, context, {"from_entity": "Alpha", "to_entity": "Gamma"})
        .content
    )
    assert found["found"] is True and found["hops"] == 2
    assert found["path"][0]["relation"] == "works_on"
    assert found["path"][0]["chunk_ids"] == ["chunk-1"]

    missing = json.loads(
        tools["graph_path"]
        .executor(db, context, {"from_entity": "Alpha", "to_entity": "Island"})
        .content
    )
    assert missing["found"] is False and "No path" in missing["reason"]
    assert entities["Alpha"].workspace_id == workspace_id


def test_walks_are_workspace_scoped(workspace, db, client):
    workspace_id, user_id = workspace
    _chain(db, workspace_id, ["Alpha", "Beta"])
    other = client.get("/api/bootstrap").json()["identity"]["workspace_id"]
    context = _context(other, user_id)
    result = registry_tools(db, context)["graph_neighbors"].executor(
        db, context, {"entity": "Alpha"}
    )
    assert "No graph entity" in result.content


def test_walk_tools_reach_the_agent_registry(workspace, db):
    """A ToolSpec nobody registers is a tool the agent can never call."""
    workspace_id, user_id = workspace
    from app.services.llm_tools import build_registry

    registry = build_registry(db, _context(workspace_id, user_id))
    assert {"graph_neighbors", "graph_path"} <= set(registry)


def test_tool_bounds_survive_non_finite_arguments(workspace, db):
    """JSON allows Infinity/NaN, and int() raises OverflowError on the former."""
    workspace_id, user_id = workspace
    _chain(db, workspace_id, ["Alpha", "Beta"])
    context = _context(workspace_id, user_id)
    tools = registry_tools(db, context)

    payload = json.loads(
        tools["graph_neighbors"]
        .executor(db, context, {"entity": "Alpha", "hops": float("inf")})
        .content
    )
    assert payload["hops"] == graph.DEFAULT_NEIGHBOR_HOPS
    walked = json.loads(
        tools["graph_path"]
        .executor(
            db,
            context,
            {"from_entity": "Alpha", "to_entity": "Beta", "max_hops": float("nan")},
        )
        .content
    )
    assert walked["found"] is True


# --------------------------------------------------------------------------
# Rebuild robustness


def _memory(
    workspace_id: str, key: str, content: str, names, *, status: str = "active"
) -> str:
    db = SessionLocal()
    try:
        item = MemoryItem(
            workspace_id=workspace_id,
            normalized_key=key,
            kind="fact",
            content=content,
            entity_names_json=json.dumps(names),
            status=status,
        )
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def test_memory_beyond_the_entity_cap_still_rebuilds(workspace, db):
    """memory_items carry up to 16 curated names; the per-item cap is 12."""
    workspace_id, user_id = workspace
    _memory(
        workspace_id,
        "over-cap",
        "nothing capitalized in this sentence",
        [f"curated {index}" for index in range(16)],
    )
    graph.rebuild_graph(workspace_id, user_id)

    projection = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    assert projection.status == "ready", projection.error
    entities = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    assert entities, "curated names still populate the graph"
    assert len(entities) <= graph.MAX_ENTITIES_PER_CHUNK
    assert all(json.loads(entity.memory_ids_json) for entity in entities)


def test_entity_names_fit_the_column(workspace, db):
    """String(200) is enforced by PostgreSQL even though SQLite ignores it."""
    workspace_id, user_id = workspace
    blob = "A" + "b" * 9_999
    _ingest(workspace_id, user_id, f"{blob} is used by Atlas Labs. {blob} again.")
    _memory(workspace_id, "long", "no capitals here", ["C" + "d" * 9_999])
    graph.rebuild_graph(workspace_id, user_id)

    projection = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    assert projection.status == "ready", projection.error
    entities = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    assert entities
    for entity in entities:
        assert len(entity.name) <= graph.MAX_ENTITY_NAME_CHARS
        assert len(entity.normalized_name) <= graph.MAX_ENTITY_NAME_CHARS


def test_version_ignores_memory_row_order(workspace, db):
    """`version` hashes inputs, so physical row order must not change it."""
    workspace_id, user_id = workspace
    first = ("k1", "Maya Chen leads Atlas Labs.")
    second = ("k2", "Atlas Labs funds Project Northstar.")

    ids = {}
    session = SessionLocal()
    try:
        session.query(MemoryItem).filter_by(workspace_id=workspace_id).delete()
        session.commit()
    finally:
        session.close()
    for key, content in (first, second):
        ids[key] = _memory(workspace_id, key, content, [])

    # Re-insert the same rows, same ids, in the opposite physical order.
    def reinsert(order) -> str:
        session = SessionLocal()
        try:
            session.query(MemoryItem).filter_by(workspace_id=workspace_id).delete()
            session.commit()
            for key, content in order:
                session.add(
                    MemoryItem(
                        id=ids[key],
                        workspace_id=workspace_id,
                        normalized_key=key,
                        kind="fact",
                        content=content,
                        entity_names_json="[]",
                        status="active",
                    )
                )
                session.commit()
        finally:
            session.close()
        graph.rebuild_graph(workspace_id, user_id)
        session = SessionLocal()
        try:
            return session.query(GraphProjection).filter_by(
                workspace_id=workspace_id
            ).one().version
        finally:
            session.close()

    assert reinsert([first, second]) == reinsert([second, first])


def test_lookup_keeps_calendar_names_the_projection_filters():
    """The calendar guess belongs to the write path, not to questions.

    Dropping "Friday" when deciding whether to *create* a node is defensible —
    capitalization is the only evidence. Dropping it from a *question* can only
    lose a match against a node that already exists, which is what made
    "Tell me about Friday" return an empty digest while the Friday node was
    sitting in the graph.
    """
    from app.services.graph import extract_entities

    asked = dict(extract_entities("Tell me about Friday", drop_calendar=False))
    assert "friday" in asked

    asked_may = dict(extract_entities("Who is May?", drop_calendar=False))
    assert "may" in asked_may

    # The projection side must still refuse them, or the noise comes back.
    projected = dict(extract_entities("Shipped in October and September"))
    assert "october" not in projected
    assert "september" not in projected


def test_graph_digest_finds_a_calendar_named_entity(workspace, db):
    """End to end: the digest that feeds the model's context must reach the node.

    The digest describes relations, so the node needs an edge to say anything —
    an isolated entity legitimately yields nothing.
    """
    from app.services.memory import _graph_digest

    workspace_id, _user_id = workspace
    friday = GraphEntity(
        workspace_id=workspace_id,
        name="Friday",
        normalized_name="friday",
        entity_type="concept",
        mention_count=3,
    )
    release = GraphEntity(
        workspace_id=workspace_id,
        name="Release Train",
        normalized_name="release train",
        entity_type="project",
        mention_count=4,
    )
    db.add_all([friday, release])
    db.flush()
    db.add(
        GraphEdge(
            workspace_id=workspace_id,
            from_entity_id=friday.id,
            to_entity_id=release.id,
            relation="co_occurs",
            weight=3,
        )
    )
    db.commit()

    digest = _graph_digest(db, workspace_id, "Tell me about Friday")
    assert digest, "the Friday node exists; the question must reach it"
    assert any("Friday" in line for line in digest)


# --------------------------------------------------------------------------
# Memory as a projection input
#
# The graph has two authoritative inputs, not one, and a workspace that has
# uploaded nothing is the case where that distinction is the whole feature: it
# is entirely normal for someone to have taught the workspace fourteen things
# about themselves and indexed zero documents. These tests hold the line that
# such a workspace still gets a graph, that a corrected fact does not keep its
# old node, and that a reader can tell the two kinds of support apart.


def _snapshot(db, workspace_id: str):
    """Everything the projection asserts, in a form two rebuilds can compare."""
    entities = {
        row.id: row
        for row in db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    }
    nodes = sorted(
        (
            row.normalized_name,
            row.entity_type,
            row.mention_count,
            tuple(sorted(json.loads(row.memory_ids_json))),
            tuple(sorted(json.loads(row.chunk_ids_json))),
        )
        for row in entities.values()
    )
    edges = sorted(
        (
            entities[row.from_entity_id].normalized_name,
            entities[row.to_entity_id].normalized_name,
            row.relation,
            row.weight,
        )
        for row in db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()
    )
    return nodes, edges


def test_a_workspace_with_memories_and_no_sources_still_has_a_graph(workspace, db):
    """The user's reported case: 14 memories, 0 sources, an empty graph.

    Nothing here is a document, so every node the rebuild produces has to come
    from a memory or there is no graph at all.
    """
    workspace_id, user_id = workspace
    _memory(
        workspace_id,
        "who",
        "Nathaniel Ly studies Computer Science at Yale.",
        ["Nathaniel Ly", "Yale"],
    )
    _memory(
        workspace_id,
        "work",
        "Nathaniel Ly interned at Capital One.",
        ["Nathaniel Ly", "Capital One"],
    )
    graph.rebuild_graph(workspace_id, user_id)

    projection = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    assert projection.status == "ready", projection.error
    entities = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    assert entities, "memories alone must be able to fill the graph"
    assert {"nathaniel ly", "yale", "capital one"} <= {
        row.normalized_name for row in entities
    }
    assert db.query(GraphEdge).filter_by(workspace_id=workspace_id).all()


def test_memory_support_is_distinguishable_from_document_support(workspace, db):
    """ADR 0002 keeps provenance on every node; the *kind* of it is the claim.

    "You told me this" and "this document says this" are different standings for
    the same name, and a reader clicking through has nothing else to tell them
    apart — so the two id lists are populated independently, and a node with only
    one kind of support carries only that kind. This is also what the entity row
    reads to say "from memory".
    """
    workspace_id, user_id = workspace
    _ingest(workspace_id, user_id, "Helios Freight ships with Borealis Rail.")
    _memory(workspace_id, "told", "Quintus Vale mentors me.", ["Quintus Vale"])
    graph.rebuild_graph(workspace_id, user_id)

    by_name = {
        row.normalized_name: row
        for row in db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    }
    remembered = by_name["quintus vale"]
    assert json.loads(remembered.memory_ids_json), "a remembered name cites its memory"
    assert json.loads(remembered.chunk_ids_json) == []
    assert json.loads(remembered.source_ids_json) == []

    quoted = by_name["helios freight"]
    assert json.loads(quoted.chunk_ids_json), "a quoted name cites its passage"
    assert json.loads(quoted.source_ids_json)
    assert json.loads(quoted.memory_ids_json) == []


def test_a_superseded_memory_contributes_nothing(workspace, db):
    """A corrected fact must not keep the node its old value put there.

    This is the failure the memory-supersession work fixed, and the projection is
    the back door into it: `Fly.io` retired hours ago, and a graph that still
    shows it — with an edge to the API it no longer hosts — is a worse answer
    than no graph, because it looks current.
    """
    workspace_id, user_id = workspace
    _memory(workspace_id, "live", "Deploys go to Zephyr Rail.", ["Zephyr Rail"])
    _memory(
        workspace_id,
        "old",
        "Deploys go to Umbral Heights.",
        ["Umbral Heights"],
        status="superseded",
    )
    _memory(
        workspace_id,
        "gone",
        "Deploys go to Quaggan Freight.",
        ["Quaggan Freight"],
        status="deleted",
    )
    graph.rebuild_graph(workspace_id, user_id)

    names = {
        row.normalized_name
        for row in db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
    }
    assert "zephyr rail" in names, "the current value is projected"
    assert "umbral heights" not in names, "a retired claim keeps no node"
    assert "quaggan freight" not in names, "a forgotten claim keeps no node"

    retired = {
        row.id
        for row in db.query(MemoryItem)
        .filter(MemoryItem.workspace_id == workspace_id, MemoryItem.status != "active")
        .all()
    }
    cited = set()
    for row in db.query(GraphEntity).filter_by(workspace_id=workspace_id).all():
        cited.update(json.loads(row.memory_ids_json))
    for row in db.query(GraphEdge).filter_by(workspace_id=workspace_id).all():
        cited.update(json.loads(row.memory_ids_json))
    # Not just "no node of its own": a retired row must not turn up as
    # provenance for a name some live memory also happens to mention.
    assert cited.isdisjoint(retired)


def test_graph_takes_memory_liveness_from_memorys_own_chokepoint(workspace, db):
    """The rule above has to be the *same* rule recall uses, not a copy of it.

    test_memory_depth.py pins `_active()` as the single place a query decides
    what counts as a live memory, on the grounds that a second copy is a second
    thing to forget. The projection is a reader of memory like any other, so it
    goes through the same function rather than spelling the predicate out again.
    """
    source = inspect.getsource(graph.rebuild_graph)
    assert "_active(" in source
    assert "MemoryItem.status" not in source, (
        "rebuild_graph decides memory liveness for itself instead of via _active()"
    )


def test_rebuilding_a_memory_only_graph_twice_changes_nothing(workspace, db):
    """ADR 0002: the projection may be dropped and rebuilt at any time."""
    workspace_id, user_id = workspace
    _memory(
        workspace_id,
        "one",
        "Maya Chen leads Atlas Labs.",
        ["Maya Chen", "Atlas Labs"],
    )
    _memory(
        workspace_id,
        "two",
        "Atlas Labs funds Project Northstar.",
        ["Atlas Labs", "Project Northstar"],
    )
    graph.rebuild_graph(workspace_id, user_id)
    projection = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    first_version = projection.version
    first = _snapshot(db, workspace_id)
    assert first[0], "nothing is proven by two empty rebuilds agreeing"

    # From scratch, not incrementally: the rows are dropped the way an operator
    # clearing the projection would drop them.
    db.query(GraphEdge).filter_by(workspace_id=workspace_id).delete()
    db.query(GraphEntity).filter_by(workspace_id=workspace_id).delete()
    db.commit()
    graph.rebuild_graph(workspace_id, user_id)
    db.expire_all()

    projection = db.query(GraphProjection).filter_by(workspace_id=workspace_id).one()
    assert projection.version == first_version
    assert _snapshot(db, workspace_id) == first


def test_memory_derived_entities_stay_inside_their_workspace(workspace, db):
    """Two tenants, one table. A rebuild reads and writes only its own rows."""
    workspace_id, user_id = workspace
    other_id = str(uuid.uuid4())
    session = SessionLocal()
    try:
        session.add(Workspace(id=other_id, name="Other tenant"))
        session.commit()
    finally:
        session.close()
    try:
        _memory(workspace_id, "mine", "Sable Ridge is ours.", ["Sable Ridge"])
        _memory(other_id, "theirs", "Cobalt Vale is theirs.", ["Cobalt Vale"])
        graph.rebuild_graph(workspace_id, user_id)

        mine = db.query(GraphEntity).filter_by(workspace_id=workspace_id).all()
        assert {row.normalized_name for row in mine} >= {"sable ridge"}
        assert "cobalt vale" not in {row.normalized_name for row in mine}
        # The other tenant's rebuild never ran, so its memories projected nothing
        # anywhere — not into its own graph, and not into ours.
        assert db.query(GraphEntity).filter_by(workspace_id=other_id).all() == []
    finally:
        session = SessionLocal()
        try:
            for model in (GraphEdge, GraphEntity, GraphProjection, MemoryItem):
                session.query(model).filter(model.workspace_id == other_id).delete()
            session.query(Workspace).filter(Workspace.id == other_id).delete()
            session.commit()
        finally:
            session.close()
