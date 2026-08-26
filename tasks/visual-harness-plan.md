# Visual agent harness plan (drafted 2026-08-25)

Goal: make the Grain chat surface render agent activity the way Claude Code /
opencode do — visible thinking, a live activity timeline, bespoke per-tool
renderers with syntax highlighting, nested subagent transcripts, a pinned
plan panel, richer diffs, and an in-chat cost meter — without a new transport
(everything rides the existing RunEvent + sequence-cursor rails).

Survey baseline (2026-08-25): SSE over polled `run_events`
(`apps/api/app/api/chat.py:1236`, `services/events.py` DeltaBuffer), ~22 event
types; client consumes via `packages/api-client` `streamRun()` yielding
untyped `Record<string, unknown>` payloads into a flat if-chain in
`apps/web/components/handlers/thread.ts:369-503`; renderer is
`views/chat.tsx` (`ToolCallCard :927`, `ProposalDiff`, approval row, steer
strip). Structural blockers: flat `agentCalls[]` keyed by run_id (no turn
tree), no thinking/delegation/guardian/usage events, `result_preview` clipped
at 500 chars, zero syntax highlighting.

## Phase A — Event contracts (backend, serial; land before any fan-out)

- [ ] A1. Typed event payloads: one Pydantic model per event type in a new
      `apps/api/app/schemas_events.py`, referenced from an endpoint response
      model so they land in openapi.json → generated client types. Client
      gets a discriminated union `RunEventPayload` and `followRun` becomes an
      exhaustive switch (lesson: optional/untyped contracts drift silently).
- [ ] A2. `thinking.delta` / `thinking.completed` events: emit from the
      Anthropic harness's already-parsed thinking blocks
      (`services/harness/anthropic.py:78-100`), batched through the existing
      DeltaBuffer. Redacted thinking emits a `redacted: true` marker only.
- [ ] A3. `guardian.decided` event carrying verdict + reason (today the
      rationale stops at the audit table; UI shows only a zap badge).
- [ ] A4. `usage.updated` event per model step: input/output tokens, cost,
      context-window fraction (source: `services/usage.py` attribution).
- [ ] A5. Delegation child visibility: children currently write no events by
      design (`services/delegation.py:18` — the old (run_id, sequence) race).
      The atomic scalar-subquery sequence INSERT (already landed, see
      lessons) removes that race, so children can now append envelope events
      to the PARENT run: `child.event {child_id, parent_tool_call_id, label,
      inner: {event, data}}`. Children emit coarse inner events only
      (tool.started/completed, message.completed, status) — no child text
      deltas, to bound write volume. Best-of-N attempts carry attempt index +
      win/lose in the envelope.
- [ ] A6. Full-fidelity tool results on demand: keep the 500-char
      `result_preview` on the event; add `result_full` storage (own column —
      lesson: a truncated summary is not a place for structured facts) and
      `GET /api/agent-tool-calls/{id}/result` for the expanded card to fetch.
- [ ] A7. `step` grouping metadata on tool events (iteration number, batch
      id) so a parallel batch can render as one group.
- [ ] Gate: pytest (new event tests: round-trip row → SSE frame → typed
      parse; child-envelope concurrency test with parallel writers), alembic
      up/down across the new column, make lint.

## Phase B — Client stream model (serial, small; touches the shared seam)

- [ ] B1. Turn tree reducer: replace flat `agentCalls[]` accumulation with a
      per-run `Turn { steps: Step[] }` where Step = text | thinking | tool |
      child-run | note (citations/screen-flag/usage), built by an exhaustive
      switch over the typed union in `handlers/thread.ts`. Keep the existing
      hook return shape via an adapter first so the three mount points
      (workspace.tsx:1065, chat-pane.tsx:174, chat-split.tsx) don't churn in
      the same change.
- [ ] B2. Migrate `ChatView` to consume the turn tree; delete the adapter.
- [ ] Gate: pnpm test (reducer unit tests: every event type, out-of-order
      resume via Last-Event-ID, thread-switch stillOpen guard), pnpm build.

## Phase C — Visual rendering (web; fan-out safe after A+B, one track per
## new file)

- [ ] C1. Syntax highlighting: add shiki (fine-grained bundle, offline — no
      CDN per CSP) behind one `<CodeBlock>` component; apply to markdown
      fences, tool args/results, and diff lines. Budget check: measure the
      pnpm build size delta; fall back to highlight.js/lowlight if shiki
      costs too much.
- [ ] C2. Per-tool renderer registry: `views/tool-renderers/` keyed by tool
      name — bash/terminal output pane, file-read viewer, search-results
      list, web-fetch card, edit→diff. Default = today's JSON card. Expanded
      view fetches `result_full` (A6). Copy bar on all payload panes.
- [ ] C3. Thinking block: collapsed-by-default italic block with duration,
      streaming shimmer while `thinking.delta` arrives; "redacted" state.
- [ ] C4. Activity timeline: replace the single last-writer-wins status line
      with a live step list for the running turn — per-tool spinners,
      parallel batches grouped (A7), elapsed time, iteration counter. The
      strings already assembled in thread.ts become rows, not overwrites.
- [ ] C5. Pinned plan panel: persistent todo/plan card for the active run
      (today `TodoChecklist` is bolted to the newest todo tool card),
      updating across the turn; `exit_plan_mode` preview promotes into it.
- [ ] C6. Diff upgrades in `ProposalDiff`: word-level intra-line highlight,
      line numbers, collapse long unchanged regions, file-path header row.
      (Per-hunk accept stays in document-review; not in scope here.)
- [ ] C7. Guardian rationale disclosure on auto-approved cards (A3) +
      in-chat usage meter (A4): tokens/cost/context bar in the turn footer.
- [ ] C8. Subagent transcript: `delegate` card expands into the child
      timeline built from `child.event` envelopes; best-of-N renders as
      attempt tabs with the winner marked.
- [ ] Gate per track: pnpm test + build; SCREENSHOT REVIEW of every visual
      change (lesson: for anything visual, look at it); e2e additions
      mutation-tested (break the feature, watch the spec fail).

## Phase D — Streaming feel (optional, last)

- [ ] D1. Client-side typewriter smoothing of message/thinking deltas
      (interpolate the 48-char batches over the 250 ms tick) — no change to
      server batching or poll rate, which are deliberate (events.py:12-16).

## Sequencing & risks

- Backend-first (Phase A whole) before any UI, so a stopped session leaves a
  complete tested API (lesson: the frontend is the resumable half).
- B is the one shared-seam refactor; do it serially, not fanned out.
- C tracks are parallelizable: each gets only its own new files; the
  registry wiring edit in chat.tsx stays serial and small (lesson: parallel
  tracks converge on "someone else will wire it" — budget for the join and
  verify at the seam by booting the app).
- Write-volume risk (A5): child envelopes are coarse-only; if a delegation
  fan-out still floods, batch envelopes through DeltaBuffer-style
  coalescing before adding any cap (a silent cap would hide child activity).
- Another session is active in the main checkout (todo.md and web files
  modified) — implementation should run in worktrees and land contracts
  first.

## Full gate per phase

make lint · pytest · pnpm test · pnpm build · pnpm test:e2e · alembic
upgrade/downgrade on a scratch DATABASE_URL for A6.
