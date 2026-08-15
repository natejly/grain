# 0010 — Personal scope: whose memory, and whose permission

## Status

Accepted. Extends ADR 0002 (the graph projection is workspace-wide) and ADR 0007
(`chat|workflow` policy scope). Does not supersede either.

## Context

Until b07f083 a workspace had exactly one member, so "the workspace's memory" and
"my memory" named the same rows and nothing distinguished them. Invitations
removed that coincidence. Two people can now share a workspace, and every piece
of state the agent carries is addressed by `workspace_id` alone.

Two consequences of that are already live, and neither is hypothetical.

**A personal thread's memory is not personal.** `Conversation.shared` defaults to
`False` and its own docstring is unambiguous — *"a personal thread is visible only
to its creator within the workspace"*. The API enforces that: `conversations.
run_activity_visible` is consulted before a member may even see, let alone decide,
a tool call parked on someone else's thread. But `write_conversation_memory` takes
what the extractor learned from that same private thread and writes it with
`workspace_id` and no owner, after which `recall()` filters on
`workspace_id, status` and `GET /api/memory` filters on `workspace_id, status`.
So the thread is private and its contents are not. A member cannot read my
conversation and can read the facts extracted from it — including, in the
injected prompt of their own next turn, without asking for it.

**One person's correction retires another person's fact.** Supersession is keyed
on `UniqueConstraint(workspace_id, kind, normalized_key)`. A claim key names a
slot, and there is one slot per workspace. When I tell the agent the standup
moved to Thursday, `_retire` marks your Tuesday row `superseded` and rewrites its
`normalized_key` so it can never be recalled again. That is the correct behaviour
for a fact about the workspace and the wrong behaviour for a fact about a person,
and nothing in the row distinguishes the two.

**One person's "always allow" authorises everybody.** `tool_policies` is keyed on
`(workspace_id, tool_name, scope)`. ADR 0007 already split the *situation* a
grant was made in, because a grant made while typing is not an answer to "may
this run unattended at 3am". It did not split *who made it*, because at the time
there was only ever one person to be. Clicking "always allow" on a `send_email`
card now removes the approval park for every member of the workspace, and the
approval park is the only containment prompt injection has to get past.

The framing worth stealing from YC's QM harness is that people customise the
agent to be *theirs* and still work with it collaboratively. Both halves matter.
A design where everything becomes personal is not a fix — it is a workspace that
has stopped being a workspace.

## Decision

### What becomes personal, and what does not

| State | Verdict | Why |
|---|---|---|
| `memory_items` | **per-person, inherited from the conversation** | See below. The one piece of state where both live problems land. |
| `tool_policies` | **per-person, with a shared tier** | A standing grant is authority; authority belongs to whoever accepted it. |
| Conversations | already personal | `Conversation.shared`, default `False`. This ADR follows it rather than adding a second opinion. |
| MCP credentials | already personal | ADR 0006: *"the credential belongs to a person, not to a workspace"*, `mcp_oauth_tokens` is per-user. |
| Dashboard pins | already personal | `dashboard_pins.user_id`. The addressing precedent. |
| Documents, sources, projects, folders | **stay shared** | Deferred, deliberately — see below. |
| Crons, workflows | **stay shared** | Deferred. |
| Sandbox sessions | **stay shared** | Deferred. |
| Graph projection | **stays workspace-wide and shared-only** | ADR 0002. See "the projection loses personal memory". |

The pattern in the first column is not "everything gets an owner". It is: state
that already had a per-person answer keeps it, state whose per-person answer is
currently *missing and causing a leak* gets one, and everything else waits for a
change of its own with a reason of its own.

### A memory inherits the visibility of the conversation it was learned in

This is the substantive decision and it deserves its argument spelled out,
because three other rules were available and each is worse.

*Everything personal* stops the workspace learning anything collectively, which
is the half of the product this ADR is not willing to trade away.

*Everything shared, with an opt-in* is the status quo plus a checkbox nobody will
find, and it leaves the leak open by default.

*The `kind` column decides* — `preference` is personal, `fact` is shared — reads
well and is unimplementable. `_upsert_item` already documents that `kind` is
per-turn model output with nothing pinning it per claim, and that scoping a
lookup by it let one claim sit under two kinds and drove the stale-served rate to
100%. A field that flaps between turns cannot carry an ownership boundary: the
same claim would land personal on Tuesday and shared on Wednesday.

So: **`Conversation.shared` decides.** A memory extracted from, or remembered in,
a personal thread is owned by that thread's creator. A memory from a shared
thread is the workspace's. This introduces no new judgement, no new field for a
model to get wrong, and no new UI — the person already made this decision, with
intent, when they did or did not share the thread. It also makes the leak
impossible by construction rather than by a filter someone must remember to
write: the memory is exactly as visible as the conversation it came from, which
is the invariant that was silently false before.

The rolling conversation summary (`kind == "summary"`) is the exception and stays
shared. It is keyed on `conversation_id` and is already unreachable to anyone who
cannot see that conversation, because `_pinned_summary` matches on the
conversation id a turn is running in. Giving it an owner would gain nothing and
would put a second owner on the one row that is already scoped by something
stronger.

### How a scope is addressed

One column, one shape, both tables:

```
owner_id: Mapped[str] = mapped_column(String(36), default="", server_default="")
```

`""` means shared. Any other value is a user id. The column joins the existing
unique key rather than replacing anything:
`(workspace_id, owner_id, kind, normalized_key)` and
`(workspace_id, owner_id, tool_name, scope)`.

**It is a sentinel and not a nullable foreign key, and that is a correctness
requirement rather than a style preference.** `NULL` is the obvious spelling of
"unowned" and it silently destroys the constraint that everything here rests on.
Both SQLite and Postgres treat NULLs as distinct inside a unique index, so
`(ws, NULL, 'fact', 'api|deploy_host')` does not conflict with another row
identical to it. The workspace's shared memory would lose supersession entirely:
two runs would leave two live rows on one claim key, `_retry_on_claim_collision`
would never fire because no IntegrityError would be raised, and the stale-served
rate would go back to where evaluate_memory.py found it before claim keys
existed. ADR 0005 chose `NULL` for `sandbox_sessions.slot_index` *because* NULLs
never collide, which is what lets a session release its slot. Same fact about the
same two engines, opposite requirement, opposite answer. An empty string collides
with an empty string on both.

The cost is that `owner_id` cannot be a foreign key, so a deleted user leaves
orphaned rows. That is already the established shape here for exactly this
reason — `MemoryItem.run_id`, `ToolPolicy.created_by`, `Agent.created_by` and a
dozen others are `String(36)` with a `""` default and no constraint — and user
deletion is not a flow this app has.

It is named `owner_id` rather than `user_id` deliberately, even though
`dashboard_pins.user_id` is the precedent being followed. Every row in
`dashboard_pins` is personal, so its column is total and is a real foreign key.
`memory_items.owner_id` is partial: it has an unowned value, it is not a foreign
key, and calling it `user_id` would promise both. The consistency being preserved
is the shape — one `String(36)` column, `""` for shared, joined into the unique
key — applied identically to the two tables this ADR touches.

**The owner is never a request field.** It is taken from the authenticated actor
or from the conversation's `created_by`. There is no endpoint that accepts an
owner id from a caller, for the same reason ADR 0005 has no code path that
accepts a provider-side sandbox id from one: a client-supplied owner is a
cross-person read waiting to be typed.

### Existing rows: shared for memory, attributed for policies

These get different answers because the evidence available is different, and the
direction a wrong answer fails in is different. That asymmetry is the whole
justification.

**Every existing `memory_items` row becomes shared** (`owner_id = ''`). No
attribution is attempted. A row's `run_id` names one run, but `importance` counts
how many runs re-touched it and `message_ids_json` spans them, so a memory
reinforced by three members across four conversations has no honest owner —
picking the earliest run's author would be a guess, recorded as if it were a
fact. Worse, it fails in the losing direction: attributing a row to me *removes*
it from your recall, which is a memory that used to be answerable and now is not.
That is data-loss-shaped, and it would be invisible — no error, just a worse
answer. Shared is the status quo's exact behaviour, so this migration changes
what no member sees, and it matches how migration 0037 backfilled every existing
conversation to `shared = True` for the identical reason: nothing that is
member-visible today may become hidden.

**Every existing `tool_policies` row becomes personal to whoever made it** —
literally `owner_id = created_by`, with no `CASE` expression, because
`_upsert_policy` has always stamped `created_by` with the acting user and the
sentinel for "we do not know" is the empty string that column already holds for
unattributed rows. Nothing is guessed: `created_by` is recorded fact about who
clicked, not an inference from adjacent data. And the direction of failure is
inverted from memory's. Narrowing a grant does not delete anything; it means
another member is asked to approve a write they were not asked about yesterday.
That is fail-closed, it is recoverable in one click, and it is the exact security
outcome this ADR exists to produce — applied to the grants that motivated it,
rather than only to grants made after it.

### Retrieval semantics: mine and the workspace's, mine wins

A turn recalls **shared memory plus the caller's own**, never another member's.
That is QM's answer and it is the right one: a workspace where I cannot see what
the team knows is not collaborative, and one where you can see what I told it in
private is not mine.

The candidate predicate is `owner_id IN ('', viewer_id)`, and it goes through
`_active()` — the same chokepoint `status == 'active'` goes through, for the same
reason. `recall()` issues three independent candidate queries (lexical, vector,
pinned summary) plus a final hydrate; a scope filter added to three of the four
is a leak, and the existing module already proves by test that no reader spells
liveness out for itself. Scope is now covered by that same proof.

The predicate has one useful property that removed the need for a second mode:
with `viewer_id = ''` it collapses to `owner_id = ''`, which is exactly
"shared only". So the graph projection, which is workspace-wide and read by every
member, calls the same function with no viewer and cannot accidentally be given
one. And a call site that forgets to pass a user gets *less*, never more.

**Precedence is by claim, not by score.** When a personal row and a shared row
hold the same `normalized_key`, the personal row is served and the shared row is
dropped from the result. Not a scoring bonus: a bonus makes the personal row
*rank higher* while still handing the model both a claim and a contradicting
claim with nothing to tell them apart, which is precisely the failure
evaluate_memory.py exists to measure and calls STALE-SERVED. `normalized_key` is
the right key for this because it is already the identity of a claim slot —
the same key supersession retires on — so "my version of this claim" needs no new
concept.

Which gives the pair of rules worth stating together, because they are the answer
to "does my correction destroy yours":

- **Supersession is within a scope.** My correction retires my rows and the
  workspace's shared row is untouched, structurally, because `owner_id` is in the
  unique key and `_upsert_item` matches on it exactly.
- **Shadowing is across scopes, at read time.** I nonetheless see my version,
  because a personal row displaces a shared row on the same claim in my recall
  and in nobody else's.

Write-time isolation and read-time precedence are different mechanisms doing
different jobs, and collapsing them into one would cost either your data or my
correction.

### Policy resolution

`evaluate_policy` gains `user_id` and keeps its shape: decide the requested
scope, then fall back to the cross-scope `chat` deny, then the tool's own
default. Owner precedence is applied *inside* the first step:

- **A `deny` in the requested scope wins, whoever wrote it.** A shared deny beats
  my personal allow — otherwise granting myself an exemption from a workspace
  prohibition is one PUT away, and the escalation this ADR is meant to close
  would reopen sideways. A personal deny beats a shared allow because tightening
  is always permitted. This is the existing rule — *"a prohibition is not a
  grant"*, *"a deny stays denied, in every mode"* — extended along one more axis.
- **Otherwise the personal row decides**, then the shared row, then the fallback.

And **only an owner may write a shared grant**. Every "always allow" from an
approval card is personal, which is the change that makes the click safe: it
authorises the person who accepted the consequence and nobody else. A shared
grant is a deliberate act by someone with the role for it, via the existing
`require_owner` gate.

## What this defers, and why each is its own change

**Files, documents, sources, projects, folders.** Deferred, and the reason is the
same one that makes this ADR careful: *a shared workspace document that silently
became personal is a data-loss-shaped bug*, and documents are the state where
that would be most expensive and least reversible. They also already have a
sharing surface of their own — folders, projects, subject-scoped threads — so
adding `owner_id` beside it invites two systems disagreeing about who can read
one file. And the retrieval path would have to thread the viewer through
ingestion, chunking, citations and the graph, which is a larger change than this
one entire ADR.

**Crons and workflows.** Deferred. Making these personal changes *execution
identity*, not visibility: a 3am cron would have to run as somebody, and ADR
0007's approval model would need rebuilding around that. Personal tool policies
deliver the security half of the benefit now — a workflow already resolves policy
at `workflow` scope, and it will now resolve it against its creator — without
that rebuild.

**MCP credentials.** Already personal per ADR 0006. Nothing to do.

**Sandbox sessions.** Deferred. A session is ephemeral, reaped on idle, and its
row is already the sole authority per ADR 0005. Whose it is, is a quota and
lifecycle question rather than a privacy one, and it should be answered next to
the quota.

**The graph projection.** Deferred, and it is the one deferral that costs
something immediately rather than merely leaving things as they were — see the
first consequence below. `graph_entities` and `graph_edges` would take the same
`owner_id` this ADR gives the other two tables, but the projection would then
have to decide what a node mentioned in both a shared chunk and a personal
memory *is* — one node with two provenances, or two nodes — and answering that
badly is worse than the enrichment being absent.

## Consequences

- **Memory stops feeding the graph unless a thread is shared, and that is a
  bigger loss than it first sounds.** `GraphProjection` is unique per workspace
  and every member reads the same nodes, so it may only be built from shared
  rows — the alternative is a projection per member, which multiplies ADR 0002's
  rebuild cost by the member count and contradicts its "one rebuildable
  projection" premise outright. But threads are personal *by default*, so the
  ordinary case is now that a memory never reaches the graph at all: entities
  learned in conversation stop appearing as nodes, and `graph_lookup` and the
  recalled digest stop knowing about them, for everyone, until somebody clicks
  share. Documents and sources still feed it, and personal memories are still
  fully recalled by the lexical and vector paths — where the substance is; the
  digest is an enrichment — but this is a real capability narrowed, not a corner
  case, and it is the strongest argument for giving `graph_entities` the same
  `owner_id` next. `test_a_personal_threads_memory_stays_out_of_the_shared_graph`
  pins the behaviour so the decision cannot be lost by accident.
- **A single-member workspace sees no behaviour change at all**, which is worth
  saying because it is the case almost every existing workspace is in. Personal
  and shared are the same set when there is one person, and the migration writes
  every existing memory row shared regardless.
- **The migration does not retroactively fix a shared conversation's history.**
  Memory extracted from a thread that was personal *before* this shipped is now
  shared forever, because the rows carry no owner to recover. Only turns after
  0040 inherit their conversation's visibility. Anything already leaked is
  already leaked, and the remedy is the existing `DELETE /api/memory/{id}`.
- **Changing a thread from personal to shared does not re-scope its memories.**
  The owner is stamped at write time. This is deliberate — retroactively
  publishing memories on a share click would be a surprise in the direction that
  cannot be undone — but it means a thread shared late has memories split across
  two scopes, and the person who shared it will not be told.
- **A workspace owner can no longer see every memory the workspace holds.**
  `GET /api/memory` returns shared plus the caller's own, so nobody has a
  complete view. That is the point of the feature and it is also a real loss for
  administration and for compliance, where "produce everything the system knows
  about X" is now a question with no endpoint. Audit rows still record every
  write.
- **A standing personal grant is still standing write authority.** Making it
  personal narrows the blast radius from the workspace to one person; it does not
  make an "always allow" on `send_email` safe. ADR 0007's residual risk is
  reduced, not retired.
- **`resolve_policy` now needs a user, and the callers that have none are the
  interesting ones.** A workflow resolves against `workflow_run.created_by`. That
  is defensible — the automation acts for whoever built it — but it does mean a
  cron inherits its author's personal grants, so revoking a departing member's
  grants changes what their crons may do. That is arguably correct and is
  certainly surprising, so it is recorded here rather than discovered later.
