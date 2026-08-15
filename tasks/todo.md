# Personal scope — ADR 0010

Head is `0039_invite_revocation`; this takes `0040_personal_scope`.

## 1. ADR
- [x] `docs/adr/0010-personal-scope.md`

## 2. Model + migration
- [ ] `MemoryItem.owner_id` String(36) default "" server_default "".
      Unique key becomes (workspace_id, owner_id, kind, normalized_key).
- [ ] `ToolPolicy.owner_id` likewise; unique (workspace_id, owner_id, tool_name, scope).
- [ ] `0040_personal_scope`: add both columns, rebuild both unique constraints,
      backfill `tool_policies.owner_id = created_by`, memory stays "".
      Verify full chain from an empty database, and the downgrade.

## 3. Personal memory
- [ ] `_active(stmt, workspace_id, viewer_id="")` — scope through the one
      chokepoint, `owner_id IN ('', viewer_id)`.
- [ ] `_upsert_item` / `remember_memory` / `tombstone_key`: exact owner match on
      the write path so my correction cannot retire your row.
- [ ] `apply_extracted_memories(..., owner_id="")`; `write_conversation_memory`
      derives it from `Conversation.shared`.
- [ ] `recall(..., user_id="")` + claim-key shadowing (personal displaces shared).
- [ ] Callers: runs.py, llm_tools.py, memory_tools.py, api/memory.py, graph.py.

## 4. Personal tool policies
- [ ] `evaluate_policy(..., user_id)`: deny-wins, then personal, then shared.
- [ ] `_upsert_policy(..., owner_id)`; approval card grants personal.
- [ ] `PUT /api/tool-policies` gains `shared: bool`, owner-only.
- [ ] `GET`/`DELETE` scoped to shared + own.

## 5. Web
- [ ] api-client: `MemoryItem.shared`, `ToolPolicy.scope`/`shared`, `deleteToolPolicy`.
- [ ] Memory view: scope pill reusing `.memory-kind` shape, existing tokens only.

## 6. Tests
- [ ] Cross-person: my personal memory is absent from your recall and your list.
- [ ] Supersession stays in scope; shadowing serves mine.
- [ ] Personal grant does not authorise another member; shared deny beats personal allow.
- [ ] `RouteCase`s for anything new; extend `_active` chokepoint assertion to scope.
- [ ] Mutation-check the recall scope filter and the policy owner lookup.

## Gates
- [ ] ruff + mypy + pytest (x2) + evaluate_memory + openapi export
- [ ] tsc + eslint + vitest + build
- [ ] playwright (x2)

## Review

All boxes above are done. What is worth remembering:

**The leak was real and pre-existing.** `Conversation.shared` defaults to False
and the API already refuses to let a member decide a tool call parked on someone
else's personal thread — but `write_conversation_memory` wrote what the extractor
learned from that thread with `workspace_id` and no owner, and `recall()` and
`GET /api/memory` filter on `workspace_id` alone. So the thread was private and
its contents were not. That is what made "a memory inherits the visibility of the
conversation it was learned in" the rule rather than a new judgement call.

**Two mechanisms, not one.** Supersession is per-scope at *write* time (owner in
the unique key, exact owner match in `_upsert_item`) so my correction cannot
retire your row; shadowing is cross-scope at *read* time (a personal row displaces
the shared row on the same `normalized_key`) so I still see my version. Collapsing
them into a scoring bonus would have kept serving both values at once, which is
the STALE-SERVED failure `evaluate_memory.py` exists to measure.

**Sentinel, not NULL.** NULLs are distinct inside a unique index on both engines,
so a nullable `owner_id` would have stopped the key constraining the shared rows
and taken supersession with it. ADR 0005 chose NULL for `slot_index` for exactly
that property; here it is the bug.

**Deferred, explicitly:** files/documents, crons/workflows, sandbox sessions, and
the graph projection. MCP credentials were already per-user (ADR 0006).

**The cost that surprised me:** threads are personal by default, so memory now
stops feeding the shared graph until a thread is shared. Bigger than "personal
memories are missing from the graph" — it is the ordinary case. Pinned by
`test_a_personal_threads_memory_stays_out_of_the_shared_graph` and written up as
the strongest argument for scoping `graph_entities` next.

**Mutation checks** (each reverted after):
- owner predicate out of `_active()` -> `test_route_isolation[GET /api/memory]`
  fails, leaking the roommate's personal memory id.
- owner filter out of the `evaluate_policy` query -> 3 failures in
  `test_personal_scope.py`, incl. one member's grant authorising another.
- `_personal_shadows_shared` out of `recall()` -> the shadowing test fails with
  both the personal and the shared value served.

**Pre-existing flake found, not caused:** `workspace.spec.ts` approval tests
(:119, :179, :336) intermittently time out at 30s after Approve. Reproduced at
b07f083 in a clean worktree at the same rate (2/0/1 failures over three runs vs
3/2/1/0/1 over five on this branch). Server-side the runs complete correctly —
messages, tool calls and events are all right in the database — so it is a client
render race, not this change. Both full Playwright runs came back 62/1 skipped.
