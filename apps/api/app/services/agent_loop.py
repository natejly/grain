from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..models import (
    MODE_DECIDER_PREFIX,
    SHARED_OWNER,
    Agent,
    AgentToolCall,
    Conversation,
    OrgToolPolicy,
    Run,
    RunEvent,
    ToolPolicy,
    WorkflowRun,
)
from . import (
    budget,
    checkpoints,
    coworking,
    orgs,
    screen,
    skills,
    spaces,
    subjects,
    usage,
    webhooks,
)
from .audit import record_audit
from .events import DeltaBuffer, append_event
from .harness import ModelStep, resolve_harness
from .llm_tools import (
    ASK_USER,
    EXIT_PLAN_MODE,
    ToolContext,
    ToolResult,
    ToolSpec,
    build_registry,
    exit_plan_mode_spec,
)
from .model import CHAT_INSTRUCTIONS, _openai_input
from .retrieval import Evidence
from .usage import usage_scope
from .web_search import (
    anchor_citations,
    harvest,
    revive_evidence,
    web_numbers,
    web_search_tool,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6

DENIAL_OUTPUT = (
    "The user denied this tool call. Do not retry it. Continue using what you "
    "already have, and tell the user what you could not do."
)

#: The run event a prompt-injection hit writes. It is both the observability
#: flag (visible in the activity feed and admin observability, like every other
#: run event) AND the per-turn escalation signal: `approval_mode_for_run` reads
#: it back to force a flagged turn to `ask_all`. Living on `run_events` is why
#: this feature needs no migration and why the flag survives a park/resume in
#: another process — the turn's history carries it.
SCREEN_FLAGGED = "screen.flagged"


@dataclass
class AgentResult:
    answer: str
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class Done:
    answer: str
    evidence: List[Evidence]


@dataclass
class Paused:
    """The run is parked waiting for the user to decide on a tool call."""

    tool_call_id: str


@dataclass
class BudgetPaused:
    """The run is parked because the workspace reached its spend ceiling.

    A separate outcome from `Paused` rather than a flag on it, because the two
    are resumed by different people doing different things: an approval is
    decided by whoever reads the card, and a ceiling is raised by an owner. A
    caller that pattern-matches on the outcome cannot forget the difference.
    """

    reason: str
    message: str


@dataclass
class Cancelled:
    text: str


Outcome = Done | Paused | BudgetPaused | Cancelled

#: Why a `waiting_for_approval` run is parked. See `Run.paused_reason`.
PAUSED_FOR_APPROVAL = "approval"
PAUSED_FOR_BUDGET = "budget"


def subject_context(subject: Optional[subjects.Subject]) -> str:
    """The turn's subject, quoted for its input.

    Labelled as the user's own material and not as an instruction — the same
    rule the retrieved passages follow: content is evidence, never a command. A
    document containing "ignore your instructions" is a document, not a new
    prompt, and so is a source file and so is a dashboard spec. The wording per
    kind lives in `services/subjects.py`, which is also where the decision about
    *how much* of each kind to inject lives.
    """
    return subject.context if subject else ""


@dataclass
class LoopState:
    """Everything needed to resume a turn in a different process.

    `input_items` holds plain dicts rather than SDK objects — the Responses API
    accepts dicts on input, so a round trip through JSON is lossless.
    """

    input_items: List[Any] = field(default_factory=list)
    pending_calls: List[Dict[str, Any]] = field(default_factory=list)
    iteration: int = 0
    text_so_far: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    #: Highest `run.steer` event sequence already folded into `input_items`.
    #: Persisted with the state so a park/resume neither replays a steering
    #: note nor drops one sent while the run was parked.
    steered_sequence: int = 0
    #: How many parked-grade calls the guardian reviewer has waved through this
    #: turn. On the state for the same reason: the cap is per turn, and a turn
    #: can cross processes.
    guardian_approvals: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "input_items": self.input_items,
                "pending_calls": self.pending_calls,
                "iteration": self.iteration,
                "text_so_far": self.text_so_far,
                "evidence": [asdict(item) for item in self.evidence],
                "steered_sequence": self.steered_sequence,
                "guardian_approvals": self.guardian_approvals,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str) -> LoopState:
        data = json.loads(raw)
        return cls(
            input_items=data.get("input_items") or [],
            pending_calls=data.get("pending_calls") or [],
            iteration=int(data.get("iteration") or 0),
            text_so_far=data.get("text_so_far") or "",
            evidence=[revive_evidence(item) for item in data.get("evidence") or []],
            steered_sequence=int(data.get("steered_sequence") or 0),
            guardian_approvals=int(data.get("guardian_approvals") or 0),
        )


class OrgBoundExceeded(RuntimeError):
    """The organization does not permit the harness or model this turn would use.

    A distinct type so the worker can report it as configuration rather than as a
    provider failure: nothing is wrong with the model, the org has simply not
    allowed it, and telling a user "the request failed" when the honest answer is
    "your organization does not allow this" sends them debugging the wrong thing.
    """


def _default_model_step(
    settings: Settings, run: Run, evidence: List[Evidence]
) -> ModelStep:
    """The model behind one turn.

    `evidence` only reaches the scripted test double, which quotes it when no
    script entry matches the prompt. The OpenAI path is handed the same passages
    through the prompt itself, so it has no use for them here.

    How the turn is *billed* is not an argument, because this function is also
    the seam tests replace: it is carried by the `usage_scope` the caller has
    already opened, and read back inside `stream_agent_response`.

    The provider branch itself lives in the harness registry now; this stays as
    the thin injectable seam that resolves it, so every `model_step=` override
    keeps its single point of interception.

    The organization's harness and model bounds are deliberately *not* checked
    here, even though this is where the harness is resolved: this function is the
    seam tests and the workflow executor replace wholesale, so a bound enforced
    inside it would be a bound an injected `model_step` skips.
    `_enforce_org_bounds` runs above, on the path every turn takes.
    """
    return resolve_harness(settings).build_step(
        settings,
        prompt=run.prompt,
        user_id=run.created_by,
        evidence=list(evidence),
        # "" is the unset convention; None lets the harness fall back to the
        # deployment defaults, so a run with no override is identical to today.
        model=run.requested_model or None,
        effort=run.requested_effort or None,
        thinking=run.show_thinking,
    )


def _enforce_org_bounds(db: Session, run: Run, settings: Settings) -> None:
    """Refuse the turn if the org does not permit this harness or this model.

    Placed on the path *before* the model step is resolved, and above the
    `model_step or _default_model_step(...)` seam rather than inside it. That
    placement is the whole guarantee: the executor and every test inject their own
    step, so a check living in the default builder would be a check that only runs
    when nobody overrode it — which is exactly backwards for a control.

    It covers the two things a request-time check structurally cannot see. The
    *deployment default* model is used by any turn that names no override, and the
    harness is process-wide, so neither is ever selected by a workspace and neither
    would otherwise be inside any org bound at all.
    """
    if not orgs.harness_permitted(db, workspace_id=run.workspace_id, settings=settings):
        raise OrgBoundExceeded(
            f"Your organization does not allow the “{settings.active_model_provider}” "
            f"harness."
        )
    effective_model = run.requested_model or settings.default_model
    # Only enforced against models the deployment itself offers. An org list that
    # does not mention a model outside `selectable_models` is not a prohibition on
    # it — `_bounded` intersects, and a model the deployment never vouched for is
    # already refused a layer down.
    if effective_model not in settings.selectable_models:
        return
    if effective_model not in orgs.allowed_models(
        db, workspace_id=run.workspace_id, settings=settings
    ):
        raise OrgBoundExceeded(
            f"Your organization does not allow the model “{effective_model}”."
        )


def _tool_payload(
    registry: Dict[str, ToolSpec], settings: Settings
) -> List[Dict[str, Any]]:
    """The `tools` array for one request: local functions, then hosted tools.

    A hosted tool has no ToolSpec because there is nothing here to execute and
    nothing to approve — the provider runs it — so it joins the wire payload
    without joining the registry the policy and approval paths consult.
    """
    payload: List[Dict[str, Any]] = [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in registry.values()
    ]
    hosted = web_search_tool(settings)
    if hosted is not None:
        payload.append(hosted)
    return payload


def _serialize_item(item: Any) -> Any:
    """Turn one model output item into something JSON-serializable."""
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return {key: value for key, value in vars(item).items() if value is not None}


#: Where a tool call is being made from. `chat` is a person typing, who will see
#: the result and can undo it. `workflow` is a compiled DAG executing, possibly
#: on a schedule with nobody at the diff.
PolicyScope = Literal["chat", "workflow"]

CHAT_SCOPE: PolicyScope = "chat"
WORKFLOW_SCOPE: PolicyScope = "workflow"

#: How much one *conversation* wants to be asked. Held on `Conversation`, read
#: by `evaluate_policy`, and only ever consulted at `chat` scope.
#:
#: - ``ask_writes``   read-only tools run, write-capable tools park. The default,
#:                    and the only mode a thread has until somebody says otherwise.
#: - ``ask_all``      everything parks, read-only searches included, for a thread
#:                    working somewhere sensitive enough to want each look first.
#: - ``auto_writes``  writes execute without parking. The bypass.
#: - ``plan``         the thread is researching, not acting: read-only tools
#:                    run, write-capable tools are refused outright (deny, not
#:                    ask — stricter than ``ask_all``), and the mode is left by
#:                    the model proposing a plan through ``exit_plan_mode``,
#:                    whose approval card carries the plan itself.
#: - ``guardian``     writes are triaged by a cheap reviewer model before they
#:                    park: a call the guardian judges a routine, clearly-safe
#:                    application of the user's request executes (attributed
#:                    ``mode:guardian``, capped per turn); everything else
#:                    parks for the human exactly as ``ask_writes`` would.
#:                    Policy-wise it IS ``ask_writes`` — the triage happens at
#:                    the park site, never inside `evaluate_policy`, so a deny
#:                    still denies and a `force_ask` still reaches a person.
ApprovalMode = Literal["ask_writes", "ask_all", "auto_writes", "plan", "guardian"]

ASK_WRITES: ApprovalMode = "ask_writes"
ASK_ALL: ApprovalMode = "ask_all"
AUTO_WRITES: ApprovalMode = "auto_writes"
PLAN: ApprovalMode = "plan"
GUARDIAN: ApprovalMode = "guardian"

APPROVAL_MODES: Tuple[ApprovalMode, ...] = (
    ASK_WRITES,
    ASK_ALL,
    AUTO_WRITES,
    PLAN,
    GUARDIAN,
)

#: `MODE_DECIDER_PREFIX` is the prefix `AgentToolCall.decided_by` carries when a
#: *mode* let a call through. The column otherwise holds a user id, and ids here
#: are uuid4 hex with no colon in them, so nothing a person could be called
#: collides with it. It is defined beside the column, in `models`, because
#: `AgentToolCall.approved_by_mode` reads it back out — one prefix, written in
#: one place and parsed in one place. Re-exported here because this module is
#: where the rule that writes it lives.


def mode_decider(mode: str) -> str:
    """`decided_by` for a call that a mode approved. "" when a mode did not.

    Property 3 of the approval modes, in one function: a row that names a *user*
    as the decider of a write nobody looked at is worse than a row that names
    nobody, because a reader who audits it later has no way to tell it apart
    from a write that really was reviewed. So the bypass writes down what
    actually happened — the mode is the decider, and it is spelled as one.
    """
    return f"{MODE_DECIDER_PREFIX}{mode}" if mode else ""


#: The three verdicts, ordered by how much they restrain the agent. Every rule in
#: `evaluate_policy` that is described as "tightening" is a move up this ladder,
#: and the organization clamp is literally a `max` over it.
_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}


def _stricter(left: str, right: str) -> str:
    """Whichever of two verdicts restrains the agent more.

    Unknown strings sort as the strictest thing there is, so a policy value this
    module does not recognise can never be the reason something ran.
    """
    return left if _STRICTNESS.get(left, 2) >= _STRICTNESS.get(right, 2) else right


def _in_scope_or_carried_deny(
    scope: PolicyScope, at: Callable[[PolicyScope], Optional[str]]
) -> Optional[str]:
    """One tier's verdict for one scope, or None if that tier is silent.

    The rule, in one sentence, applied at both the workspace tier and the org
    tier: **a row in this scope decides; absent one, a `chat` deny still carries;
    absent that, the tier says nothing.**

    Factored out rather than written twice because the two tiers agreeing is not
    a coincidence to be maintained by hand — it is the reason a reader who
    understands "always allow does not authorise a 3am run" already understands
    what an org-level `chat` row does to a workflow.
    """
    here = at(scope)
    if here is not None:
        return here
    return "deny" if at(CHAT_SCOPE) == "deny" else None


def _org_ceiling(db: Session, *, workspace_id: str, tool_name: str, scope: PolicyScope) -> str:
    """The strictest verdict the organization will permit for this tool.

    `allow` when the org has no opinion, which is the identity element of
    `_stricter` — an org that has configured nothing changes no answer.
    """
    org_id = orgs.org_id_for_workspace(db, workspace_id)
    if not org_id:
        return "allow"
    rows = list(
        db.scalars(
            select(OrgToolPolicy).where(
                OrgToolPolicy.organization_id == org_id,
                OrgToolPolicy.tool_name == tool_name,
            )
        )
    )
    by_scope = {row.scope: row.policy for row in rows if row.policy in _STRICTNESS}
    return _in_scope_or_carried_deny(scope, by_scope.get) or "allow"


@dataclass(frozen=True)
class Verdict:
    """What the policy said, and whether a conversation's mode is what said it."""

    #: ask | allow | deny.
    policy: str
    #: The mode that produced this verdict, when a mode is what produced it, and
    #: "" when a policy row or the tool's own default did. Set for exactly one
    #: transition — a bypass turning an approval park into an execution — because
    #: that is the only case in which a write happens and no person ever sees it.
    #: `AgentToolCall.decided_by` is stamped from it.
    by_mode: str = ""
    #: True only when this `ask` is the tool's own DEFAULT under guardian mode —
    #: no policy row asked it, no org ceiling mandated it, no `force_ask` raised
    #: it. The guardian reviewer may only ever soften that default: a person or
    #: an organization who wrote "ask me" gets a person, and computing the
    #: provenance HERE — where the rows and the ceiling are already in hand — is
    #: what keeps the park site from having to re-derive it and drift.
    guardian_may_review: bool = False


def evaluate_policy(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    spec: Optional[ToolSpec],
    scope: PolicyScope,
    mode: ApprovalMode = ASK_WRITES,
) -> Verdict:
    """ask | allow | deny for one tool in one situation. The only decision point.

    Without an override, read-only tools run unattended and write-capable tools
    ask. An unknown tool resolves to allow so the loop can hand the model back a
    plain "unknown tool" error instead of parking the run on a phantom approval.

    `scope` has no default, and that is deliberate: the only value a default
    could take is the wide one, and a call site that forgot to think about
    unattended execution would then get the answer that assumes a human is
    watching. Making it explicit costs one keyword and removes the failure.

    How the two scopes interact is the whole of ADR 0007's sharpest residual
    risk:

    - A row **in this scope** decides. Full stop.
    - Absent one, a `chat` **deny** still denies a workflow. A prohibition is not
      a grant; refusing to carry it across would be the one direction of leakage
      that makes the system less safe, and nobody granted anything by writing it.
    - Absent one, any other `chat` row — notably a standing `allow` from clicking
      "always allow" on an approval card — is **ignored** by a workflow, which
      falls back to the tool's own default and therefore parks on every write.

    So "always allow send_email", clicked once in a conversation, does not
    authorise a 3am scheduled run to send mail. Getting that authority requires
    granting it at `workflow` scope, where the question being answered is the
    question actually being asked.

    `user_id` is the second axis, added by ADR 0010 once a workspace could hold
    two people: it stopped being true that the person clicking "always allow" was
    the only person the grant could reach. Rows come in two tiers — the
    workspace's (`owner_id == ""`, writable only by an owner) and the caller's
    own — and within the requested scope:

    - **A `deny` decides, whichever tier it is in.** A shared deny beating my
      personal allow is the load-bearing half: without it, exempting myself from
      a workspace prohibition is one PUT away and the escalation this closes
      reopens sideways. A personal deny beating a shared allow is free, because
      tightening always is. This is the existing rule — *a prohibition is not a
      grant* — extended one axis further out.
    - **Otherwise mine decides, then the workspace's.** Personal precedence is
      what makes a grant mean what its clicker thought it meant.

    The cross-scope `chat` deny below is read across both tiers for the same
    reason it is read across scopes: a prohibition should be hard to lose track
    of, wherever it was written.

    `mode` is the conversation's own answer to "how much do you want to be
    asked", and it is applied last, on top of everything above, under two rules
    that are the whole safety argument for having a bypass at all:

    - **It is ignored unless `scope` is chat.** A mode is stored on a
      conversation, and a workflow's backing Run carries a conversation too (the
      executor makes one to hold the transcript), so without this line a mode set
      while typing could reach an unattended 3am run through the back door the
      chat|workflow split was built to close. `mode` defaults to `ask_writes` for
      the same reason `scope` has no default at all: the forgetful call site
      gets the narrow answer.
    - **A `deny` stays denied**, in every mode. A prohibition is not a grant, and
      the bypass is permission to skip *asking*, not permission to overrule a
      refusal. An `ask` row is different in kind — it is a request to be asked,
      and the mode is the conversation answering that request in advance — so
      `auto_writes` does clear one, while nothing clears a deny.

    Above all of that sits the **organization**, and its rule is the one-way one:
    *scopes may only tighten organization-wide policies*. It is enforced as a
    single `_stricter` at the very end of this function, and being last is the
    whole of the argument. Everything that can loosen — a workspace `allow`, a
    personal `allow`, `auto_writes`, `DEV_UNRESTRICTED_AGENT` (which is a mode, so
    it arrives here already collapsed into `auto_writes`) — has finished running
    by then, and none of them can produce a value stricter than what they were
    given, so none of them can move the answer back down. In ladder terms:

    - org `deny` is 2, and no verdict is above 2, so it is final;
    - org `ask` is 1, so a 0 from below is raised to 1 while a 2 stands —
      tightening below an `ask` stays available, relaxing does not;
    - org `allow` is 0, the identity element, so an org with no opinion is
      indistinguishable from no org at all.

    The org is looked up from `workspace_id` **inside this function** rather than
    accepted as a parameter, and that is deliberate: a fourth argument is a
    fourth thing a call site can pass wrongly, and "the caller forgot the org" is
    exactly the shape of bug that would make the ceiling optional. Derived, it is
    unbypassable by construction — there is no argument to omit.

    One consequence to state plainly: with `spec is None` no rows are read at
    all, org included, so an org cannot deny a tool that does not exist. The
    unknown-tool `allow` returns to the model as a "no such tool" error rather
    than running anything, so there is nothing there for a ceiling to restrain.
    """
    if spec is None:
        base = "allow"
    else:
        rows = list(
            db.scalars(
                select(ToolPolicy).where(
                    ToolPolicy.workspace_id == workspace_id,
                    ToolPolicy.tool_name == spec.name,
                    # Shared plus the caller's own. Another member's grant is not
                    # merely outranked here, it is never fetched — the query is
                    # where cross-person authority has to stop, because anything
                    # that reaches the ranking below can be reasoned about wrong.
                    ToolPolicy.owner_id.in_({SHARED_OWNER, user_id}),
                )
            )
        )
        valid = [row for row in rows if row.policy in {"ask", "allow", "deny"}]
        by_owner = {
            (row.owner_id == SHARED_OWNER, row.scope): row.policy for row in valid
        }

        def _tier(at_scope: PolicyScope) -> Optional[str]:
            """This scope's verdict: any deny, else mine, else the workspace's."""
            mine = by_owner.get((False, at_scope))
            ours = by_owner.get((True, at_scope))
            if "deny" in (mine, ours):
                return "deny"
            return mine if mine is not None else ours

        resolved = _in_scope_or_carried_deny(scope, _tier)
        base = resolved if resolved is not None else ("allow" if spec.read_only else "ask")

    # An `ask` the guardian may later soften: the tool's own default, under
    # guardian mode, in chat scope — never one a policy row wrote. Rows are the
    # user's or the workspace's explicit "ask me", and the reviewer replaces
    # only the *default* prudence, not a stated instruction. The org ceiling
    # and `force_ask` get their say below.
    default_guardian_ask = (
        spec is not None
        and not spec.force_ask
        and mode == GUARDIAN
        and scope == CHAT_SCOPE
        and resolved is None
        and base == "ask"
    )

    # GUARDIAN is deliberately in the first arm: at policy level it *is*
    # ask_writes, and letting a new mode value fall through to the final
    # `else` would silently make it a second `auto_writes`. The guardian's
    # triage happens where the resulting `ask` would park, in
    # `_drain_pending`, so nothing it does can loosen a deny or reach a
    # workflow scope this line already refused.
    if scope != CHAT_SCOPE or mode in (ASK_WRITES, GUARDIAN) or base == "deny":
        result = Verdict(policy=base, guardian_may_review=default_guardian_ask)
    elif mode == PLAN:
        # Plan mode. Read-only tools keep whatever the rows above said, so a
        # standing `ask` still asks; `exit_plan_mode` parks unconditionally,
        # because approving that card *is* approving the plan and no standing
        # row may pre-answer it; and anything write-capable is refused outright.
        # A plan-mode turn is never *offered* a write tool (`_plan_narrowed`
        # strips them from the registry), so this deny is the second,
        # independent lock — it catches a queued write from a conversation
        # switched into plan mode while parked. `by_mode` stays "" on that
        # deny: the attribution marks a bypass letting a write through, and
        # refusing one is the opposite of that.
        if spec is not None and spec.name == EXIT_PLAN_MODE:
            result = Verdict(policy="ask")
        elif spec is not None and not spec.read_only:
            result = Verdict(policy="deny")
        else:
            result = Verdict(policy=base)
    elif mode == ASK_ALL:
        result = Verdict(policy="ask")
    else:
        # auto_writes. `by_mode` is set only where the mode changed the answer: a
        # tool a row already allowed was not let through by the bypass, and
        # saying it was would put a claim on the audit row that the policy table
        # contradicts.
        result = Verdict(policy="allow", by_mode=AUTO_WRITES if base == "ask" else "")

    # A custom sandbox tool with approval="always" carries `force_ask`, which may
    # only tighten: any `allow` this arrives at — from a policy row, the tool's
    # own default, or an `auto_writes` bypass, in chat scope or workflow scope —
    # becomes an `ask`. A `deny` is never reached here (it returned above), so a
    # prohibition still prohibits, mirroring how a deny survives every mode.
    if spec is not None and spec.force_ask and result.policy == "allow":
        result = Verdict(policy="ask")

    if spec is None:
        return result
    # The organization ceiling, and it is last on purpose — see the docstring.
    # Removing this clamp is the mutation that makes `test_org_scope.py` fail.
    ceiling = _org_ceiling(
        db, workspace_id=workspace_id, tool_name=spec.name, scope=scope
    )
    clamped = _stricter(result.policy, ceiling)
    # An org that voiced ANY opinion — even an `ask` that happens to equal the
    # default the value arrived at — takes the call out of the guardian's
    # reach. "A human must review this tool" from an org is not the default
    # prudence the reviewer exists to soften; letting a cheap model answer it
    # would be the exact scope-may-only-tighten inversion the clamp's
    # placement exists to rule out.
    may_review = result.guardian_may_review and ceiling == "allow"
    if clamped == result.policy:
        if may_review == result.guardian_may_review:
            return result
        return Verdict(
            policy=result.policy, by_mode=result.by_mode, guardian_may_review=False
        )
    # The org moved the answer, so a `by_mode` attribution would now be a lie: the
    # bypass did not let this call through, it was overruled. Property 3 of the
    # approval modes is that `decided_by` says what actually happened, and a row
    # claiming a mode approved a call that never ran is exactly the record a later
    # auditor cannot tell apart from a real one.
    return Verdict(policy=clamped)


def resolve_policy(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    spec: Optional[ToolSpec],
    scope: PolicyScope,
    mode: ApprovalMode = ASK_WRITES,
) -> str:
    """`evaluate_policy`'s verdict, as the bare string most callers want.

    Not a second decision point — there is still exactly one, immediately above.
    This is its narrow face, kept because every caller but the agent loop only
    ever compares the verdict to "ask" / "deny", and asking them to reach through
    a dataclass to do that would buy nothing. The loop uses the wide one, because
    it is the only caller that also has to record *who* decided.
    """
    return evaluate_policy(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        spec=spec,
        scope=scope,
        mode=mode,
    ).policy


def _run_was_flagged(db: Session, run: Run) -> bool:
    """Whether a prompt-injection screen has flagged this run's turn.

    A cheap existence query over `run_events`. One hit is enough — a turn that
    ingested injected content stays flagged for the rest of its tool calls, which
    is the point: the escalation must not lapse just because the *next* source
    the same turn reads happens to be clean.
    """
    return (
        db.scalar(
            select(RunEvent.id)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == SCREEN_FLAGGED,
            )
            .limit(1)
        )
        is not None
    )


def _screen(
    db: Session, run: Run, *, kind: str, text: str, settings: Settings
) -> None:
    """Screen one untrusted string; record a hit and, in enforce, flag the run.

    Called at each injection point — the retrieved passages and open document at
    turn start, tool/MCP output as it is folded back, web_search results as they
    are harvested. `classify` decides nothing about the run; the mode is applied
    here:

    - A clean verdict records nothing and changes nothing.
    - An injection verdict, or a `ScreenError` (fail-closed: a backend that could
      not answer is treated as a hit, never a silent pass), writes a
      `screen.flagged` event + audit row. The event is the escalation signal
      `approval_mode_for_run` reads, and it is written in both modes so a hit is
      observable either way — shadow simply never escalates, because that read is
      gated on enforce.

    Off by default: with `screen_enabled` false this returns before any classify
    call, so a turn is byte-identical to today.
    """
    if not settings.screen_enabled:
        return
    body = text or ""
    if not body.strip():
        return
    try:
        verdict = screen.classify(body, kind=kind, settings=settings)
        if verdict.label != "injection":
            return
        score: Optional[float] = round(verdict.score, 3)
        reason = "injection"
    except screen.ScreenError as exc:
        score = None
        reason = f"backend_error: {str(exc)[:200]}"
    enforced = settings.screen_mode == "enforce"
    payload: Dict[str, Any] = {
        "kind": kind,
        "backend": settings.screen_backend,
        "mode": settings.screen_mode,
        "score": score,
        "reason": reason,
        # Whether this hit will actually gate the turn. True only in enforce;
        # shadow records the identical flag and leaves the turn unchanged.
        "enforced": enforced,
    }
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type=SCREEN_FLAGGED,
        payload=payload,
    )
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="screen.flagged",
        resource_type="run",
        resource_id=run.id,
        detail=payload,
    )
    db.commit()


def approval_mode_for_run(
    db: Session, run: Run, *, scope: PolicyScope, settings: Optional[Settings] = None
) -> ApprovalMode:
    """The mode governing this run's next tool call.

    Read at each decision rather than once per turn, so switching a thread out of
    bypass is felt by the very next call the model makes instead of the next
    turn the user sends.

    Anything that is not a chat run resolves to `ask_writes` here, and that is
    the first of the two locks on property 1 (the second is in `evaluate_policy`,
    which ignores the mode at workflow scope regardless of what it is handed). A
    workflow's backing Run has a Conversation of its own, so a single missing
    guard would let a mode reach a run nobody is watching.

    The prompt-injection escalation lands here rather than in a parallel gate: a
    turn flagged by the screen is forced to `ask_all` — the strictest posture,
    every tool call parks — which *overrides* the conversation's stored mode,
    `auto_writes` included. That is the whole safety argument for the escalation:
    an injection cannot ride a thread's bypass into an unreviewed write. Gated on
    enforce mode (shadow records the same flag but never escalates) and on chat
    scope like the rest of this function. Read per call, so a hit recorded from a
    tool output mid-turn escalates the very next call in the same turn.

    The escalation lands on `ask_all` even for a thread in `plan` mode, and that
    is a deliberate trade rather than an oversight: the two modes are not
    ordered. Plan is stricter about writes (deny beats ask) but looser about
    reads (they run silently, and a read with attacker-shaped arguments is an
    exfiltration channel). `ask_all` is the posture where *nothing* runs without
    a person seeing it — reads park, and the write a planning model should not
    be proposing anyway meets a human decision instead of a silent deny.
    """
    if scope != CHAT_SCOPE or not run.conversation_id:
        return ASK_WRITES
    settings = settings or get_settings()
    if settings.screen_mode == "enforce" and _run_was_flagged(db, run):
        return ASK_ALL
    conversation = db.get(Conversation, run.conversation_id)
    if conversation is None or conversation.workspace_id != run.workspace_id:
        conversation = None
    # Plan mode outranks the development bypass below, alone among the modes:
    # the bypass exists to skip approval friction while developing, and a
    # developer who switched a thread into plan mode is exercising plan mode
    # itself — a flag that quietly turned it back into `auto_writes` would make
    # the feature untestable in the only environment the flag can be on.
    #
    # Refreshed rather than trusted, for the same reason `_drain_pending`
    # refreshes the run: switching a thread's mode is a safety switch, and one
    # that waits for the current turn to finish is not one. Sessions here are
    # opened with `expire_on_commit=False`, so a Conversation still resident in
    # the identity map would keep answering with whatever it said when the turn
    # began — for all six iterations of it. As written today the identity map's
    # references are weak and this function drops the only strong one before it
    # returns, so the refresh changes no behaviour; it is what stops the
    # behaviour from being an accident of garbage collection that any future
    # caller holding on to a Conversation would silently undo. No guard for a
    # conversation deleted mid-turn: purging one deletes its runs too, and
    # `_drain_pending`'s `db.refresh(run)` reaches that first.
    if conversation is not None:
        db.refresh(conversation)
        if conversation.approval_mode == PLAN:
            return PLAN
    # The development bypass, and it deliberately sits BELOW the injection
    # escalation rather than above it. `DEV_UNRESTRICTED_AGENT` is a bypass, and
    # a bypass that outranked the screen would be strictly weaker than the
    # `auto_writes` the screen already overrides — the one thing an injection
    # must never be able to ride. It also sits below the scope guard above, so it
    # cannot reach an unattended run: that guard and `evaluate_policy`'s own
    # chat-scope check are two independent locks and this adds no third door.
    #
    # It resolves to `auto_writes` rather than to a fourth mode so that
    # everything downstream — the deny that still denies, the `decided_by`
    # attribution on every call it lets through, the trail the indicator renders
    # — is the code that is already there and already tested.
    if settings.dev_unrestricted_agent:
        return AUTO_WRITES
    if conversation is None:
        return ASK_WRITES
    # Spelled out rather than cast: a column holding a value this enum has since
    # dropped must land on the strict mode, not on whatever it says.
    if conversation.approval_mode == ASK_ALL:
        return ASK_ALL
    if conversation.approval_mode == AUTO_WRITES:
        return AUTO_WRITES
    if conversation.approval_mode == GUARDIAN:
        return GUARDIAN
    return ASK_WRITES


def _billing_operation(scope: PolicyScope) -> str:
    """How a turn in this situation is billed. Same split as the policy scope,
    and for the same reason: unattended spend is the kind worth seeing alone."""
    return usage.WORKFLOW_NODE if scope == WORKFLOW_SCOPE else usage.CHAT


def policy_scope_for_run(db: Session, run: Run) -> PolicyScope:
    """Which situation this run's tool calls are being made in.

    A chat run is `chat`. A run the workflow executor created is `workflow` —
    including while an *agent* node is borrowing it, because "nobody is watching"
    is a property of what started the run, not of which loop happens to be
    executing inside it.

    That distinction is the leg of the fix that matters most. ADR 0007's
    injection scenario ends at an agent node honouring instructions it read out
    of a fetched document, and an agent node resolving at chat scope would
    inherit the very standing grant the scope split exists to withhold.

    A cron task run has no WorkflowRun backing it — a fresh turn *is* its
    execution — but it is no less unattended, so `cron_id` classifies it as
    `workflow` by construction. The whole policy/approval/billing chain then
    treats it like a scheduled workflow without a second code path.
    """
    if run.cron_id:
        return WORKFLOW_SCOPE
    backing = db.scalar(select(WorkflowRun.id).where(WorkflowRun.run_id == run.id))
    return WORKFLOW_SCOPE if backing is not None else CHAT_SCOPE


def _render_result(result: ToolResult, evidence_offset: int) -> str:
    parts: List[str] = []
    if result.content:
        parts.append(result.bounded_content())
    for index, item in enumerate(result.evidence, start=evidence_offset + 1):
        parts.append(
            f"[{index}] {item.filename}, passage {item.ordinal + 1}\n{item.excerpt}"
        )
    return "\n\n".join(parts) if parts else "(empty result)"


def _cancelled(db: Session, run: Run, state: LoopState) -> Cancelled:
    run.status = "cancelled"
    # `paused_reason` describes a park, and this run is no longer parked. Cleared
    # everywhere a run leaves the park so the column can never be read as a
    # statement about a run that is not waiting for anybody.
    run.paused_reason = ""
    run.lease_expires_at = None
    run.agent_state_json = None
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.cancelled",
        payload={"status": "cancelled"},
    )
    db.commit()
    return Cancelled(text=state.text_so_far)


def describe_proposal(
    db: Session, context: ToolContext, spec: ToolSpec, raw_arguments: str
) -> str:
    """Render what the call would do, for the approval card. Never fatal."""
    if spec.preview is None:
        return ""
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
        if not isinstance(arguments, dict):
            return ""
        return spec.preview(db, context, arguments)[:20000]
    except Exception:
        # A preview is a courtesy; failing to render one must not block the
        # approval the user is waiting on.
        db.rollback()
        return ""


def _park_for_approval(
    db: Session,
    run: Run,
    state: LoopState,
    call: Dict[str, Any],
    spec: ToolSpec,
    context: ToolContext,
) -> Paused:
    """Record the proposed call, persist loop state, and stop the turn."""
    raw_arguments = str(call.get("arguments") or "{}")
    record = AgentToolCall(
        workspace_id=run.workspace_id,
        run_id=run.id,
        name=str(call.get("name") or ""),
        arguments_json=raw_arguments[:4000],
        call_id=str(call.get("call_id") or "")[:80],
        proposal_preview=describe_proposal(db, context, spec, raw_arguments),
        status="proposed",
    )
    db.add(record)
    db.flush()
    call["tool_call_id"] = record.id
    run.status = "waiting_for_approval"
    run.paused_reason = PAUSED_FOR_APPROVAL
    run.lease_expires_at = None
    run.agent_state_json = state.to_json()
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="tool.proposed",
        payload={
            "tool_call_id": record.id,
            "tool_name": record.name,
            "description": spec.description,
            "arguments": record.arguments_json,
            "preview": record.proposal_preview,
        },
    )
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.waiting_for_approval",
        payload={"status": "waiting_for_approval"},
    )
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="agent_tool.proposed",
        resource_type="agent_tool_call",
        resource_id=record.id,
        detail={"tool": record.name},
    )
    # The outbound-webhook chokepoint for parks: ids and the tool's name only
    # — never arguments or the preview, which are workspace content.
    webhooks.emit(
        db,
        workspace_id=run.workspace_id,
        event="approval.requested",
        payload={
            "tool_call_id": record.id,
            "run_id": run.id,
            "tool_name": record.name,
        },
    )
    db.commit()
    return Paused(tool_call_id=record.id)


def _park_for_budget(
    db: Session, run: Run, state: LoopState, verdict: budget.Verdict
) -> BudgetPaused:
    """Suspend the turn on the spend ceiling, exactly as an approval suspends it.

    Deliberately the same three writes `_park_for_approval` makes — the status,
    the cleared lease, the serialized `LoopState` — because that combination is
    what every other part of this system already understands as "parked, waiting
    on a person, resumable, do not sweep". ADR 0008 argues the case; the code's
    version of the argument is that this function had almost nothing to invent.

    What it does *not* write is an `AgentToolCall`. There is no proposed call
    here and nothing to approve — the model had not been asked yet — so a row
    claiming otherwise would put a card in the approval inbox that approves
    nothing and would let the decision endpoint resume a run whose limit is
    still exceeded.
    """
    run.status = "waiting_for_approval"
    run.paused_reason = PAUSED_FOR_BUDGET
    run.lease_expires_at = None
    run.agent_state_json = state.to_json()
    payload: Dict[str, Any] = {
        "status": "waiting_for_approval",
        "paused_reason": PAUSED_FOR_BUDGET,
        "message": verdict.message(),
        **verdict.payload(),
    }
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.waiting_for_budget",
        payload=payload,
    )
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="run.budget_exceeded",
        resource_type="run",
        resource_id=run.id,
        detail=verdict.payload(),
    )
    db.commit()
    return BudgetPaused(reason=verdict.reason, message=verdict.message())


def execute_agent_tool_call(
    db: Session,
    run: Run,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    name: str,
    raw_arguments: str,
    *,
    existing_id: Optional[str] = None,
    denied: bool = False,
    decided_by: str = "",
) -> ToolResult:
    """Run one tool call and record it.

    `decided_by` names whoever authorised a call that skipped the approval park —
    in practice `mode_decider(...)`, since a call a *person* decided was already
    stamped by the decision endpoint. Left empty for the ordinary case of a tool
    whose policy never asked, where an unset `decided_at` is the true statement:
    there was no decision to make.
    """
    started = time.monotonic()
    record = db.get(AgentToolCall, existing_id) if existing_id else None
    if record is None:
        record = AgentToolCall(
            workspace_id=run.workspace_id,
            run_id=run.id,
            name=name,
            arguments_json=raw_arguments[:4000],
        )
        db.add(record)
        db.flush()
    if decided_by:
        record.decided_by = decided_by
        record.decided_at = utcnow()
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="tool.started",
        payload={
            "tool_call_id": record.id,
            "tool_name": name,
            # The one window in which "turn it off" can still prevent something
            # is the turn itself, so the bypass has to be legible *while* it is
            # spending — a client that only learns which calls went through
            # unreviewed from the settle-time refetch shows "nothing has gone
            # through yet" for exactly as long as things are going through, and
            # a run that parks or fails never corrects it.
            "approved_by_mode": record.approved_by_mode,
        },
    )
    db.commit()

    if denied:
        result = ToolResult(content=DENIAL_OUTPUT)
        record.status = "denied"
    else:
        spec = registry.get(name)
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
        except (ValueError, TypeError):
            arguments = None
        if spec is None:
            result = ToolResult(content=f"Error: unknown tool “{name}”.")
            record.status = "failed"
            record.error = "unknown tool"
        elif arguments is None:
            result = ToolResult(content="Error: tool arguments were not valid JSON.")
            record.status = "failed"
            record.error = "invalid arguments"
        else:
            # The undo trail's capture point: read the before-state of what a
            # write is about to change, then record it once the write lands.
            # Both halves are swallow-and-log — a checkpoint is a convenience
            # the tool call must never pay for with a failure.
            pending = None
            if not spec.read_only:
                try:
                    pending = checkpoints.capture_before(db, context, name, arguments)
                except Exception:
                    logger.warning(
                        "checkpoint capture failed for %s", name, exc_info=True
                    )
            try:
                result = spec.executor(db, context, arguments)
                record.status = "succeeded"
            except Exception as exc:  # Tool bugs become model-visible errors, not crashes.
                db.rollback()
                record = db.get(AgentToolCall, record.id) or record
                result = ToolResult(content=f"Error: tool failed: {str(exc)[:300]}")
                record.status = "failed"
                record.error = str(exc)[:1000]
            else:
                if pending is not None:
                    try:
                        checkpoints.record_checkpoint(
                            db,
                            run=run,
                            tool_call_id=record.id,
                            name=name,
                            pending=pending,
                            result=result,
                        )
                    except Exception:
                        logger.warning(
                            "checkpoint record failed for %s", name, exc_info=True
                        )
    record.latency_ms = int((time.monotonic() - started) * 1000)
    record.result_preview = (result.content or "")[:500]
    # Carried beside the preview, not inside it: `result_preview` is prose for
    # the model clipped at 500 characters, and the artifact list is the part a
    # long stdout pushes off the end. The event repeats them so a live chat
    # renders the figure without waiting for a refetch.
    record.artifacts_json = json.dumps(result.artifacts)
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="tool.completed",
        payload={
            "tool_call_id": record.id,
            "tool_name": name,
            "status": record.status,
            "preview": record.result_preview,
            "artifacts": result.artifacts,
            # Repeated from `tool.started` rather than left to be remembered: a
            # client that joined the stream late, or that reloaded mid-run, has
            # only the events it received, and the completed row is the one it
            # keeps.
            "approved_by_mode": record.approved_by_mode,
        },
    )
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="agent_tool.executed",
        resource_type="agent_tool_call",
        resource_id=record.id,
        detail={
            "tool": name,
            "status": record.status,
            # Only present when something other than a human authorised the
            # call, which is the fact an audit of a bypassed thread is looking
            # for. Absent means the ordinary path.
            **({"decided_by": decided_by} if decided_by else {}),
        },
    )
    db.commit()
    return result


def _sanitized_arguments(name: str, raw: str) -> str:
    """Strip argument keys only a human may supply, before the amendment merge.

    `ask_user`'s executor reads `answer` out of its merged arguments, and the
    genuine answer arrives as a decision amendment. The model authors
    `arguments_json` freely, so without this strip it could ship its own
    `answer` alongside the question and a bare Approve would enter fabricated
    text into the transcript as the user's own words — the one trust class
    that is deliberately never screened. In-band flags cannot fix this (the
    model writes the whole object); removing the key at the execution boundary
    can.
    """
    if name != ASK_USER:
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return raw
    if not isinstance(parsed, dict) or "answer" not in parsed:
        return raw
    parsed.pop("answer", None)
    return json.dumps(parsed)


def _amended(raw_arguments: str, amendment: Optional[Any]) -> str:
    """Fold a reviewer's amendment into the model's arguments.

    Only reached for a call a human approved with conditions, so unparseable
    arguments are left exactly as they are — the executor already has the one
    correct answer for those ("tool arguments were not valid JSON") and a
    silently repaired object would be a worse lie than the error.
    """
    if not isinstance(amendment, dict) or not amendment:
        return raw_arguments
    try:
        parsed = json.loads(raw_arguments or "{}")
    except (ValueError, TypeError):
        return raw_arguments
    if not isinstance(parsed, dict):
        return raw_arguments
    return json.dumps({**parsed, **amendment})


def _guardian_clears(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    spec: ToolSpec,
    call: Dict[str, Any],
    context: ToolContext,
    scope: PolicyScope,
    mode: ApprovalMode,
    settings: Settings,
) -> bool:
    """Whether the guardian reviewer waves this would-park call through.

    Only ever *loosens* an `ask` into an execution, under five stacked guards:
    the conversation opted into guardian mode; the scope is chat (an unattended
    run has no user whose intent a reviewer could check the call against); the
    spec does not `force_ask` (its author demanded a person); the call is not
    `exit_plan_mode` (approving a plan is the user's, definitionally); and the
    per-turn approval cap has room. Everything else — a defer, a reviewer
    error, a missing reviewer — falls back to the park, which is exactly
    today's behaviour. Fail-closed means the feature can only remove waits,
    never reviews the mode did not already gate.
    """
    if mode != GUARDIAN or scope != CHAT_SCOPE:
        return False
    if spec.force_ask or spec.name == EXIT_PLAN_MODE:
        return False
    from . import guardian

    if state.guardian_approvals >= guardian.MAX_GUARDIAN_APPROVALS_PER_TURN:
        return False
    # The reviewer judges the call against what the user is asking for NOW,
    # not only the prompt that started the turn. Two rules keep those in step
    # with steering: a steer this model step has not yet absorbed means the
    # user is actively re-instructing — the reviewer defers to the human park,
    # because a person mid-sentence outranks any triage of their old words —
    # and steers the step did absorb are appended to the prompt the reviewer
    # reads, so "don't send it yet" reaches the gate that would have sent it.
    steers = list(
        db.scalars(
            select(RunEvent)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == "run.steer",
            )
            .order_by(RunEvent.sequence)
        )
    )
    if any(row.sequence > state.steered_sequence for row in steers):
        return False
    prompt = run.prompt
    if steers:
        notes = []
        for row in steers:
            try:
                content = str(json.loads(row.payload_json).get("content") or "")
            except ValueError:
                content = ""
            if content.strip():
                notes.append(f"- {content.strip()}")
        if notes:
            prompt = f"{prompt}\n\nMid-task additions from the user:\n" + "\n".join(
                notes
            )
    raw_arguments = str(call.get("arguments") or "{}")
    verdict = guardian.review(
        name=spec.name,
        arguments_json=raw_arguments,
        preview=describe_proposal(db, context, spec, raw_arguments),
        prompt=prompt,
        settings=settings,
    )
    if not verdict.approve:
        return False
    state.guardian_approvals += 1
    record_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=run.created_by,
        action="agent_tool.guardian_approved",
        resource_type="run",
        resource_id=run.id,
        detail={"tool": spec.name, "reason": verdict.reason[:300]},
    )
    db.commit()
    return True


def _delegate_parallel_batch(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    scope: PolicyScope,
    settings: Settings,
) -> bool:
    """Execute a run of queued `delegate` calls concurrently. False = not ours.

    The one place the loop fans out, and the shape is chosen by two hard
    constraints. `run_events` is unique on (run_id, sequence), so worker
    threads write **no events** — every row and event is written serially on
    the parent session, before and after the concurrent section, in queue
    order. And `usage_scope` is a ContextVar, so each worker runs inside a
    `contextvars` copy of this frame's context and bills to the same turn.

    Only calls whose policy verdict is a plain `allow` join a batch; the first
    call needing anything else falls back to the serial path, which knows how
    to park, deny, and attribute. A batch of one is left to the serial path
    too — same behaviour, one code path fewer.
    """
    from .delegation import DELEGATE_TOOL, MAX_PARALLEL_DELEGATES, _delegate

    spec = registry.get(DELEGATE_TOOL)
    if spec is None:
        return False
    batch: List[Dict[str, Any]] = []
    for call in state.pending_calls[:MAX_PARALLEL_DELEGATES]:
        if str(call.get("name") or "") != DELEGATE_TOOL or call.get("decision"):
            break
        verdict = evaluate_policy(
            db,
            workspace_id=run.workspace_id,
            user_id=run.created_by,
            spec=spec,
            scope=scope,
            mode=approval_mode_for_run(db, run, scope=scope, settings=settings),
        )
        if verdict.policy != "allow" or verdict.by_mode:
            break
        batch.append(call)
    if len(batch) < 2:
        return False

    # Serial bookkeeping, first half: the same rows and events
    # `execute_agent_tool_call` would write, in queue order.
    records: List[AgentToolCall] = []
    parsed_args: List[Dict[str, Any]] = []
    for call in batch:
        raw_arguments = str(call.get("arguments") or "{}")
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
            if not isinstance(arguments, dict):
                raise ValueError
        except (ValueError, TypeError):
            arguments = {}
        parsed_args.append(arguments)
        record = AgentToolCall(
            workspace_id=run.workspace_id,
            run_id=run.id,
            name=DELEGATE_TOOL,
            arguments_json=raw_arguments[:4000],
            call_id=str(call.get("call_id") or "")[:80],
        )
        db.add(record)
        db.flush()
        records.append(record)
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="tool.started",
            payload={
                "tool_call_id": record.id,
                "tool_name": DELEGATE_TOOL,
                "approved_by_mode": record.approved_by_mode,
            },
        )
    db.commit()

    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from ..database import SessionLocal

    def _work(arguments: Dict[str, Any]) -> Tuple[ToolResult, int, str]:
        started = time.monotonic()
        session = SessionLocal()
        error_text = ""
        try:
            result = _delegate(session, context, arguments)
        except Exception as exc:  # Parity with the serial path: bugs become
            session.rollback()  # model-visible errors, never a crashed turn —
            # and the ROW says failed, exactly as execute_agent_tool_call
            # records the identical failure when the same call runs alone.
            error_text = str(exc)[:1000]
            result = ToolResult(content=f"Error: tool failed: {str(exc)[:300]}")
        finally:
            session.close()
        return result, int((time.monotonic() - started) * 1000), error_text

    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _work, arguments)
            for arguments in parsed_args
        ]
        outcomes = [future.result() for future in futures]

    # Serial bookkeeping, second half — still queue order, so the transcript,
    # the events, and the evidence numbering are byte-identical to a serial
    # execution of the same calls.
    for call, record, (result, latency_ms, error_text) in zip(
        batch, records, outcomes, strict=True
    ):
        record.status = "failed" if error_text else "succeeded"
        if error_text:
            record.error = error_text
        record.latency_ms = latency_ms
        record.result_preview = (result.content or "")[:500]
        record.artifacts_json = json.dumps(result.artifacts)
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="tool.completed",
            payload={
                "tool_call_id": record.id,
                "tool_name": DELEGATE_TOOL,
                "status": record.status,
                "preview": record.result_preview,
                "artifacts": result.artifacts,
                "approved_by_mode": record.approved_by_mode,
            },
        )
        record_audit(
            db,
            workspace_id=run.workspace_id,
            actor_id=run.created_by,
            action="agent_tool.executed",
            resource_type="agent_tool_call",
            resource_id=record.id,
            detail={"tool": DELEGATE_TOOL, "status": record.status},
        )
        db.commit()
        _screen(
            db,
            run,
            kind="tool_output",
            text="\n\n".join(
                [result.content or "", *(item.excerpt for item in result.evidence)]
            ),
            settings=settings,
        )
        state.input_items.append(
            {
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or ""),
                "output": _render_result(result, evidence_offset=len(state.evidence)),
            }
        )
        state.evidence.extend(result.evidence)
    del state.pending_calls[: len(batch)]
    return True


#: The event the steer endpoint writes and `_absorb_steering` reads. One name,
#: defined beside the reader, imported by the writer and the tests.
STEER_REQUESTED = "run.steer"


def _steering_pending(db: Session, run: Run, state: LoopState) -> bool:
    """Whether unabsorbed steer events exist, without absorbing them — the
    finish-time check needs to know before deciding what order to append."""
    return (
        db.scalar(
            select(RunEvent.id)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == "run.steer",
                RunEvent.sequence > state.steered_sequence,
            )
            .limit(1)
        )
        is not None
    )


def _absorb_steering(db: Session, run: Run, state: LoopState) -> int:
    """Fold user guidance sent mid-run into the model's next call.

    Steering arrives as `run.steer` events; the per-run `sequence` is the
    cursor (persisted in LoopState), so a park/resume neither replays a note
    nor drops one sent while parked. Only called at the outer-loop checkpoint
    beside the cancel check — never inside `_drain_pending`, where a user item
    would split a function_call from the output the API requires adjacent.
    """
    rows = db.scalars(
        select(RunEvent)
        .where(
            RunEvent.run_id == run.id,
            RunEvent.event_type == "run.steer",
            RunEvent.sequence > state.steered_sequence,
        )
        .order_by(RunEvent.sequence)
    ).all()
    absorbed = 0
    for event in rows:
        try:
            content = str(json.loads(event.payload_json).get("content") or "")
        except ValueError:
            content = ""
        if content.strip():
            state.input_items.append(
                {"role": "user", "content": f"[The user adds, mid-task]: {content}"}
            )
            absorbed += 1
        state.steered_sequence = event.sequence
    return absorbed


def _drain_pending(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    scope: PolicyScope,
    settings: Settings,
) -> Optional[Outcome]:
    """Run queued calls until one needs approval. None means the queue emptied."""
    while state.pending_calls:
        db.refresh(run)
        if run.cancel_requested:
            return _cancelled(db, run, state)
        if _delegate_parallel_batch(
            db,
            run,
            state,
            registry=registry,
            context=context,
            scope=scope,
            settings=settings,
        ):
            continue
        call = state.pending_calls[0]
        name = str(call.get("name") or "")
        spec = registry.get(name)
        decision = call.get("decision")
        # A call arriving with a decision already on it was decided by a person
        # at `POST /api/agent-tool-calls/{id}/decision`, which stamped the row
        # itself. Nothing left for this path to attribute.
        decided_by = ""
        if decision is None:
            mode = approval_mode_for_run(db, run, scope=scope, settings=settings)
            verdict = evaluate_policy(
                db,
                workspace_id=run.workspace_id,
                # Whose grants apply: the member whose turn this is. A standing
                # allow another member clicked is not an answer they gave on this
                # person's behalf.
                user_id=run.created_by,
                spec=spec,
                scope=scope,
                mode=mode,
            )
            if verdict.policy == "ask" and spec is not None:
                # `guardian_may_review` is the policy's provenance ruling: only
                # the tool's own default ask, org silent, no standing row. The
                # helper's checks are the second lock, not the first.
                if verdict.guardian_may_review and _guardian_clears(
                    db,
                    run,
                    state,
                    spec=spec,
                    call=call,
                    context=context,
                    scope=scope,
                    mode=mode,
                    settings=settings,
                ):
                    decision = "approved"
                    decided_by = mode_decider(GUARDIAN)
                else:
                    return _park_for_approval(db, run, state, call, spec, context)
            else:
                decision = "denied" if verdict.policy == "deny" else "approved"
                decided_by = mode_decider(verdict.by_mode)
        result = execute_agent_tool_call(
            db,
            run,
            registry,
            context,
            name,
            _amended(
                _sanitized_arguments(name, str(call.get("arguments") or "{}")),
                call.get("amendment"),
            ),
            existing_id=call.get("tool_call_id"),
            denied=decision == "denied",
            decided_by=decided_by,
        )
        # Tool/MCP output is untrusted content re-injected as function_call_output
        # — screened before it is folded in, so a hit escalates the *next* call in
        # this same queue (approval_mode_for_run is re-read per call above). Both
        # halves the model reads are screened: the rendered content AND the tool's
        # evidence excerpts (extended into state.evidence below and spliced by
        # _render_result), so a tool that hides an injection in an excerpt rather
        # than the body does not slip past.
        _screen(
            db,
            run,
            kind="tool_output",
            text="\n\n".join(
                [result.content or "", *(item.excerpt for item in result.evidence)]
            ),
            settings=settings,
        )
        state.input_items.append(
            {
                "type": "function_call_output",
                "call_id": str(call.get("call_id") or ""),
                "output": _render_result(result, evidence_offset=len(state.evidence)),
            }
        )
        state.evidence.extend(result.evidence)
        state.pending_calls.pop(0)
    return None


def _apply_web_search(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    response: Any,
    text: str,
    settings: Settings,
) -> str:
    """Fold this step's hosted-search results into the turn; return its answer text.

    Everything happens inside the step that produced it. The sources join
    `state.evidence`, so `_finish_run` validates and reports them with the same
    call that handles retrieved passages; the `[n]` markers go into the text
    before it is ever treated as an answer, so the validator sees the same string
    the user will; and the trail event is written now — long before
    `run.completed`, which stream consumers stop reading at.
    """
    outcome = harvest(
        response,
        text,
        settings=settings,
        evidence_offset=len(state.evidence),
        known=web_numbers(state.evidence),
    )
    state.evidence.extend(outcome.evidence)
    if outcome.happened:
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="web_search.completed",
            payload=outcome.event_payload(),
        )
        db.commit()
    # The harvested excerpts are untrusted content spliced into the turn. Screen
    # them too — the weakest vector (the provider already ingested the page text
    # model-side and the excerpt is a slice of the model's own answer), but a
    # source class exempt from the screen is a gap, so it is covered for
    # completeness.
    if outcome.evidence:
        _screen(
            db,
            run,
            kind="web_search",
            text="\n\n".join(item.excerpt for item in outcome.evidence),
            settings=settings,
        )
    return anchor_citations(text, outcome.anchors)


@dataclass(frozen=True)
class AgentDirectives:
    """What the run's agent contributes to a turn: its voice, and its tools.

    Resolved once per entry into the loop — start and both resume doors — so a
    turn that parks resumes with the same instructions and the same registry
    subset it began with, whatever has happened to the Agent row in between.
    """

    instructions: str
    #: None means the whole registry. A set — empty included — is the agent's
    #: provisioned subset, applied as `build_registry(..., allowed=)`.
    allowed: Optional[frozenset[str]]


def _space_id_for(db: Session, run: Run) -> str:
    """The run's space, for the ToolContext — "" for every failure, and "" by
    construction for workflow, cron and subject runs."""
    return spaces.space_id_for_conversation(
        db, workspace_id=run.workspace_id, conversation_id=run.conversation_id
    )


def resolve_directives(db: Session, run: Run) -> AgentDirectives:
    """`run.agent_id` → the instructions and tool subset this turn runs under.

    Fallbacks are deliberate and total: a missing agent row or blank
    instructions yields the stock `CHAT_INSTRUCTIONS`, and an allowlist that
    does not parse as a JSON list yields None (all tools, still policy-gated)
    — a corrupt row must degrade to default behaviour, never to a failed turn.
    `Agent.enabled` is ignored here on purpose: disabling gates the *selection*
    of an agent for new runs (chat resolution, workflow validation), not the
    resumption of a run already carrying its id.
    """
    agent = db.get(Agent, run.agent_id) if run.agent_id else None
    if agent is not None and agent.workspace_id != run.workspace_id:
        # Runs are only ever created with a workspace-checked agent id, so this
        # is belt-and-braces — but a cross-tenant prompt is the one mistake this
        # function must be structurally unable to make.
        agent = None
    instructions = CHAT_INSTRUCTIONS
    allowed: Optional[frozenset[str]] = None
    if agent is not None:
        if agent.instructions.strip():
            instructions = agent.instructions
        raw = agent.allowed_tools_json
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                allowed = frozenset(str(item) for item in parsed)
    # A space's standing instructions compose with the agent's voice rather
    # than replacing it — only an agent replaces the base — and sit before the
    # skill splice so the most turn-specific layer stays last. Trusted like
    # `Agent.instructions` (member-authored through an authenticated PATCH),
    # so not `_screen`ed. `for_run` degrades every failure — missing
    # conversation, deleted space, cross-workspace id, blank text — to None or
    # "" here: no injection, never a failed turn.
    space = spaces.for_run(db, run)
    if space is not None and space.instructions.strip():
        instructions = f"{instructions}\n\n{spaces.space_block(space)}"
    # A skill invoked for this turn is spliced onto the agent's voice, not in
    # place of it: same instruction path, resolved once per loop entry, so a
    # turn that parks and resumes re-injects the identical body. A deleted skill
    # renders to "" and degrades to no injection, exactly like the missing agent.
    if run.skill_id:
        injected = skills.render_for_run(db, run)
        if injected:
            instructions = f"{instructions}\n\n{injected}"
    # Last: what everyone ELSE is doing right now — runs in flight and cards
    # under claim — so this turn routes around work already in hand instead of
    # duplicating it. "" in a quiet workspace, which is the common case.
    # Re-resolved on every loop entry like the layers above, so a turn that
    # parks an hour resumes seeing the workspace as it is, not as it was.
    awareness = coworking.digest_block(db, run=run)
    if awareness:
        instructions = f"{instructions}\n\n{awareness}"
    return AgentDirectives(instructions=instructions, allowed=allowed)


def _registry_for(
    db: Session,
    context: ToolContext,
    subject: Optional[subjects.Subject],
    directives: AgentDirectives,
    settings: Settings,
) -> Dict[str, ToolSpec]:
    """The tools this turn may see: the agent's provisioned subset, narrowed
    again to the families its subject is about.

    Both narrowings are applied HERE, at registry construction, and the ordering
    is the security property rather than an implementation detail. A tool outside
    the subject's set is *absent* from the turn — the model is never offered it,
    and `execute_agent_tool_call` answers "unknown tool" if one is somehow
    named — so no policy answer can reach it. `auto_writes` is permission to skip
    *asking*; it was never permission to widen the registry. Run the filter after
    the policy question instead and a document panel's bypass would hand the
    model `fs_delete`.

    That is also the blast-radius argument for scoping at all: a document thread
    that can call `fs_delete` can destroy a project the user is not looking at,
    from a panel whose visible subject is a paragraph of prose.

    `DEV_UNRESTRICTED_AGENT` drops the *subject* narrowing only, and only in
    development — `config._guard_dev_unrestricted` makes the flag impossible to
    switch on anywhere else. The agent's own provisioned subset survives it: that
    subset is what makes an authored agent that agent, and a development mode
    that quietly changed which agent you were talking to would be developing
    against something other than the product.
    """
    allowed = directives.allowed
    if not settings.dev_unrestricted_agent:
        allowed = subjects.narrow(
            allowed,
            subjects.allowed_tools_for(db, context, subject.kind if subject else ""),
        )
    return build_registry(db, context, allowed=allowed)


#: Spliced onto the turn's instructions while its conversation is in plan mode,
#: the same way a skill's body is spliced on — and like a skill it is resolved
#: per loop entry, so a resume whose approval already lifted the mode rebuilds
#: without it.
PLAN_MODE_INSTRUCTIONS = (
    "Plan mode is on for this conversation. The user wants to review a plan "
    "before anything is changed. Research with the read-only tools available; "
    "do not attempt to create, edit, or delete anything — write-capable tools "
    "are withheld until the plan is approved. When you know enough to propose, "
    "call `exit_plan_mode` with the complete plan (as markdown) in the `plan` "
    "argument. The user reviews it there: approval turns plan mode off so the "
    "work can begin, and denial means revise the plan and propose again."
)


def _plan_narrowed(
    registry: Dict[str, ToolSpec], instructions: str, mode: ApprovalMode
) -> Tuple[Dict[str, ToolSpec], str]:
    """The turn's registry and instructions, adjusted for plan mode.

    Narrowing follows `_registry_for`'s rule that a tool a turn must not run is
    *absent*, not present-and-denied: a planning model is never offered a
    write-capable tool, so it plans around what it can see instead of burning
    iterations on refusals. `evaluate_policy`'s plan branch stays as the
    second, independent lock for the call that arrives anyway.

    `exit_plan_mode` is added here rather than shipped by a registry family
    because it is mode machinery, not a capability: an agent's provisioned
    subset must not be able to strip it (a plan mode with no exit is a locked
    room), and no other mode should ever offer it.
    """
    if mode != PLAN:
        return registry, instructions
    narrowed = {name: spec for name, spec in registry.items() if spec.read_only}
    narrowed[EXIT_PLAN_MODE] = exit_plan_mode_spec()
    return narrowed, f"{instructions}\n\n{PLAN_MODE_INSTRUCTIONS}"


def _advance(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    registry: Dict[str, ToolSpec],
    context: ToolContext,
    step: ModelStep,
    settings: Settings,
    scope: PolicyScope,
    instructions: str = CHAT_INSTRUCTIONS,
) -> Outcome:
    tools = _tool_payload(registry, settings)
    while True:
        blocked = _drain_pending(
            db,
            run,
            state,
            registry=registry,
            context=context,
            scope=scope,
            settings=settings,
        )
        if blocked is not None:
            return blocked
        if state.iteration >= MAX_ITERATIONS:
            raise RuntimeError("Agent loop exceeded the iteration budget")
        db.refresh(run)
        if run.cancel_requested:
            return _cancelled(db, run, state)
        # Mid-turn steering lands here, at the head of each iteration: after
        # the queue drained (so a steer never splits a function_call from its
        # output) and before the model is called (so it shapes the very next
        # step instead of the next turn).
        _absorb_steering(db, run, state)

        # The ceiling, checked here and nowhere else on this path: the last
        # statement before the expensive call. Every iteration, not once per
        # turn — a runaway loop is exactly a turn whose *sixth* step is the one
        # worth refusing, and a check outside the loop would wave it through.
        verdict = budget.evaluate(
            db,
            workspace_id=run.workspace_id,
            unattended=scope == WORKFLOW_SCOPE,
            settings=settings,
        )
        if not verdict.allowed:
            return _park_for_budget(db, run, state, verdict)

        final_round = state.iteration == MAX_ITERATIONS - 1
        buffer = DeltaBuffer(db, workspace_id=run.workspace_id, run_id=run.id)
        # The thinking trail's own lane: same buffering, distinct event type,
        # so the client can render it apart from the answer. Streamed live and
        # deliberately not persisted anywhere else — the trail is working
        # narration for the person watching, not part of the transcript.
        thinking_buffer = DeltaBuffer(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="thinking.delta",
        )
        response: Any = None
        incomplete = False
        for kind, value in step(
            state.input_items, [] if final_round else tools, instructions
        ):
            if kind == "delta":
                buffer.add(str(value))
            elif kind == "thinking":
                thinking_buffer.add(str(value))
            elif kind == "completed":
                response = value
            elif kind == "incomplete":
                response = value
                incomplete = True
        thinking_buffer.flush()
        buffer.flush()
        state.iteration += 1
        if response is None:
            raise RuntimeError("Model stream ended without a completed response")
        state.text_so_far += _apply_web_search(
            db, run, state, response=response, text=buffer.text, settings=settings
        )
        if incomplete:
            # The provider ended the stream early (usually the output-token
            # ceiling) but everything streamed so far is real. Discarding it
            # for an error was the old behavior and the worse trade: finish
            # the turn honestly with what exists. Any function calls on the
            # truncated response are dropped — a cut-off argument list is not
            # a call worth executing. Only a stream that produced nothing at
            # all is still an error.
            reason = str(
                getattr(
                    getattr(response, "incomplete_details", None), "reason", ""
                )
                or ""
            )
            answer = state.text_so_far.strip()
            if not answer:
                raise RuntimeError(
                    "Model stream ended early: " + (reason or "response.incomplete")
                )
            note = (
                "it hit the output limit"
                if reason == "max_output_tokens"
                else "the model stopped early"
            )
            # No "say continue" advice on purpose: the next turn's transcript
            # truncates long messages, so a continue could not reliably see
            # what it was continuing — advertising it would promise more than
            # the product keeps. The note itself stays: the reader must know
            # the answer is not the whole answer.
            return Done(
                answer=answer + f"\n\n*(The answer was cut short — {note}.)*",
                evidence=state.evidence,
            )

        calls = [
            item
            for item in (response.output or [])
            if getattr(item, "type", None) == "function_call"
        ]
        if not calls:
            # The finish-time steering check: a note typed during the FINAL
            # model call passed the route's 409 gate (the run was still
            # running) but arrived after the last absorb checkpoint. Without
            # this, that note would be silently ignored for the turn it aimed
            # at — accepted with a 202, folded into nothing. Absorbing here
            # and looping once more answers it in this turn; the answer so far
            # is kept as context, exactly as a tool round would keep it.
            # Chronology matters: the answer is extended FIRST, then the note
            # absorbed, so the next call reads [answer, note] — the note came
            # after the answer, and shown the other way round the model could
            # conclude the answer already addressed it. Hence the peek before
            # either mutation.
            if state.iteration < MAX_ITERATIONS and _steering_pending(
                db, run, state
            ):
                state.input_items.extend(
                    _serialize_item(item) for item in response.output or []
                )
                _absorb_steering(db, run, state)
                if state.text_so_far:
                    state.text_so_far += "\n\n"
                    # The live view builds the message from message.delta
                    # events; the joiner has to travel the same lane or the
                    # two segments render run-together until the completed
                    # message replaces them.
                    append_event(
                        db,
                        workspace_id=run.workspace_id,
                        run_id=run.id,
                        event_type="message.delta",
                        payload={"delta": "\n\n"},
                    )
                    db.commit()
                continue
            answer = (state.text_so_far or response.output_text or "").strip()
            if not answer:
                raise RuntimeError("Model returned an empty response")
            return Done(answer=answer, evidence=state.evidence)
        state.input_items.extend(_serialize_item(item) for item in response.output)
        state.pending_calls = [
            {
                "call_id": getattr(call, "call_id", ""),
                "name": getattr(call, "name", ""),
                "arguments": getattr(call, "arguments", "") or "{}",
            }
            for call in calls
        ]


def _finish(db: Session, run: Run, outcome: Outcome) -> Optional[AgentResult]:
    if isinstance(outcome, Done):
        run.agent_state_json = None
        db.commit()
        return AgentResult(answer=outcome.answer, evidence=outcome.evidence)
    return None


def run_agent_turn(
    db: Session,
    run: Run,
    *,
    evidence: List[Evidence],
    transcript: Optional[List[Any]] = None,
    memory_context: str = "",
    settings: Optional[Settings] = None,
    model_step: Optional[ModelStep] = None,
    workflow_node: bool = False,
) -> Optional[AgentResult]:
    """Start a turn. None means the run parked for approval or was cancelled.

    `workflow_node` is asserted by the workflow executor, which borrows this
    function for an `agent` node. Nobody else may start a turn on a run that
    backs a workflow: `recover_durable_work` re-queues runs left `running` by an
    unclean stop, and without this the recovery sweep would staple a chat turn
    onto an automation. Failing loudly is the correct outcome — the workflow run
    is the record that matters, and it can be re-run.

    The guard keys on the *actual* danger — a WorkflowRun backing the run — not
    on policy scope. A cron task run is at `workflow` scope (via `cron_id`) yet
    has no DAG: a fresh turn is its correct execution and recovery, so it must be
    allowed through here exactly as a chat run is.
    """
    settings = settings or get_settings()
    scope = policy_scope_for_run(db, run)
    backs_workflow = (
        db.scalar(select(WorkflowRun.id).where(WorkflowRun.run_id == run.id)) is not None
    )
    if not workflow_node and backs_workflow:
        raise RuntimeError("This run belongs to a workflow; start it through the executor")
    subject = subjects.resolve(db, run)
    context = subjects.tool_context(run, subject, space_id=_space_id_for(db, run))
    directives = resolve_directives(db, run)
    registry = _registry_for(db, context, subject, directives, settings)
    registry, instructions = _plan_narrowed(
        registry,
        directives.instructions,
        approval_mode_for_run(db, run, scope=scope, settings=settings),
    )
    state = LoopState(
        input_items=[
            {
                "role": "user",
                "content": _openai_input(
                    run.prompt,
                    evidence,
                    transcript,
                    memory_context,
                    subject_context(subject),
                ),
            }
        ],
        evidence=list(evidence),
    )
    # Bound here rather than in the caller because this is the innermost frame
    # that knows all five ids, and because both callers — the chat worker and the
    # workflow executor — reach the model through it.
    with usage_scope(
        workspace_id=run.workspace_id,
        run_id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.created_by,
        agent_id=run.agent_id or "",
        operation=_billing_operation(scope),
    ):
        # The turn-start injection points: the retrieved passages, the open
        # document, and the long-term memory are all content the user did not
        # type. Screened once here, before the first model call, and inside the
        # usage scope so the builtin classifier's own spend is billed to this
        # run. A hit flags the run; enforce then reads that flag on every tool
        # call below. Resumes do not re-screen — the flag is a run event that
        # persists across a park/resume. The user's own `run.prompt` is trusted
        # input and is deliberately not screened as an attack on itself.
        if settings.screen_enabled:
            _screen(
                db,
                run,
                kind="evidence",
                text="\n\n".join(item.excerpt for item in evidence),
                settings=settings,
            )
            # Named "document" still: it is the event kind every dashboard,
            # audit query and test in this repo already reads, and what it has
            # always meant is "the content this turn spliced in from the thing
            # the user has open". That is now a file or a spec as often as a
            # document, and renaming the kind would silently empty every
            # existing query for the sake of a more accurate word.
            _screen(
                db,
                run,
                kind="document",
                text=subject.context if subject else "",
                settings=settings,
            )
            _screen(db, run, kind="memory", text=memory_context, settings=settings)
        _enforce_org_bounds(db, run, settings)
        outcome = _advance(
            db,
            run,
            state,
            registry=registry,
            context=context,
            step=model_step or _default_model_step(settings, run, list(evidence)),
            settings=settings,
            scope=scope,
            instructions=instructions,
        )
    return _finish(db, run, outcome)


def resume_agent_turn(
    db: Session,
    run: Run,
    *,
    tool_call_id: str,
    decision: str,
    amendment: Optional[Dict[str, Any]] = None,
    inputs: Optional[Dict[str, Any]] = None,
    settings: Optional[Settings] = None,
    model_step: Optional[ModelStep] = None,
) -> Optional[AgentResult]:
    """Continue a parked turn once the user has decided on the proposed call.

    `amendment` is how a reviewer approves *part* of a call. It is merged into
    the arguments on the way to the executor and deliberately not written over
    `AgentToolCall.arguments_json`, which stays the record of what the model
    asked for; what the human authorised is on the audit row.

    Also the door a *workflow* comes back through. ADR 0007 chose one park and
    one resume over two state machines that have to agree, so a workflow node
    that needs approval writes the same `AgentToolCall` against the same `Run`,
    and `POST /api/agent-tool-calls/{id}/decision` lands here for both. The
    branch below is where the two part company:

    - No `agent_state_json` means no model turn was in flight, so the parked call
      belongs to a workflow *tool* or *manual* node and the executor owns the
      resume — `inputs` threads a manual node's submitted values to it.
    - With state, an *agent* node inside a workflow parked mid-turn. The turn
      finishes here, but the graph has not, so the executor takes the answer as
      that node's output and carries on rather than the run being completed.

    Imported inside the function because the executor imports this module — it
    is built on this machinery, which is the point.
    """
    from .workflows import executor as workflow_executor

    settings = settings or get_settings()
    workflow_run = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == run.id))
    if workflow_run is not None and not run.agent_state_json:
        workflow_executor.resume_after_decision(
            db,
            workflow_run,
            tool_call_id=tool_call_id,
            decision=decision,
            inputs=inputs,
        )
        return None
    if not run.agent_state_json:
        raise RuntimeError("Run has no saved agent state to resume")
    state = LoopState.from_json(run.agent_state_json)
    if not state.pending_calls:
        raise RuntimeError("Run has no pending tool call to resume")
    head = state.pending_calls[0]
    if head.get("tool_call_id") != tool_call_id:
        raise RuntimeError("Decision does not match the parked tool call")
    head["decision"] = decision
    if amendment and decision == "approved":
        head["amendment"] = amendment

    record = db.get(AgentToolCall, tool_call_id)
    if record is not None:
        record.decided_at = utcnow()
    run.status = "running"
    run.paused_reason = ""
    run.agent_state_json = None
    db.commit()
    return _continue(
        db,
        run,
        state,
        settings=settings,
        model_step=model_step,
        workflow_run=workflow_run,
    )


def resume_after_budget(
    db: Session,
    run: Run,
    *,
    settings: Optional[Settings] = None,
    model_step: Optional[ModelStep] = None,
) -> Optional[AgentResult]:
    """Continue a turn parked on the spend ceiling, once the limit was raised.

    The mirror of `resume_agent_turn` and deliberately thinner, because there is
    no decision to apply: the turn stopped *before* asking the model, so its
    `pending_calls` are empty and the state resumes exactly where it stopped.

    Nothing here re-checks the ceiling, and that is not an omission. `_advance`
    evaluates it at the top of every iteration, so a run released while still
    over the limit simply parks again on the same evidence — one rule, one place,
    and no way for the release path to hold a more generous opinion than the
    enforcement path.
    """
    settings = settings or get_settings()
    if not run.agent_state_json:
        raise RuntimeError("Run has no saved agent state to resume")
    state = LoopState.from_json(run.agent_state_json)
    workflow_run = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == run.id))
    run.status = "running"
    run.paused_reason = ""
    run.agent_state_json = None
    db.commit()
    return _continue(
        db,
        run,
        state,
        settings=settings,
        model_step=model_step,
        workflow_run=workflow_run,
    )


def _continue(
    db: Session,
    run: Run,
    state: LoopState,
    *,
    settings: Settings,
    model_step: Optional[ModelStep],
    workflow_run: Optional[WorkflowRun],
) -> Optional[AgentResult]:
    """Walk a restored `LoopState` to its next stop, and hand the outcome on.

    Shared by both resume doors so they cannot drift: the same registry, the same
    policy scope, the same billing attribution, and the same rule about who owns
    the terminal state.
    """
    from .workflows import executor as workflow_executor

    subject = subjects.resolve(db, run)
    context = subjects.tool_context(run, subject, space_id=_space_id_for(db, run))
    directives = resolve_directives(db, run)
    registry = _registry_for(db, context, subject, directives, settings)
    scope = policy_scope_for_run(db, run)
    registry, instructions = _plan_narrowed(
        registry,
        directives.instructions,
        approval_mode_for_run(db, run, scope=scope, settings=settings),
    )
    if any(call.get("name") == EXIT_PLAN_MODE for call in state.pending_calls):
        # The approval that resumes this turn may itself have lifted plan mode,
        # rebuilding the full registry above — but the parked `exit_plan_mode`
        # call at the head of the queue still needs its spec to execute rather
        # than land on "unknown tool".
        registry.setdefault(EXIT_PLAN_MODE, exit_plan_mode_spec())
    with usage_scope(
        workspace_id=run.workspace_id,
        run_id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.created_by,
        agent_id=run.agent_id or "",
        operation=_billing_operation(scope),
    ):
        # Re-checked on resume, not just at turn start: a parked run comes back
        # minutes or hours later, and an org that tightened its bounds in between
        # meant it to apply to work already in flight.
        _enforce_org_bounds(db, run, settings)
        outcome = _advance(
            db,
            run,
            state,
            registry=registry,
            context=context,
            step=model_step or _default_model_step(settings, run, state.evidence),
            settings=settings,
            scope=scope,
            instructions=instructions,
        )
    if workflow_run is not None:
        # The agent node's turn is over; the graph is not. Returning None keeps
        # `services/runs.resume_run` from completing a run the next node still
        # needs, and the executor writes the terminal state when the DAG ends.
        workflow_executor.resume_after_agent_turn(db, workflow_run, outcome=outcome)
        return None
    return _finish(db, run, outcome)
