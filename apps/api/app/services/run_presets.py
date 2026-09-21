"""Run presets: named policy bundles, expanded once at send time.

THE DOCTRINE, which is `Space.default_agent_id`'s doctrine applied to policy: a
preset is a composer SEED and a send-time expansion, never a run-path layer.
`_stage_turn` resolves a name into explicit per-turn facts — effort, retrieval
budget, plan flag, tool families — and writes them onto the `Run`. Nothing
downstream ever asks "which preset is this workspace on"; there is no such
question, because there is no such state. A run row carries what it was given,
which is why a turn that parks on an approval for an hour resumes under exactly
the policy its sender saw, and why a preset retired between the send and the
resume changes nothing about the turn in flight.

`auto` is the one name that never reaches a row. It is a deterministic
classifier over the prompt's own shape — no model call, no latency, no
ambiguity about what it will decide — resolved by `route()` at send time into a
concrete preset, and it is that concrete name the `Run` records. A router whose
decision could not be re-derived from the prompt would make every historical
run unexplainable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Pattern, Tuple

from sqlalchemy.orm import Session

from .llm_tools import ToolContext, registry_families

QUICK_LOOKUP = "quick-lookup"
GROUNDED = "grounded-answer"
DEEP_RESEARCH = "deep-research"
DELIVERABLE = "deliverable"
AUTO = "auto"


@dataclass(frozen=True)
class PresetPolicy:
    """What one preset pins. Every field is a per-turn fact, not a preference.

    `effort` of "" means "leave the user's own effort alone" — the identity
    value, not a third effort level. `families` of None means the whole
    registry, matching `subjects.families_for`'s own use of None as "no
    opinion" rather than "nothing".

    THERE IS NO `approval_mode` FIELD, and its absence is the doctrine made
    structural. It used to be here as "a client-side seed only", and the client
    duly seeded it — straight into `PUT /conversations/{id}/approval-mode`,
    persistent conversation state every later turn reads. Picking "Quick
    lookup", whose description mentions effort and passages and nothing else,
    moved a thread the user had deliberately put in `plan` (writes denied) down
    to `ask_writes` (writes park and can be approved), and kept it there. A
    containment control the user set is not a preset's to move; if a field
    cannot be offered without something writing it, the honest fix is not to
    offer it.
    """

    name: str
    label: str
    description: str
    effort: str
    budget: str
    families: Optional[FrozenSet[str]]
    step_plan: bool


#: `grounded-answer` is deliberately the IDENTITY preset: every field it pins
#: equals today's default, so picking it changes nothing at all. It exists so
#: "the normal way" is a thing the picker can name and a run row can record,
#: rather than the absence of a choice.
PRESETS: Dict[str, PresetPolicy] = {
    QUICK_LOOKUP: PresetPolicy(
        name=QUICK_LOOKUP,
        label="Quick lookup",
        description=(
            "One fast pass: low effort, three short passages, core tools only. "
            "For a fact you expect to be in one document."
        ),
        effort="low",
        budget="low",
        families=frozenset({"core"}),
        step_plan=False,
    ),
    GROUNDED: PresetPolicy(
        name=GROUNDED,
        label="Grounded answer",
        description=(
            "The normal turn: your own effort setting, five passages, every "
            "tool this thread has."
        ),
        effort="",
        budget="medium",
        families=None,
        step_plan=False,
    ),
    DEEP_RESEARCH: PresetPolicy(
        name=DEEP_RESEARCH,
        label="Deep research",
        description=(
            "Plans the question into steps first, then works through them: "
            "high effort, ten passages, search, graph, memory and delegation. "
            "No document or file writes."
        ),
        effort="high",
        budget="high",
        families=frozenset({"core", "graph", "delegation", "memory"}),
        step_plan=True,
    ),
    DELIVERABLE: PresetPolicy(
        name=DELIVERABLE,
        label="Deliverable",
        description=(
            "Plans first and then builds something: high effort, ten passages, "
            "every tool, with the guardian reviewing each action."
        ),
        effort="high",
        budget="high",
        families=None,
        step_plan=True,
    ),
}

#: The picker's own row for `auto`. Its policy fields are `grounded-answer`'s,
#: purely as placeholders for a UI that wants a shape — `resolve()` must never
#: return it, because "auto" is a question, not an answer.
_AUTO_ROW = PresetPolicy(
    name=AUTO,
    label="Auto",
    description=(
        "Reads the question and picks one of the presets below: a short "
        "factual question gets a quick lookup, a long or comparative one gets "
        "deep research, anything asking for a document gets the deliverable "
        "preset."
    ),
    effort="",
    budget="medium",
    families=None,
    step_plan=False,
)

#: Order the picker shows, `auto` first.
_ORDER: Tuple[str, ...] = (QUICK_LOOKUP, GROUNDED, DEEP_RESEARCH, DELIVERABLE)

#: The artefacts a user asks to be MADE. Nouns only — a noun on its own is not
#: a request, which is the whole lesson of this list.
#:
#: TWO SIGNALS, NOT ONE, and never a bare substring. `"memo" in prompt` matched
#: "memory" — a first-class product noun in this app — and `"chart"` matched
#: "charter", so "What is in my memory about the deploy host?" routed to the
#: DELIVERABLE preset: high effort, ten passages, and on the API path a
#: twelve-round plan turn, for a seven-word lookup. Word boundaries fix that
#: half. The other half is that a boundary-matched noun is still not a request
#: — "When was the deck rebuilt?" and "Which slides did legal approve?" are
#: lookups — so a creation VERB has to appear as well. `coverage.py`'s
#: DEBATE_MARKERS records the same lesson from the other cluster: a marker that
#: turns up inside ordinary prose cannot be rescued by padding.
DELIVERABLE_ARTEFACTS: Tuple[str, ...] = (
    "report",
    "memo",
    "deck",
    "slide",
    "spreadsheet",
    "chart",
    "dashboard",
    "doc",
    "document",
    "write-up",
    "presentation",
    "summary",
)

#: The verbs that turn one of those nouns into a request for work. Explicit
#: forms rather than a stem plus a suffix pattern, because the suffix pattern
#: is what re-admits "charter" for "chart".
DELIVERABLE_VERBS: Tuple[str, ...] = (
    "write",
    "writes",
    "writing",
    "draft",
    "drafts",
    "drafting",
    "make",
    "makes",
    "making",
    "build",
    "builds",
    "building",
    "create",
    "creates",
    "creating",
    "prepare",
    "prepares",
    "preparing",
    "produce",
    "produces",
    "producing",
    "generate",
    "generates",
    "generating",
    "compile",
    "compiles",
    "compiling",
    "assemble",
    "assembles",
    "assembling",
    "update",
    "updates",
    "updating",
    "put together",
)

#: Phrases that ask for an artefact on their own, needing no noun: "write up
#: what we found" names the work without naming the thing.
DELIVERABLE_PHRASES: Tuple[str, ...] = (
    "write up",
    "write me up",
)

#: Phrases that mean "this has more than one side". Comparison and causation are
#: the two shapes a single retrieval pass reliably under-serves.
RESEARCH_MARKERS: Tuple[str, ...] = (
    "compare",
    "trade-off",
    "tradeoff",
    "pros and cons",
    "evaluate",
    "why did",
    "research",
    "landscape",
    "versus",
    "vs",
)


def _word_re(markers: Tuple[str, ...], *, prefix: bool = False) -> Pattern[str]:
    """Match any of `markers` as WORDS, optionally plural.

    `prefix=True` also matches the marker as the start of a longer word, which
    is what keeps "compare" catching "compared" and "comparison". It is safe
    for the research markers, whose failure mode is a missed comparison, and
    deliberately NOT used for the deliverable nouns, where "chart" as a prefix
    is "charter" again.
    """
    body = "|".join(re.escape(marker) for marker in markers)
    tail = r"\w*" if prefix else r"s?\b"
    return re.compile(r"\b(?:" + body + r")" + tail)


_ARTEFACT_RE = _word_re(DELIVERABLE_ARTEFACTS)
_VERB_RE = _word_re(DELIVERABLE_VERBS)
_PHRASE_RE = _word_re(DELIVERABLE_PHRASES)
_RESEARCH_RE = _word_re(RESEARCH_MARKERS, prefix=True)

#: A question this short, opening this way, is a lookup. Both bounds are
#: deliberately conservative: misrouting a real question to `quick-lookup`
#: costs the user an answer, while misrouting a lookup to `grounded-answer`
#: costs only what today already costs.
LOOKUP_MAX_WORDS = 12
RESEARCH_MIN_WORDS = 60
RESEARCH_MIN_QUESTIONS = 3

LOOKUP_OPENERS: Tuple[str, ...] = (
    "what",
    "who",
    "when",
    "where",
    "which",
    "how many",
    "how much",
    "define",
)


def resolve(name: str) -> Optional[PresetPolicy]:
    """The policy for a preset name, or None when there is none to apply.

    None for "", for `auto` (which is a routing question, never applicable
    policy) and for any unknown name. Degrading rather than raising is the rule
    `styles.py` already applies to a retired style: a run row naming a preset
    this build has since dropped must behave like today, never fail the turn.
    """
    return PRESETS.get(name)


def classify(prompt: str) -> str:
    """The `auto` router: a preset name from the prompt's shape alone.

    Deterministic and model-free, in this order, and the order is the design:

    1. Asking for an artefact wins outright — a creation verb AND the artefact
       it acts on, or one of the phrases that is a request by itself. It is the
       only branch where the user named the OUTPUT rather than the question,
       and it is the one that the wrong route visibly fails to deliver.
    2. Length, question count, or a comparison/causation marker means research.
    3. A short question opening with an interrogative is a lookup.
    4. Everything else is the normal turn.
    """
    lowered = " ".join(prompt.strip().lower().split())
    words = len(prompt.split())
    if _PHRASE_RE.search(lowered) or (
        _VERB_RE.search(lowered) and _ARTEFACT_RE.search(lowered)
    ):
        return DELIVERABLE
    if (
        words >= RESEARCH_MIN_WORDS
        or lowered.count("?") >= RESEARCH_MIN_QUESTIONS
        or _RESEARCH_RE.search(lowered)
    ):
        return DEEP_RESEARCH
    if words <= LOOKUP_MAX_WORDS and lowered.startswith(LOOKUP_OPENERS):
        return QUICK_LOOKUP
    return GROUNDED


def route(name: str, prompt: str) -> str:
    """The preset a turn actually runs under: `auto` resolved, anything else kept.

    This is what the send endpoint stores, which is why `Run.preset` is never
    the literal "auto": a row saying "auto" would record the question instead of
    the answer, and nothing afterwards could tell you which policy the turn ran.
    """
    return classify(prompt) if name == AUTO else name


def allowed_tools_for_preset(
    db: Session, context: ToolContext, name: str
) -> Optional[FrozenSet[str]]:
    """The tool names a preset admits, or None for "no opinion".

    Derived from `registry_families` exactly the way `subjects.allowed_tools_for`
    derives its set, so a tool added to a family is in or out by where it ships
    and never by whether somebody remembered a second hand-written list.

    It can only ever be intersected with the other allow-lists (see
    `subjects.narrow`), so a preset can make a tool ABSENT and never present: it
    can neither widen an agent's provisioned subset nor a subject's families.
    """
    policy = resolve(name)
    if policy is None or policy.families is None:
        return None
    families = policy.families
    return frozenset(
        tool_name
        for family, tools in registry_families(db, context)
        if family in families
        for tool_name in tools
    )


def catalogue() -> List[PresetPolicy]:
    """Every row the picker shows, `auto` first.

    Includes the `auto` row, which `resolve()` deliberately does not: the picker
    needs something to render and the run path needs nothing at all.
    """
    return [_AUTO_ROW, *(PRESETS[name] for name in _ORDER)]
