"""Where a turn's content came from, and what that makes the next call cost.

The prompt-injection screen answers "does this text *look* like an
instruction?" — a classifier, and a classifier is wrong sometimes. This module
answers a different question that is never wrong: **where did this text come
from?** A page fetched off the open internet is untrusted whatever it says; a
benign-looking MCP result is still an MCP result. So the gate this module feeds
keys on provenance, not on words — no classifier, no model judgement, no text
matching.

The shape mirrors `screen.py` on purpose, including its central discipline:
*this module decides nothing about the run.* It labels blocks, records what a
turn ingested, and answers two pure questions (`gated_action`, `gate_reason`).
`agent_loop.evaluate_policy` — the one documented decision point — is what
turns any of that into an approval card, as a tighten-only clamp with the same
shape as `force_ask`.

The guarantee, stated exactly:

    When any GATING-class untrusted content has entered this turn, a proposed
    call that writes, or that reaches the network, escalates to a human
    approval card for the rest of the turn.

It is **turn-level**, not per-block, and that is not a limitation to be fixed
later — nothing in a model's output proves which context block caused which
tool call, and a model can act on something it read three steps ago.
`last_untrusted` exists to make the card *readable* ("this turn read a page
from the web") and is documented below as never consulted by the gate. Any
copy that says "this block triggered this call" would be claiming proof that
does not exist.

The carrier is a run event (`taint.marked`) rather than a column, for the same
reason `screen.flagged` is one: it needs no migration, it survives a park and
resume through a different process, and it is already what
`approval_mode_for_run` reads per call.

Four coverage gaps are deliberate and stated rather than papered over. The
first three are about the DEFAULT class set, which is `web_fetch,mcp_result`
and nothing else — every other untrusted class is labelled and recorded but
does not arm the gate unless `TAINT_GATING_CLASSES` names it:

* `workspace_chunk` and `memory_item` are recorded but NOT gated by default.
  Gating them would park every write in every grounded turn, and a gate nobody
  leaves on protects nobody. A poisoned document already in the library can
  therefore still drive an `auto_writes` write; the knob is there for a
  deployment that wants it, and the screen remains the (classifier-based,
  opt-in) answer for that class.
* `sandbox_output` is recorded but NOT gated by default either. Sandbox output
  is ordinarily the model's own code printing over workspace data, which is why
  the default set omits it — but a sandbox that can dial out brings content in
  from outside the deployment, and that content must gate. The executors
  therefore REPORT `web_fetch` on `ToolResult.provenance` whenever
  `SANDBOX_NETWORK_POLICY` is not "none" (or a workspace tool declares egress
  hosts), so a networked sandbox arms the default gate without a new class.
  See `sandbox/tools._execute` and `sandbox/custom._executor`.
* `tool_result`, the fail-closed catch-all, is likewise outside the default
  set. It is fail-closed for LABELLING (a family added next year is untrusted
  until its author says otherwise) and fail-OPEN for GATING (that label does
  not park anything by default). A family that genuinely carries external
  content must name a gating class rather than rely on the catch-all.
* `networked` on a `ToolSpec` is fail-OPEN — a family that forgets it is
  ungated for egress — while `provenance` is fail-CLOSED, defaulting to the
  untrusted catch-all. The registry sweep in `test_provenance.py` is the
  mitigation, and a test is weaker than a type; making `networked` a required
  argument is the stronger move, and costs ~40 construction sites.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Run, RunEvent, Workspace
from .events import append_event

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # `llm_tools` imports THIS module to type its two new ToolSpec fields, so
    # the edge has to stay one-way: at runtime nothing here touches llm_tools.
    from .llm_tools import ToolResult, ToolSpec
    from .retrieval import Evidence

# --------------------------------------------------------------------------
# The classes
#
# Seven, because seven is how many genuinely different origins a turn's content
# has in this build. They are strings rather than an enum because they ride a
# run-event payload and a VARCHAR(64) column, and an enum that has to be
# serialised at both ends is an enum that is really a string.

#: The user typed it. The only trusted class — and `ask_user`'s output is
#: literally the user's typed answer, so it belongs here too.
USER_DIRECT = "user_direct"
#: A passage out of this workspace's own index: retrieved evidence, the open
#: document, an attached file, a row from a connected database.
WORKSPACE_CHUNK = "workspace_chunk"
#: Text that arrived over the network from outside this deployment — a fetched
#: page, a hosted web-search excerpt, a Gmail body.
WEB_FETCH = "web_fetch"
#: Output of a tool on a connected MCP server. The canonical untrusted-external
#: content of this threat model.
MCP_RESULT = "mcp_result"
#: A memory saved from an earlier conversation.
MEMORY_ITEM = "memory_item"
#: Whatever the sandbox printed. Code the model wrote, run on data of unknown
#: origin, is not a trusted narrator of its own output.
SANDBOX_OUTPUT = "sandbox_output"
#: The catch-all, and the DEFAULT on `ToolSpec` on purpose: a tool family added
#: next year is untrusted until its author says otherwise. That is the only
#: direction this default may be wrong in.
TOOL_RESULT = "tool_result"

#: Every class this build knows. `Settings.taint_gating_class_set` intersects
#: with it, so a name this build does not know is dropped rather than silently
#: disarming the classes spelled correctly beside it.
CLASSES = (
    USER_DIRECT,
    WORKSPACE_CHUNK,
    WEB_FETCH,
    MCP_RESULT,
    MEMORY_ITEM,
    SANDBOX_OUTPUT,
    TOOL_RESULT,
)

#: Worst-first, for the one place a set of classes has to collapse to a single
#: text-carrying block (`turn_start_blocks`' prompt). Every class in the set is
#: still recorded — this only decides which one the text hangs off, so the
#: choice is about what an incident reader sees first, not about what gates.
_RISK_ORDER = (
    MCP_RESULT,
    WEB_FETCH,
    SANDBOX_OUTPUT,
    TOOL_RESULT,
    WORKSPACE_CHUNK,
    MEMORY_ITEM,
)

TRUSTED = frozenset({USER_DIRECT})
UNTRUSTED = frozenset(CLASSES) - TRUSTED

#: The run event this module writes. Same carrier as `screen.flagged`: no
#: migration, survives park/resume, already the shape `_run_was_flagged` reads.
TAINT_MARKED = "taint.marked"

#: How many `taint.marked` rows one `turn_taint` read will look at. A turn
#: writes a handful (one per ingest point), so this is a ceiling on a runaway
#: loop rather than a window anybody is expected to hit — and it is a bound on
#: a query that runs once per tool call.
#:
#: THE BOUND MUST NOT BE ABLE TO DISARM THE GATE. Two rules keep it honest:
#: the window keeps the NEWEST rows (a bound on a monotonic safety signal that
#: kept the oldest would make a late `web_fetch` invisible), and a read that
#: SATURATES the window makes `turn_taint` assume every gating class is
#: present. A run past the ceiling is a run nobody can account for, and the
#: only safe answer about content you did not look at is "assume it was there".
MAX_TAINT_EVENTS = 200

#: Labels recorded per event, for the attribution refinement only.
MAX_EVENT_LABELS = 5
#: One label, clipped. Short because it is read, not parsed.
MAX_LABEL_CHARS = 60

#: `AgentToolCall.gate_reason` is VARCHAR(64) and the web parses the string
#: exactly. So `gate_reason` drops whole classes off the tail rather than
#: truncating mid-name: a clipped string that no longer parses renders as
#: nothing at all, which is the one outcome worse than an incomplete list.
MAX_GATE_REASON_CHARS = 64


@dataclass(frozen=True)
class ContextBlock:
    """One piece of content a turn ingested, with where it came from.

    `kind` deliberately reuses the exact strings `agent_loop._screen` already
    takes — 'prompt' | 'evidence' | 'document' | 'memory' | 'tool_output' |
    'web_search' — so ONE structure feeds both the taint ledger and the
    classifier. Two parallel descriptions of the same text is how the two
    drift, and a drifted pair would screen one string and gate another.

    `label` is for a human reading the approval card ("the open document",
    "gmail_search"). It is never parsed and never consulted by the gate.

    `reported` marks a block that carries no text of its own because the class
    IS the whole report — an executor saying "I pulled in a web page" through
    `ToolResult.provenance`. Such a block is screened as nothing (there is
    nothing to screen) and recorded regardless, which is the difference
    between a delegate child being visible to the gate and being a hole
    through it.
    """

    provenance: str
    kind: str
    text: str
    label: str = ""
    reported: bool = False

    @property
    def untrusted(self) -> bool:
        return self.provenance in UNTRUSTED

    @property
    def recordable(self) -> bool:
        """Whether this block belongs in the ledger.

        Untrusted, and asserting something: text the model actually read, or
        an executor's explicit report about itself. An untrusted block that is
        neither — an empty document splice, an evidence list that came back
        empty — is silence, and silence is not an ingest.
        """
        return self.untrusted and bool(self.text.strip() or self.reported)


# --------------------------------------------------------------------------
# Builders — one per injection point in the loop


def prompt_blocks(prompt: str, classes: Sequence[str]) -> List[ContextBlock]:
    """The prompt, classed by where its text ACTUALLY came from.

    `classes` empty means the default and the common case: the user typed it,
    so it is `user_direct` — trusted, never screened, arming nothing.

    A non-empty set is the workflow executor saying "this prompt is a template
    with tool output, an upstream agent's answer, or a webhook body spliced
    into it". One block carries the text (the worst class present, so an
    incident reader sees the sharpest one first) and the rest ride as
    text-less `reported` blocks — the same seam `tool_result_blocks` uses for a
    delegate child, and for the same reason: every class has to reach the
    ledger even though there is only one string to screen.
    """
    known = [name for name in _RISK_ORDER if name in set(classes)]
    if not known:
        return [
            ContextBlock(
                provenance=USER_DIRECT,
                kind="prompt",
                text=prompt or "",
                label="what you asked",
            )
        ]
    blocks = [
        ContextBlock(
            provenance=known[0],
            kind="prompt",
            text=prompt or "",
            label="this step's prompt",
        )
    ]
    blocks.extend(
        ContextBlock(
            provenance=name,
            kind="prompt",
            text="",
            label="this step's prompt",
            reported=True,
        )
        for name in known[1:]
    )
    return blocks


def turn_start_blocks(
    *,
    prompt: str,
    evidence: Sequence[Evidence],
    spliced_context: str,
    memory_context: str,
    prompt_classes: Sequence[str] = (),
) -> List[ContextBlock]:
    """What a turn ingests before its first model call.

    The three untrusted strings are byte-identical to the three `_screen` calls
    this replaced, with ONE deliberate change: a mixed evidence list splits by
    class, so a hosted web-search excerpt is recorded as `web_fetch` rather
    than being averaged into `workspace_chunk`. A single-class list produces
    one block whose text is exactly `"\\n\\n".join(item.excerpt ...)`, which
    `test_provenance.py` pins.

    The user's own prompt is here as a `user_direct` block — trusted, so it is
    never screened (it is not an attack on itself) and never taints anything.
    It is present because a ledger that recorded only the scary half would not
    be a record of what the turn read.

    `prompt_classes` is how a caller says the prompt is NOT the user's typing.
    A workflow agent node's prompt is a template with an upstream tool's
    output, an upstream agent's answer or a webhook body spliced into it, and
    stamping that `user_direct` would launder a compromised MCP server's text
    through the one trusted class — unscreened, and arming nothing. Default
    empty, so every chat turn is byte-identical to before. See `prompt_blocks`.
    """
    blocks: List[ContextBlock] = prompt_blocks(prompt, prompt_classes)
    # Grouped, not per-item: the screen's spend and its event count both scale
    # with the number of strings it is handed, and one block per passage would
    # multiply both by the retrieval limit.
    grouped: Dict[str, List[str]] = {}
    for item in evidence:
        grouped.setdefault(classify_evidence(item), []).append(item.excerpt)
    for class_name in sorted(grouped):
        blocks.append(
            ContextBlock(
                provenance=class_name,
                kind="evidence",
                text="\n\n".join(grouped[class_name]),
                label=(
                    "pages found on the web"
                    if class_name == WEB_FETCH
                    else "passages from your library"
                ),
            )
        )
    blocks.append(
        # Named "document" still — the kind every dashboard, audit query and
        # test already reads. See the note at the old call site.
        ContextBlock(
            provenance=WORKSPACE_CHUNK,
            kind="document",
            text=spliced_context or "",
            label="what you have open",
        )
    )
    blocks.append(
        ContextBlock(
            provenance=MEMORY_ITEM,
            kind="memory",
            text=memory_context or "",
            label="saved memories",
        )
    )
    return blocks


def tool_result_blocks(
    spec: Optional[ToolSpec], result: ToolResult
) -> List[ContextBlock]:
    """What one executed tool call folded back into the turn.

    The first block's text is the same joined string `_screen` was handed here
    before — the rendered content AND the evidence excerpts, so a tool hiding
    an injection in an excerpt rather than the body does not slip past.

    Any class the EXECUTOR reported about itself (`ToolResult.provenance`)
    beyond its spec's own becomes a text-less block: there is nothing extra to
    screen, but the class has to reach the ledger. That is the seam that stops
    delegation from being a hole straight through the gate — a child writes no
    run events by construction, so `run_child_agent` reporting "I read a web
    page" is the parent's only way to know.
    """
    from .agent_loop import _screen_kind

    name = spec.name if spec is not None else ""
    kind = _screen_kind(name)
    own = spec.provenance if spec is not None else TOOL_RESULT
    blocks = [
        ContextBlock(
            provenance=own,
            kind=kind,
            text="\n\n".join(
                [result.content or "", *(item.excerpt for item in result.evidence)]
            ),
            label=name,
        )
    ]
    for class_name in sorted(set(result.provenance) - {own}):
        if class_name not in UNTRUSTED:
            continue
        blocks.append(
            ContextBlock(
                provenance=class_name,
                kind=kind,
                text="",
                label=name,
                reported=True,
            )
        )
    return blocks


def web_search_blocks(evidence: Sequence[Evidence]) -> List[ContextBlock]:
    """The hosted-search excerpts one model step harvested."""
    return [
        ContextBlock(
            provenance=WEB_FETCH,
            kind="web_search",
            text="\n\n".join(item.excerpt for item in evidence),
            label="web search",
        )
    ]


def classify_evidence(item: Evidence) -> str:
    """One evidence item's class: a web result, or this workspace's own index."""
    # Lazy: `web_search` pulls the model config and this module is imported by
    # llm_tools, which every registry build touches.
    from .web_search import WebEvidence

    return WEB_FETCH if isinstance(item, WebEvidence) else WORKSPACE_CHUNK


# --------------------------------------------------------------------------
# The ledger


def mark(
    db: Session,
    run: Run,
    blocks: Iterable[ContextBlock],
    *,
    settings: Optional[Settings] = None,
) -> Optional[RunEvent]:
    """Record what this ingest point brought into the turn. One event, or none.

    Written UNCONDITIONALLY — not behind `screen_enabled`, and not behind
    `taint_gating_enabled`. The ledger is a record of what the turn read, and a
    record that only exists while a gate is armed is no record at all: an
    operator turning gating on next week would have nothing to look back at,
    and `test_provenance_is_recorded_even_with_the_screen_off` pins that.

    `gating` is the subset that arms the gate *at write time*, and it is
    informational. `turn_taint` recomputes the intersection at READ time, so
    flipping the workspace override mid-park takes effect on the very next
    call rather than on the next turn.
    """
    settings = settings or get_settings()
    recorded = [block for block in blocks if block.recordable]
    if not recorded:
        return None
    classes = sorted({block.provenance for block in recorded})
    gating = gating_classes(
        db, workspace_id=run.workspace_id, settings=settings
    )
    labels: List[str] = []
    for block in recorded:
        label = block.label.strip()[:MAX_LABEL_CHARS]
        if label and label not in labels:
            labels.append(label)
    payload: Dict[str, Any] = {
        "classes": classes,
        # The first recorded block's kind. The classes list is the load-bearing
        # field; `kind` is the one-word "where in the turn" an incident reader
        # wants, and an ingest point whose blocks disagree about it (turn start,
        # which carries evidence + document + memory) is answered by its first.
        "kind": recorded[0].kind,
        "labels": labels[:MAX_EVENT_LABELS],
        "gating": sorted(set(classes) & gating),
    }
    event = append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type=TAINT_MARKED,
        payload=payload,
    )
    db.commit()
    return event


def _marked_payloads(db: Session, run: Run) -> Tuple[List[Dict[str, Any]], bool]:
    """This run's taint events, oldest first, bounded. Junk rows are skipped.

    Returns the payloads AND whether the bound was hit. The query takes the
    NEWEST `MAX_TAINT_EVENTS` rows and then restores chronological order, so
    the two callers still read oldest-first while the rows that fall out of the
    window are the ones nobody is deciding on any more. `last_untrusted`'s
    `reversed()` scan is then really the latest ingest rather than the latest
    of the first 200.
    """
    rows = list(
        db.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == TAINT_MARKED)
            .order_by(RunEvent.sequence.desc())
            .limit(MAX_TAINT_EVENTS)
        )
    )
    rows.reverse()
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        try:
            parsed = json.loads(row.payload_json)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return (payloads, len(rows) >= MAX_TAINT_EVENTS)


def turn_taint(
    db: Session, run: Run, *, settings: Optional[Settings] = None
) -> frozenset[str]:
    """The gating-class content this turn has ingested so far.

    Read PER TOOL CALL, not once per turn, for exactly the reason
    `approval_mode_for_run` is: an MCP result folded in at step three must gate
    the very next call in the same queue. A per-turn cache would reintroduce
    the lapse this exists to prevent.

    The intersection with `gating_classes` happens here rather than at write
    time so the workspace override and the deployment flag are read live.

    A SATURATED READ ANSWERS "everything". `_marked_payloads` is bounded, and a
    run that has written more taint events than the bound has ingests this
    function did not look at. Reporting the classes it happened to see would
    make the gate fail OPEN on exactly the busiest turns; reporting the whole
    gating set costs a parked call on a turn already past a ceiling nobody is
    expected to reach.
    """
    settings = settings or get_settings()
    gating = gating_classes(db, workspace_id=run.workspace_id, settings=settings)
    if not gating:
        return frozenset()
    payloads, saturated = _marked_payloads(db, run)
    if saturated:
        return gating
    seen: set[str] = set()
    for payload in payloads:
        raw = payload.get("classes")
        if isinstance(raw, list):
            seen.update(str(item) for item in raw)
    return frozenset(seen & gating)


def last_untrusted(db: Session, run: Run) -> Optional[Dict[str, Any]]:
    """The most recent untrusted ingest, for READABILITY. Never for the gate.

    A REFINEMENT, and the honesty rule around it is the whole reason it is
    documented this loudly: nothing in a model's output proves which block
    caused which call. This answers "the last untrusted thing this turn read",
    which is a plausible thing to show a reviewer and not a claim about
    causation. The enforced guarantee stays strictly turn-level.
    """
    payloads, _saturated = _marked_payloads(db, run)
    for payload in reversed(payloads):
        raw = payload.get("classes")
        if isinstance(raw, list) and any(str(item) in UNTRUSTED for item in raw):
            return payload
    return None


# --------------------------------------------------------------------------
# The posture, and the two pure questions the clamp asks


def gating_classes(
    db: Session, *, workspace_id: str, settings: Optional[Settings] = None
) -> frozenset[str]:
    """Which classes arm the gate for this workspace, right now.

    THE OVERRIDE IS READ FIRST, and it is read as a three-value sentinel:

    * `"off"` — empty. "Record but do not gate", whatever the deployment says.
    * `"on"` — the deployment's class set, or the two genuinely external
      classes when the deployment narrowed or cleared it. A workspace that
      asks for the strict posture must GET a non-empty set: the panel's copy
      says "keep the gate armed even if the deployment default changes", and
      an "on" that resolved to nothing wherever the flag was off would be a
      security control that is stored, audited, re-displayed and inert.
    * `""` and ANY unrecognised value — follow the deployment, including its
      off switch. An unknown override must tighten, never disarm, which is the
      same rule `approval_mode_for_run` applies to an unknown `approval_mode`;
      here "tighten" means falling through to the deployment rather than
      disarming, because there is nothing tighter to fall through to.

    So the deployment flag is a default, not a ceiling — a workspace may raise
    it, and only `"off"` (workspace) or the flag-with-no-override lowers it.
    """
    settings = settings or get_settings()
    override = (
        db.scalar(select(Workspace.taint_gating).where(Workspace.id == workspace_id))
        or ""
    ).strip().lower()
    if override == "off":
        return frozenset()
    if override == "on":
        return settings.taint_gating_class_set or frozenset({WEB_FETCH, MCP_RESULT})
    if not settings.taint_gating_enabled:
        return frozenset()
    return settings.taint_gating_class_set


def gated_action(spec: Optional[ToolSpec]) -> str:
    """What this call risks: "" | "egress" | "write".

    `""` for an unknown tool: `evaluate_policy` resolves one to `allow` so the
    loop can hand the model a plain "no such tool" error, and parking a run on
    a phantom approval would be strictly worse than that.

    Egress is checked FIRST so a *read-only* MCP call is still gated. A tool
    that reaches the network on the workspace's behalf can carry data out of
    it, whatever it claims to do with what it finds — that is the half a
    write-only gate would miss entirely.
    """
    if spec is None:
        return ""
    if spec.networked:
        return "egress"
    if not spec.read_only:
        return "write"
    return ""


def gate_reason(*, classes: Iterable[str], action: str) -> str:
    """The machine string stamped on `AgentToolCall.gate_reason`.

    `taint:<sorted,classes>:<action>`. Sorted so the same situation produces
    the same string, and so a test can assert one. Over the column's 64
    characters, whole classes come off the END — the web parses this exactly
    and renders nothing it cannot parse, so half a class name would cost the
    reviewer the entire sentence.

    A DROP IS ANNOUNCED, never silent. The names sort alphabetically, so the
    tail this has to cut is always `workspace_chunk` then `web_fetch` — the
    class the feature is named after. A reviewer shown "this turn read an MCP
    result and a saved memory", with the fetched page quietly missing, would
    approve an egress call on an incomplete account of where the turn's content
    came from. So the elision rides in the string as a `+N` segment, and
    `describeGate` renders it as "and N other kinds of source".

    Returns "" when even one class plus the marker cannot fit, which is the
    same "render nothing rather than something wrong" rule as before.
    """
    if not action:
        return ""
    names = sorted({str(item) for item in classes if str(item) in CLASSES})
    if not names:
        return ""
    dropped = 0
    while names:
        shown = names if not dropped else [*names, f"+{dropped}"]
        rendered = f"taint:{','.join(shown)}:{action}"
        if len(rendered) <= MAX_GATE_REASON_CHARS:
            return rendered
        names.pop()
        dropped += 1
    return ""
