from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Chunk, GraphEdge, GraphEntity, GraphProjection, MemoryItem, Source
from .audit import record_audit
from .model import GRAPH_ENTITY_KINDS, extract_graph_facts, normalize_relation
from .usage import usage_scope

ENTITY_PATTERN = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&'’-]{1,})(?:[ \t]+(?:[A-Z][A-Za-z0-9&'’-]{1,})){0,3}\b"
    r"|\b[A-Z]{2,8}\b"
)
IGNORED_ENTITIES = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "it",
    "of",
    "on",
    "or",
    "our",
    "the",
    "this",
    "to",
    "we",
    "with",
}
ORGANIZATION_SUFFIXES = {"inc", "labs", "lab", "llc", "ltd", "company", "corp", "team"}
# A capitalized month or weekday is a time reference, not a thing the graph can
# usefully connect: "September" links every passage written that month to every
# other, which is the densest possible edge with the least possible meaning. Only
# a name that is *entirely* one of these words is dropped, so "September Group"
# and "Friday Harbor" still become entities.
CALENDAR_WORDS = frozenset(
    {
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
        "oct", "nov", "dec",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri",
        "sat", "sun",
        "today", "tomorrow", "yesterday", "tonight",
        "q1", "q2", "q3", "q4",
    }
)
# Stripped only when the bare form is a name the workspace uses on its own — see
# _article_aliases.
LEADING_ARTICLES = ("the ", "a ", "an ")
MAX_ENTITIES_PER_CHUNK = 12
MAX_PROVENANCE_IDS = 100
# GraphEntity.name and .normalized_name are String(200). SQLite ignores the
# declared width, but PostgreSQL is authoritative here and would reject a longer
# value, failing the whole workspace rebuild over one base64 blob in one chunk.
# The extractor caps its own names at the same width.
MAX_ENTITY_NAME_CHARS = 200

CO_OCCURRENCE_RELATION = "co_occurs"
# Two names sharing a passage is evidence of association, not of a stated relation.
# A fixed low score keeps extracted relations sorted above the structural fallback
# without pretending co-occurrence carries the extractor's certainty.
CO_OCCURRENCE_CONFIDENCE = 0.25
# Co-occurrence is O(n^2) in the entities of a passage, so a passage naming a
# dozen things emits 66 weight-1 pairs that assert nothing. Requiring a pair to
# recur across passages keeps the association that repetition actually evidences
# and drops the combinatorial tail.
MIN_CO_OCCURRENCE_WEIGHT = 2
# ...except from a passage that named only a handful of things, where a single
# pairing is already specific. Without this exception a workspace holding one
# document would project entities and no edges at all.
MAX_SPECIFIC_PASSAGE_ENTITIES = 4
# Ceiling on extraction calls per rebuild. A rebuild walks the whole workspace, so
# the projection must stay cheap enough to run on every ingest; passages beyond the
# budget still land in the graph through the regex extractor.
MAX_LLM_PASSAGES_PER_REBUILD = 60

# Walk bounds. A hub entity can carry hundreds of edges, so every expansion is
# capped before it reaches the model's context.
DEFAULT_NEIGHBOR_HOPS = 2
MAX_NEIGHBOR_HOPS = 3
DEFAULT_NEIGHBOR_RESULTS = 25
MAX_NEIGHBOR_RESULTS = 50
MAX_EDGES_PER_EXPANSION = 200
DEFAULT_PATH_HOPS = 4
MAX_PATH_HOPS = 6
MAX_PATH_VISITED = 400


def _normalized(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip(" .,:;()[]{}").casefold()


def _is_noise_name(normalized: str, *, drop_calendar: bool = True) -> bool:
    """Names that can never identify a thing worth a node.

    The calendar branch is a guess made on capitalization alone, which cannot
    tell the month from the surname. It is therefore only applied where
    capitalization is the *only* evidence — the regex backbone. A name an
    extractor read in context and typed as a person, or a name a human curated
    onto a memory, carries better evidence than this heuristic and keeps it.
    """
    return (
        len(normalized) < 2
        or normalized in IGNORED_ENTITIES
        or (drop_calendar and normalized in CALENDAR_WORDS)
    )


# Kinds that assert what a name *is*. An extractor returning one of these for
# "May" or "Friday" has resolved the ambiguity the calendar filter exists to
# guess at; "concept"/"event" is the default it falls back to and is not
# evidence, so those still lose to the filter.
SPECIFIC_ENTITY_KINDS = frozenset(
    {"person", "organization", "project", "place", "product"}
)


def name_candidates(normalized: str) -> List[str]:
    """Spellings that may name the same node after an article merge.

    rebuild_graph folds 'the atlas' into 'atlas' when the workspace uses both, so
    every lookup keyed on a normalized name — tool resolution, the memory recall
    digest — has to try both spellings or it silently misses the node that
    survived the merge.
    """
    candidates = [normalized]
    for article in LEADING_ARTICLES:
        if normalized.startswith(article):
            candidates.append(normalized[len(article) :])
            return candidates
    candidates.append("the " + normalized)
    return candidates


def _entity_type(name: str) -> str:
    """A conservative guess when no extractor typed the entity.

    Casing used to decide this: any ALL-CAPS name was called an organization,
    which typed 'RFC', 'TODO' and 'API' as companies. An acronym's referent is
    exactly what casing cannot tell us, so the guess is gone and only structural
    evidence — a corporate suffix, a "Project X" prefix — still types a name.
    """
    words = {_normalized(part) for part in name.split()}
    if words.intersection(ORGANIZATION_SUFFIXES):
        return "organization"
    if name.lower().startswith("project "):
        return "project"
    if len(name.split()) >= 2:
        return "named_entity"
    return "concept"


def _article_aliases(names: Iterable[str]) -> Dict[str, str]:
    """Map 'the atlas' onto 'atlas', but only inside a workspace that says both.

    Stripping articles unconditionally would merge 'the guardian' (the paper)
    into any unrelated 'guardian'. Requiring the bare form to appear on its own
    somewhere in this workspace makes the merge evidence-based: the corpus itself
    demonstrates that the two spellings are used interchangeably.
    """
    known = {name for name in names if not _is_noise_name(name, drop_calendar=False)}
    aliases: Dict[str, str] = {}
    for name in known:
        for article in LEADING_ARTICLES:
            if not name.startswith(article):
                continue
            bare = name[len(article) :]
            if bare in known:
                aliases[name] = bare
            break
    # Follow chains ('the a team' -> 'a team' -> 'team') to their end, or the fold
    # would leave the middle spelling behind as a third node nothing points at.
    for name in list(aliases):
        seen = {name}
        target = aliases[name]
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        aliases[name] = target
    return aliases


def _record_display(
    display_names: Dict[str, str], normalized: str, display: str
) -> None:
    """Keep the spelling that matches the node's own normalized name.

    After an article merge the node is keyed on 'atlas', so 'Atlas' must win over
    'The Atlas' whichever passage arrived first.
    """
    current = display_names.get(normalized)
    if current is None or (
        _normalized(current) != normalized and _normalized(display) == normalized
    ):
        display_names[normalized] = display


def extract_entities(
    text: str, *, drop_calendar: bool = True
) -> List[Tuple[str, str]]:
    """Candidate entity names in `text`.

    `drop_calendar=False` is for *lookup* rather than projection. Discarding
    "May" and "Friday" is a defensible guess when the only evidence is
    capitalization and the result is a new node. It is the wrong call when the
    text is a question being matched against nodes that already exist: a node is
    only there because it survived this same filter or a human curated it, so
    filtering the question can lose a match but can never invent one. Applying
    it to both sides is what made "Tell me about Friday" return an empty digest
    while the Friday node sat in the graph.
    """
    found: Dict[str, str] = {}
    for match in ENTITY_PATTERN.finditer(text):
        display = re.sub(r"\s+", " ", match.group(0)).strip()[:MAX_ENTITY_NAME_CHARS]
        normalized = _normalized(display)
        if _is_noise_name(normalized, drop_calendar=drop_calendar):
            continue
        if display[0].isupper() and match.start() == 0 and " " not in display:
            # A sentence-leading common word is weak evidence until it repeats elsewhere.
            display = display.strip()
        found.setdefault(normalized, display)
    return [(normalized, display) for normalized, display in found.items()]


@dataclass(frozen=True)
class PassageFacts:
    """What one passage contributes to the projection.

    `displays` is every candidate name; `trusted` is the subset an extractor
    asserted deliberately, which is accepted without the repeat-mention test the
    regex candidates have to pass.
    """

    displays: Dict[str, str] = field(default_factory=dict)
    types: Dict[str, str] = field(default_factory=dict)
    trusted: Set[str] = field(default_factory=set)
    relations: List[Tuple[str, str, str, float]] = field(default_factory=list)

    def folded(self, aliases: Dict[str, str]) -> PassageFacts:
        """The same facts with every name rewritten to its canonical node."""
        if not aliases:
            return self
        displays: Dict[str, str] = {}
        for normalized, display in self.displays.items():
            _record_display(displays, aliases.get(normalized, normalized), display)
        relations: List[Tuple[str, str, str, float]] = []
        for left, right, relation, confidence in self.relations:
            canonical_left = aliases.get(left, left)
            canonical_right = aliases.get(right, right)
            # A merge can collapse both ends onto one node, which would otherwise
            # store a relation from an entity to itself.
            if canonical_left != canonical_right:
                relations.append(
                    (canonical_left, canonical_right, relation, confidence)
                )
        return PassageFacts(
            displays=displays,
            types={
                aliases.get(normalized, normalized): kind
                for normalized, kind in self.types.items()
            },
            trusted={aliases.get(name, name) for name in self.trusted},
            relations=relations,
        )


def analyze_passage(
    text: str,
    *,
    user_id: str,
    settings: Settings,
    use_llm: bool,
) -> PassageFacts:
    """Entities and typed relations for one passage.

    The regex pass always runs: it is the candidate generator the LLM pass types
    and trusts, not a fallback. The LLM pass adds the names capitalization cannot
    see (lowercase products, multi-word efforts) and the typed relations
    co-occurrence can only guess at. `use_llm` is the per-rebuild budget, not a
    mode: passages past the budget keep the regex candidates alone.
    """
    displays: Dict[str, str] = {}
    for normalized, display in extract_entities(text):
        displays.setdefault(normalized, display)
    if not use_llm:
        return PassageFacts(displays=displays)
    try:
        extracted = extract_graph_facts(text, user_id=user_id, settings=settings)
    except Exception:
        # The projection is disposable and rebuilt on demand, so a provider
        # outage degrades to the regex extractor rather than failing the rebuild.
        return PassageFacts(displays=displays)

    types: Dict[str, str] = {}
    trusted: Set[str] = set()
    relations: List[Tuple[str, str, str, float]] = []
    for item in extracted["entities"]:
        display = str(item.get("name") or "").strip()
        normalized = _normalized(display)
        kind = str(item.get("type") or "")
        # The vocabulary is re-checked here, at the boundary where a kind becomes
        # a stored column, not only where the response was parsed.
        kind = kind if kind in GRAPH_ENTITY_KINDS else "concept"
        # The instructions tell the extractor to skip bare calendar words, and
        # instructions are not a guarantee — but an extractor that read the
        # sentence and called "May" a person outranks a filter that only saw the
        # capital M. Only an unspecific kind still loses to the filter.
        if not normalized or _is_noise_name(
            normalized, drop_calendar=kind not in SPECIFIC_ENTITY_KINDS
        ):
            continue
        # The extractor saw the sentence, so its casing beats the regex guess.
        displays[normalized] = display
        types[normalized] = kind
        trusted.add(normalized)
    for item in extracted["relations"]:
        left = _normalized(str(item.get("from") or ""))
        right = _normalized(str(item.get("to") or ""))
        if left == right or left not in trusted or right not in trusted:
            continue
        score = item.get("confidence", 0.0)
        relations.append(
            (
                left,
                right,
                # Normalised, not merely membership-checked: the vocabulary is
                # closed either way, but "creates" should land on "created"
                # rather than on the null relation.
                normalize_relation(str(item.get("relation") or "")),
                float(score) if isinstance(score, (int, float)) else 0.0,
            )
        )
    return PassageFacts(
        displays=displays, types=types, trusted=trusted, relations=relations
    )


def _projection(db: Session, workspace_id: str) -> GraphProjection:
    projection = db.scalar(
        select(GraphProjection).where(GraphProjection.workspace_id == workspace_id)
    )
    if projection is None:
        projection = GraphProjection(workspace_id=workspace_id)
        db.add(projection)
        db.flush()
    return projection


def mark_graph_stale(db: Session, workspace_id: str) -> None:
    projection = _projection(db, workspace_id)
    if projection.status != "building":
        projection.status = "stale"


def _bounded(values: Iterable[str]) -> List[str]:
    return sorted(set(values))[:MAX_PROVENANCE_IDS]


RelationKey = Tuple[str, str, str]


def rebuild_graph(workspace_id: str, actor_id: str) -> None:
    db = SessionLocal()
    try:
        settings = get_settings()
        llm_budget = MAX_LLM_PASSAGES_PER_REBUILD

        projection = _projection(db, workspace_id)
        projection.status = "building"
        projection.error = ""
        db.commit()

        rows = db.execute(
            select(Chunk, Source)
            .join(Source, Source.id == Chunk.source_id)
            .where(
                Chunk.workspace_id == workspace_id,
                Source.workspace_id == workspace_id,
                Source.deleted_at.is_(None),
                Source.status == "ready",
            )
            .order_by(Source.id, Chunk.ordinal)
        ).all()

        mention_count: Counter[str] = Counter()
        display_names: Dict[str, str] = {}
        entity_types: Dict[str, str] = {}
        entity_sources: Dict[str, Set[str]] = defaultdict(set)
        entity_chunks: Dict[str, Set[str]] = defaultdict(set)
        entity_memories: Dict[str, Set[str]] = defaultdict(set)
        chunk_entities: List[Tuple[Chunk, List[str]]] = []
        extracted_accepted: Set[str] = set()
        relation_weight: Counter[RelationKey] = Counter()
        relation_confidence: Dict[RelationKey, float] = {}
        relation_sources: Dict[RelationKey, Set[str]] = defaultdict(set)
        relation_chunks: Dict[RelationKey, Set[str]] = defaultdict(set)
        relation_memories: Dict[RelationKey, Set[str]] = defaultdict(set)
        version_hasher = hashlib.sha256()
        # The extraction mode is an input to the projection, so a provider change
        # has to produce a new version rather than silently reusing the old one.
        version_hasher.update(("provider:" + settings.active_model_provider).encode())

        def record_relations(
            facts: PassageFacts,
            *,
            source_id: str = "",
            chunk_id: str = "",
            memory_id: str = "",
        ) -> None:
            for left, right, relation, confidence in facts.relations:
                key = (left, right, relation)
                relation_weight[key] += 1
                # Max, not mean: the strongest assertion of a relation is what the
                # reader cares about, and repetition never weakens it.
                relation_confidence[key] = max(
                    relation_confidence.get(key, 0.0), confidence
                )
                if source_id:
                    relation_sources[key].add(source_id)
                if chunk_id:
                    relation_chunks[key].add(chunk_id)
                if memory_id:
                    relation_memories[key].add(memory_id)

        # Extraction is gathered before anything is accumulated: an article merge
        # ('the atlas' into 'atlas') can only be decided once the whole
        # workspace's vocabulary is known, and every counter below must already
        # be keyed on the merged node.
        chunk_facts: List[Tuple[Chunk, Source, PassageFacts]] = []
        # One rebuild is up to MAX_LLM_PASSAGES_PER_REBUILD extraction calls with
        # no user waiting on them, which is exactly the shape of spend that goes
        # unnoticed until it is on an invoice.
        with usage_scope(workspace_id=workspace_id, user_id=actor_id):
            for chunk, source in rows:
                version_hasher.update(chunk.id.encode())
                version_hasher.update(hashlib.sha256(chunk.content.encode()).digest())
                chunk_facts.append(
                    (
                        chunk,
                        source,
                        analyze_passage(
                            chunk.content,
                            user_id=actor_id,
                            settings=settings,
                            use_llm=llm_budget > 0,
                        ),
                    )
                )
                llm_budget = max(0, llm_budget - 1)

        memory_items = list(
            db.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.workspace_id == workspace_id,
                    MemoryItem.status == "active",
                )
                # Unordered rows would make `version` a hash of physical row order
                # and hand the LLM budget to a different subset on every rebuild.
                .order_by(MemoryItem.id)
            )
        )
        memory_facts: List[Tuple[MemoryItem, PassageFacts, List[Tuple[str, str]]]] = []
        for item in memory_items:
            version_hasher.update(item.id.encode())
            version_hasher.update(hashlib.sha256(item.content.encode()).digest())
            with usage_scope(workspace_id=workspace_id, user_id=actor_id):
                facts = analyze_passage(
                    item.content,
                    user_id=actor_id,
                    settings=settings,
                    use_llm=llm_budget > 0,
                )
            llm_budget = max(0, llm_budget - 1)
            curated_raw = (
                str(raw)[:MAX_ENTITY_NAME_CHARS]
                for raw in json.loads(item.entity_names_json)
            )
            curated = [
                (normalized, display)
                for normalized, display in (
                    (_normalized(name), name) for name in curated_raw
                )
                # A curated name is the memory writer's own claim about what this
                # note is about; a human who typed "May" meant their colleague.
                if not _is_noise_name(normalized, drop_calendar=False)
            ]
            memory_facts.append((item, facts, curated))

        aliases = _article_aliases(
            itertools.chain(
                (name for _chunk, _source, facts in chunk_facts for name in facts.displays),
                (name for _item, facts, _curated in memory_facts for name in facts.displays),
                (
                    name
                    for _item, _facts, curated in memory_facts
                    for name, _display in curated
                ),
            )
        )

        for chunk, source, raw_facts in chunk_facts:
            facts = raw_facts.folded(aliases)
            normalized_names = list(facts.displays)
            for normalized, display in facts.displays.items():
                mention_count[normalized] += 1
                _record_display(display_names, normalized, display)
                if normalized not in entity_types and facts.types.get(normalized):
                    entity_types[normalized] = facts.types[normalized]
                entity_sources[normalized].add(source.id)
                entity_chunks[normalized].add(chunk.id)
            extracted_accepted.update(facts.trusted)
            record_relations(facts, source_id=source.id, chunk_id=chunk.id)
            chunk_entities.append((chunk, normalized_names))

        memory_entities: List[Tuple[MemoryItem, List[str]]] = []
        memory_accepted: Set[str] = set()
        for item, raw_facts, raw_curated in memory_facts:
            facts = raw_facts.folded(aliases)
            # Curated names lead so that the per-item cap trims extractor
            # candidates first: a curated name is the memory writer's own claim.
            merged: Dict[str, str] = {}
            for normalized, display in raw_curated:
                _record_display(merged, aliases.get(normalized, normalized), display)
            curated_names = set(merged)
            for normalized, display in facts.displays.items():
                merged.setdefault(normalized, display)
            names = list(merged)[:MAX_ENTITIES_PER_CHUNK]
            kept = set(names)
            for normalized in names:
                mention_count[normalized] += 1
                _record_display(display_names, normalized, merged[normalized])
                if normalized not in entity_types and facts.types.get(normalized):
                    entity_types[normalized] = facts.types[normalized]
                entity_memories[normalized].add(item.id)
            # Accept curated names outright, but only those that survived the cap:
            # a name with no recorded display or provenance cannot become a row.
            memory_accepted.update(curated_names.intersection(kept))
            extracted_accepted.update(facts.trusted.intersection(kept))
            record_relations(facts, memory_id=item.id)
            memory_entities.append((item, names))

        accepted = (
            # An ALL-CAPS single word used to be accepted on one mention, which
            # let any stray 'RFC' or 'TODO' into the graph. Casing is not
            # evidence that a token names something; recurrence is.
            {
                name
                for name, count in mention_count.items()
                if count >= 2 or " " in display_names[name]
            }
            | memory_accepted
            | extracted_accepted
        )

        db.execute(delete(GraphEdge).where(GraphEdge.workspace_id == workspace_id))
        db.execute(delete(GraphEntity).where(GraphEntity.workspace_id == workspace_id))
        db.flush()

        entities: Dict[str, GraphEntity] = {}
        for normalized in sorted(accepted):
            entity = GraphEntity(
                workspace_id=workspace_id,
                name=display_names[normalized],
                normalized_name=normalized,
                entity_type=entity_types.get(normalized)
                or _entity_type(display_names[normalized]),
                mention_count=mention_count[normalized],
                source_ids_json=json.dumps(_bounded(entity_sources[normalized])),
                chunk_ids_json=json.dumps(_bounded(entity_chunks[normalized])),
                memory_ids_json=json.dumps(_bounded(entity_memories[normalized])),
            )
            db.add(entity)
            entities[normalized] = entity
        db.flush()

        edge_counts: Counter[Tuple[str, str]] = Counter()
        edge_sources: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        edge_chunks: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        edge_memories: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        specific_pairs: Set[Tuple[str, str]] = set()

        def passage_pairs(
            names: Iterable[str],
        ) -> Tuple[List[Tuple[str, str]], bool]:
            """Pairs a passage contributes, bounded without starving a name.

            The cap bounds the quadratic pairing, but taking the head of a
            *sorted* list chose the survivors by first letter: in a workspace
            whose passages each name more than the cap, an entity late in the
            alphabet was denied every edge however often it appeared. Rank by the
            recurrence this module already treats as evidence, pair the top of
            that ranking fully, and give each name past the cap one edge to the
            passage's strongest name — linear, so the bound holds, and no name
            that shared a passage is left unconnected. Pair keys are ordered
            alphabetically so the same pair keeps one key across passages.
            """
            ranked = sorted(
                {name for name in names if name in accepted},
                key=lambda name: (-mention_count[name], name),
            )
            head = sorted(ranked[:MAX_ENTITIES_PER_CHUNK])
            pairs = list(itertools.combinations(head, 2))
            specific = len(ranked) <= MAX_SPECIFIC_PASSAGE_ENTITIES
            for overflow in ranked[MAX_ENTITIES_PER_CHUNK:]:
                left, right = sorted((ranked[0], overflow))
                pairs.append((left, right))
            return pairs, specific

        for chunk, names in chunk_entities:
            pairs, specific = passage_pairs(names)
            for key in pairs:
                edge_counts[key] += 1
                edge_sources[key].add(chunk.source_id)
                edge_chunks[key].add(chunk.id)
                if specific:
                    specific_pairs.add(key)
        for item, names in memory_entities:
            pairs, specific = passage_pairs(names)
            for key in pairs:
                edge_counts[key] += 1
                edge_memories[key].add(item.id)
                if specific:
                    specific_pairs.add(key)

        edge_total = 0
        typed_pairs: Set[FrozenSet[str]] = set()
        # Entities that already carry at least one edge. The minimum-weight rule
        # is allowed to thin the graph but not to shatter it, so a pair it would
        # drop is reinstated when it is the only evidence either end has.
        connected: Set[str] = set()
        # A pair carrying a real relation does not also need the null one: with
        # `depends_on` recorded, a second `related_to` edge between the same two
        # nodes is a duplicate that says less.
        named_pairs = {
            frozenset((left, right))
            for left, right, relation in relation_weight
            if relation != "related_to" and left in accepted and right in accepted
        }
        for relation_key in sorted(relation_weight):
            left, right, relation = relation_key
            if left not in accepted or right not in accepted:
                continue
            if relation == "related_to" and frozenset((left, right)) in named_pairs:
                continue
            typed_pairs.add(frozenset((left, right)))
            connected.update((left, right))
            db.add(
                GraphEdge(
                    workspace_id=workspace_id,
                    from_entity_id=entities[left].id,
                    to_entity_id=entities[right].id,
                    relation=relation,
                    weight=relation_weight[relation_key],
                    confidence=relation_confidence.get(relation_key, 0.0),
                    source_ids_json=json.dumps(_bounded(relation_sources[relation_key])),
                    chunk_ids_json=json.dumps(_bounded(relation_chunks[relation_key])),
                    memory_ids_json=json.dumps(_bounded(relation_memories[relation_key])),
                )
            )
            edge_total += 1

        def add_co_occurrence(left: str, right: str, weight: int) -> None:
            nonlocal edge_total
            connected.update((left, right))
            db.add(
                GraphEdge(
                    workspace_id=workspace_id,
                    from_entity_id=entities[left].id,
                    to_entity_id=entities[right].id,
                    relation=CO_OCCURRENCE_RELATION,
                    weight=weight,
                    confidence=CO_OCCURRENCE_CONFIDENCE,
                    source_ids_json=json.dumps(_bounded(edge_sources[(left, right)])),
                    chunk_ids_json=json.dumps(_bounded(edge_chunks[(left, right)])),
                    memory_ids_json=json.dumps(_bounded(edge_memories[(left, right)])),
                )
            )
            edge_total += 1

        thin: List[Tuple[str, str]] = []
        for (left, right), weight in edge_counts.items():
            if frozenset((left, right)) in typed_pairs:
                # A stated relation already connects this pair; keeping the
                # co-occurrence twin would only dilute the walk with a weaker
                # duplicate of an edge we can name.
                continue
            if (
                weight < MIN_CO_OCCURRENCE_WEIGHT
                and (left, right) not in specific_pairs
            ):
                # Seen together exactly once, in a passage crowded enough that the
                # pairing is an artefact of the passage's length rather than a fact
                # about the two names.
                thin.append((left, right))
                continue
            add_co_occurrence(left, right, weight)

        # A workspace whose passages each name five or more things that never
        # recur together — interview notes, meeting minutes — has *every* pair at
        # weight 1, and the rule above would leave it with entities and no edges
        # at all, which is a strictly less useful graph than the noisy one. Give
        # back the strongest dropped pairing for any entity the filter orphaned:
        # the reduction is kept wherever the graph is already connected.
        for left, right in sorted(
            thin, key=lambda pair: (-edge_counts[pair], pair)
        ):
            if left in connected and right in connected:
                continue
            add_co_occurrence(left, right, edge_counts[(left, right)])

        projection = _projection(db, workspace_id)
        projection.status = "ready"
        projection.version = version_hasher.hexdigest()
        projection.entity_count = len(entities)
        projection.edge_count = edge_total
        projection.error = ""
        projection.built_at = utcnow()
        record_audit(
            db,
            workspace_id=workspace_id,
            actor_id=actor_id,
            action="graph.rebuilt",
            resource_type="graph_projection",
            resource_id=projection.id,
            detail={
                "entities": projection.entity_count,
                "edges": projection.edge_count,
                "version": projection.version,
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        projection = _projection(db, workspace_id)
        projection.status = "failed"
        projection.error = str(exc)[:1000]
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Walking the projection


@dataclass(frozen=True)
class EntityMatch:
    """Either the entity a name resolved to, or the near misses to offer back."""

    entity: Optional[GraphEntity]
    suggestions: List[str]


@dataclass(frozen=True)
class Neighbor:
    entity_id: str
    name: str
    entity_type: str
    distance: int
    relation: str
    via: str
    weight: int
    confidence: float


@dataclass(frozen=True)
class PathStep:
    from_name: str
    to_name: str
    relation: str
    weight: int
    confidence: float
    source_ids: List[str]
    chunk_ids: List[str]
    memory_ids: List[str]


def resolve_entity(db: Session, workspace_id: str, name: str) -> EntityMatch:
    """Exact normalized match, with substring near misses when there is none."""
    normalized = _normalized(name)
    if not normalized:
        return EntityMatch(entity=None, suggestions=[])
    # The rebuild merges 'the atlas' into 'atlas' when the workspace uses both,
    # so asking for either spelling has to reach the node that survived.
    candidates = name_candidates(normalized)
    entity = db.scalar(
        select(GraphEntity)
        .where(
            GraphEntity.workspace_id == workspace_id,
            GraphEntity.normalized_name.in_(candidates),
        )
        # Ordered so the exact spelling wins when both spellings survived as
        # separate nodes (which happens when neither is a workspace-wide alias).
        .order_by((GraphEntity.normalized_name == normalized).desc())
    )
    if entity is not None:
        return EntityMatch(entity=entity, suggestions=[])
    near = db.scalars(
        select(GraphEntity)
        .where(
            GraphEntity.workspace_id == workspace_id,
            GraphEntity.normalized_name.contains(normalized),
        )
        .order_by(GraphEntity.mention_count.desc(), GraphEntity.name.asc())
        .limit(5)
    )
    return EntityMatch(entity=None, suggestions=[row.name for row in near])


def _expansion(db: Session, workspace_id: str, frontier: Set[str]) -> List[GraphEdge]:
    """Edges touching the frontier, strongest first, hard-capped.

    Ordering puts named relations ahead of co-occurrence so that truncating a hub
    keeps the edges that carry meaning, and the id tiebreak keeps a walk
    reproducible between runs.
    """
    if not frontier:
        return []
    return list(
        db.scalars(
            select(GraphEdge)
            .where(
                GraphEdge.workspace_id == workspace_id,
                GraphEdge.from_entity_id.in_(frontier)
                | GraphEdge.to_entity_id.in_(frontier),
            )
            .order_by(
                (GraphEdge.relation == CO_OCCURRENCE_RELATION).asc(),
                GraphEdge.weight.desc(),
                GraphEdge.id.asc(),
            )
            .limit(MAX_EDGES_PER_EXPANSION)
        )
    )


def _entities_by_id(
    db: Session, workspace_id: str, ids: Iterable[str]
) -> Dict[str, GraphEntity]:
    wanted = set(ids)
    if not wanted:
        return {}
    return {
        row.id: row
        for row in db.scalars(
            select(GraphEntity).where(
                GraphEntity.workspace_id == workspace_id,
                GraphEntity.id.in_(wanted),
            )
        )
    }


def neighbors(
    db: Session,
    workspace_id: str,
    entity: GraphEntity,
    *,
    max_hops: int = DEFAULT_NEIGHBOR_HOPS,
    limit: int = DEFAULT_NEIGHBOR_RESULTS,
) -> Tuple[List[Neighbor], bool]:
    """Breadth-first walk out from `entity`. Returns (neighbors, truncated)."""
    hops = max(1, min(max_hops, MAX_NEIGHBOR_HOPS))
    cap = max(1, min(limit, MAX_NEIGHBOR_RESULTS))
    visited = {entity.id}
    frontier = {entity.id}
    known: Dict[str, GraphEntity] = {entity.id: entity}
    found: List[Neighbor] = []
    truncated = False
    for distance in range(1, hops + 1):
        edges = _expansion(db, workspace_id, frontier)
        truncated = truncated or len(edges) >= MAX_EDGES_PER_EXPANSION
        discovered: List[Tuple[str, GraphEdge, str]] = []
        for edge in edges:
            for near, far in (
                (edge.from_entity_id, edge.to_entity_id),
                (edge.to_entity_id, edge.from_entity_id),
            ):
                if near in frontier and far not in visited:
                    visited.add(far)
                    discovered.append((far, edge, near))
        if not discovered:
            break
        known.update(
            _entities_by_id(
                db, workspace_id, [far for far, _edge, _near in discovered]
            )
        )
        for far, edge, near in discovered:
            row = known.get(far)
            if row is None:
                continue
            if len(found) >= cap:
                truncated = True
                break
            found.append(
                Neighbor(
                    entity_id=row.id,
                    name=row.name,
                    entity_type=row.entity_type,
                    distance=distance,
                    relation=edge.relation,
                    via=known[near].name if near in known else "?",
                    weight=edge.weight,
                    confidence=edge.confidence,
                )
            )
        if len(found) >= cap:
            # Hops left unexplored means the answer really is a partial view.
            truncated = truncated or distance < hops
            break
        frontier = {far for far, _edge, _near in discovered}
    return found, truncated


def shortest_path(
    db: Session,
    workspace_id: str,
    start: GraphEntity,
    goal: GraphEntity,
    *,
    max_hops: int = DEFAULT_PATH_HOPS,
) -> Optional[List[PathStep]]:
    """Fewest-edge path between two entities, or None when there is none.

    Edges are undirected for traversal — reaching a fact through the reverse of
    `owns` is still reaching it — but each step is reported in its stored
    direction so the relation still reads correctly.
    """
    hops = max(1, min(max_hops, MAX_PATH_HOPS))
    if start.id == goal.id:
        return []
    came_from: Dict[str, Tuple[str, GraphEdge]] = {}
    visited = {start.id}
    frontier = {start.id}
    for _hop in range(hops):
        edges = _expansion(db, workspace_id, frontier)
        discovered: Set[str] = set()
        for edge in edges:
            for near, far in (
                (edge.from_entity_id, edge.to_entity_id),
                (edge.to_entity_id, edge.from_entity_id),
            ):
                if near not in frontier or far in visited:
                    continue
                visited.add(far)
                discovered.add(far)
                came_from[far] = (near, edge)
        if goal.id in visited:
            return _replay(db, workspace_id, start.id, goal.id, came_from)
        if not discovered or len(visited) >= MAX_PATH_VISITED:
            break
        frontier = discovered
    return None


def _replay(
    db: Session,
    workspace_id: str,
    start_id: str,
    goal_id: str,
    came_from: Dict[str, Tuple[str, GraphEdge]],
) -> List[PathStep]:
    chain: List[GraphEdge] = []
    cursor = goal_id
    while cursor != start_id:
        previous, edge = came_from[cursor]
        chain.append(edge)
        cursor = previous
    chain.reverse()
    names = _entities_by_id(
        db,
        workspace_id,
        itertools.chain.from_iterable(
            (edge.from_entity_id, edge.to_entity_id) for edge in chain
        ),
    )
    return [
        PathStep(
            from_name=names[edge.from_entity_id].name
            if edge.from_entity_id in names
            else "?",
            to_name=names[edge.to_entity_id].name if edge.to_entity_id in names else "?",
            relation=edge.relation,
            weight=edge.weight,
            confidence=edge.confidence,
            source_ids=json.loads(edge.source_ids_json),
            chunk_ids=json.loads(edge.chunk_ids_json),
            memory_ids=json.loads(edge.memory_ids_json),
        )
        for edge in chain
    ]
