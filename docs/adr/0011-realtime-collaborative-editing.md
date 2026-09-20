# 0011 — Real-time collaborative editing: the road past last-write-wins

## Status

Proposed. **Decision: open — stage 1 ships now.** Stage 1 (the save
precondition and its conflict banner) is in this change; everything past it is
an option this ADR ranks but does not commit to. Extends the coworking presence
work; does not supersede ADR 0010 (documents stay workspace-shared).

## Context: what the collaboration layer actually is today

Grain's collaboration layer is a presence-and-lease system, not a concurrent
editing system, and the distinction is load-bearing for every option below.

One SSE connection per workspace (`GET /api/coworking/stream`) is fed by a
250ms server-side DB poll against SQLite. It emits durable `workspace_events`
replayed after a sequence cursor, plus diffed snapshots of runs-in-flight and
presence. There are no websockets and no pub/sub; the 250ms poll exists
*because* SQLite is the shipped backend, and every frame passes a per-viewer
visibility filter so personal-thread surfaces never leak.

Presence is a DB table — one row per (workspace, actor, surface), 15s
read-side TTL — heartbeated over plain HTTP POSTs with a 350ms client
throttle. A beat's `state_json` carries the caret offset, selection, typing
flag, a sanitized pointer, and *the entire live draft*, capped at 64KB. That
last item is what makes document "co-editing" work at all: it is follow-mode.
The newest editor's unsaved draft streams to everyone else's pane read-only; a
follower's first keystroke forks a private copy; two dirty drafts get an
explicit clash banner that says, honestly, whoever saves last wins.

Underneath, `PUT /api/documents/{id}` replaces the whole content column and
snapshots the prior text into `DocumentVersion`. Until this change it carried
no precondition, so a concurrent save was recoverable (the snapshot exists)
but never prevented. Stage 1 closes exactly that: the PUT now accepts an
optional `base_version_id`, a stale base answers 409 with the current head,
and the editor shows a reload-theirs / overwrite-anyway banner. The head token
is the newest `DocumentVersion` id — an honest save-generation counter that
already existed, which is why stage 1 needs no migration.

Two more facts constrain everything below. First, the agent edit-proposal
interlock is a **second concurrency regime over the same Document row**: a
pending agent proposal pauses every human write path (save, Cmd+S, restore)
until reviewed, and its diffs are computed against a base that must not drift.
Any real merge machinery has to reconcile with it, not route around it.
Second, cursors and carets are plain character offsets against
possibly-divergent text, clamped to length — there are no anchors that survive
a concurrent edit, and end-to-end caret latency is roughly 0.5–1s (350ms
client throttle + 250ms poll + snapshot diff).

## Constraints any option must satisfy

These are shipped invariants, most of them pinned by tests. An option that
quietly breaks one is not cheaper, it is wrong.

- **`report()` replaces a surface's state.** A save retires the live draft by
  replacing it, which is why the pointer lives on its own 90ms channel merged
  at send time. Any new sync protocol either keeps this replace semantics for
  presence or moves drafts off presence entirely; it cannot make beats
  merge-y without breaking draft retirement.
- **A presence clear bypasses the throttle** and cancels pending timers, or
  ghost cursors persist for the 15s TTL. New mounts and new transports keep
  this.
- **The per-viewer visibility gate must be re-implemented in any new
  fan-out.** Personal threads never leak through presence, runs, or digests
  (`test_coworking.py` pins the gate). A websocket or CRDT provider that
  broadcasts room-wide reintroduces the leak by default.
- **Instruction-equality asserts forbid instruction-side signaling.** The
  shared-workspace fixtures assert `== CHAT_INSTRUCTIONS`; collaboration
  state cannot be smuggled into agent instructions beyond the existing
  visibility-gated digest block.
- **SQLite is single-writer**, which favors central ordering (the server
  already totally orders workspace_events with an atomic scalar-subquery
  sequence) and punishes chatty per-keystroke writes.
- **Heartbeats cost O(doc size) per beat per follower.** The 64KB draft rides
  every beat. Any design that increases beat frequency without shrinking the
  payload multiplies this.

## Options considered

### A. Follow-mode + save preconditions (stage 1 — shipped in this change)

Keep the single-writer-plus-followers model and make the write path honest:
the precondition turns silent overwrites into an explicit, recoverable 409
loop. Cost: zero beyond this change — no migration, no transport work, no new
storage.

What it does not do: two genuinely concurrent editors still race to the
banner. The 409 protects data, not flow; the losing editor resolves by hand
with whole-document granularity. For the current usage pattern — small
workspaces, mostly follow-mode, agents as the second writer more often than a
second human — this is a defensible floor, and it is the floor every later
stage builds on.

### B. Per-paragraph three-way merge on 409 (stage 2 candidate)

When a save hits 409, the server has all three texts: the client's base
version (recoverable from `DocumentVersion` by the very id the precondition
carries), the client's attempt, and the current head. Run a diff3 at
paragraph granularity, auto-apply non-overlapping hunks, and return only true
overlaps for a merge-picker UI. No transport change, no storage change, no new
concurrency regime — the interlock is untouched because agent writes still go
through proposals, and the merge only ever runs inside the existing 409 path.

Honest costs: diff3 edge cases (paragraph splits/joins, trailing-newline
churn) need a real test corpus; the merge-picker is a new UI surface; and
paragraph granularity means two people editing one paragraph still land in
the picker. Estimate: **1–2 weeks**, dominated by tests and the picker.

This is the highest ratio of conflict-pain removed to invariants risked, and
it composes with autosave: an autosave behind the precondition becomes safe
exactly when a stale autosave can merge rather than clobber.

### C. Operational transformation

Central ordering fits Grain — SQLite's single writer and the existing
workspace_events sequence make the server a natural total-order authority,
which is the hard part of OT in peer-to-peer settings. But OT's cost is not
ordering, it is transform-function correctness (the notoriously subtle
transform property proofs) plus client-side rebase of unacknowledged ops. Over
a 250ms polling transport, every client holds long unacked queues and rebases
constantly: the worst of both worlds — OT's complexity *and* polling's
latency, with none of Docs' sub-100ms feel to show for it. Estimate: **4–6
weeks** to a version that would still feel like stage 1. **Not recommended at
any stage**; if the team is ever paying OT-level costs, option D buys more
for the same money.

### D. CRDT — Yjs vs Loro (stage 3 candidate)

A CRDT makes convergence a data-structure property rather than a server
protocol, which suits a polling transport better than OT does: updates can
batch into beats and apply in any order.

**Yjs** is the mature pick: battle-tested text type, an awareness protocol
that maps almost one-to-one onto the presence table (cursors, names, states),
and a provider interface simple enough to implement over the existing
poll/POST transport. **Loro** is smaller and Rust/WASM-native with a cleaner
rich-text story, but the ecosystem is younger and the provider work is all
ours. Either way the real costs are the same four:

1. **Storage — this is the one option that needs a migration.** A CRDT
   update-log or state blob lives beside `Document.content`, with periodic
   materialization into `DocumentVersion` so history, restore, retrieval
   chunking, and the share-link renderer keep reading plain text.
2. **The agent interlock must be reconciled, not bypassed.** The clean
   resolution: an approved proposal applies as a CRDT transaction against the
   live doc, which dissolves the stale-hunk race by construction. The
   fallback: proposals freeze the base and rebase on approve, which keeps
   today's regime at the price of keeping today's race window.
3. **The editor moves off the raw textarea.** Offsets-into-a-string stops
   being the caret model; the mirror-textarea overlay and `splitForCaret` are
   replaced by editor-native decorations.
4. **The visibility gate and replace-semantics invariants** must be rebuilt
   inside whatever provider fans updates out.

Estimate: **4–8 weeks**, *plus* the transport upgrade below to make it feel
different from stage 1. Shipping a CRDT over the 250ms poll converges
correctly but types like a laggy terminal.

## The latency note that keeps stage 3 honest

No merge machinery fixes the ~0.5–1s floor: that floor is the transport
(350ms throttle + 250ms poll), and CRDTs ride the same transport. A push
transport (websocket or long-lived SSE with server-side fan-out) is an
orthogonal stage with its own costs — connection lifecycle, the visibility
gate re-implemented in the fan-out layer, and a departure from the deliberate
SQLite-polling architecture. It should be gated on the same evidence as stage
3 itself: real concurrent editing demand, not architectural appetite.

## Recommendation: a staged path, decision held open

- **Stage 1 — ships now.** Save precondition + conflict banner (this change).
  Data loss from concurrent saves becomes impossible; conflict resolution is
  manual and whole-document.
- **Stage 2 — per-paragraph merge on 409, then autosave behind the
  precondition.** Option B. Most 409s dissolve into silent merges; autosave
  becomes safe to turn on, which removes the unsaved-dirty-state window that
  makes last-write-wins reachable at all. 1–2 weeks when scheduled.
- **Stage 3 — Yjs behind a per-document opt-in flag, only on evidence.**
  Option D, with the interlock reconciled via proposals-as-transactions, and
  only if stage 2 telemetry (409 rate, merge-picker rate, concurrent-editor
  minutes) shows genuine simultaneous editing. The opt-in flag keeps the
  migration and editor swap off the hot path for the majority of documents
  that never see two editors.

The decision past stage 1 is deliberately open. Stage 2 is cheap enough to
schedule on the first complaint; stage 3 is expensive enough that adopting it
without telemetry would be résumé-driven engineering against a workspace size
this product does not yet have.

## Consequences

- Stage 1 changes the failure mode, not the collaboration model: followers
  still follow, forks still fork, and the banner is now backed by a server
  check instead of etiquette.
- `DocumentVersion`'s head id becomes load-bearing as a concurrency token.
  Its ordering (created_at desc, id desc — the id tiebreak makes equal
  timestamps deterministic) is now contract, and stage 2 depends on the same
  rows for the merge base.
- The attachment pane still writes through the same PUT with no base token —
  unguarded last-write-wins until it is wired in a follow-up.
- Restore stays an unconditional, deliberate overwrite. Revisit only if stage
  2's autosave lands, at which point an un-preconditioned restore becomes the
  last silent-overwrite path.

### Per-stage test strategy

- **Stage 1 (in this change):** service-level DocumentConflict cases and the
  HTTP 409 contract in `test_artifacts.py`; the isolation suite run unchanged
  (no new routes, no new `db.get` sites); `test_coworking.py` run green to
  prove no presence behavior moved. Presence invariants (replace-semantics,
  clear-bypasses-throttle, visibility gate) stay pinned by their existing
  tests; the memory eval is untouched.
- **Stage 2:** a diff3 corpus as table-driven service tests (splits, joins,
  adjacent hunks, whitespace churn) before any UI; the 409 contract tests
  extended to assert which hunks auto-merged; autosave ships only behind the
  already-tested precondition.
- **Stage 3:** convergence property tests on the CRDT layer (random
  interleavings converge byte-identical); the interlock race pinned as a
  test — an approved proposal applied over a concurrent human edit must land
  both; the visibility gate re-pinned against the new fan-out before it
  carries a single frame. Everything runs against the scripted provider, per
  the standing rule.
