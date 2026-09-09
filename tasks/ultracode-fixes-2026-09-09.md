# Ultracode fix pass — 2026-09-09

A multi-agent hunt (7 dimension finders → dedup → 3-lens adversarial verify, 46
agents) over the Grain codebase, weighted toward recently-merged code, then the
confirmed findings fixed and re-verified. A second multi-agent pass reviewed the
fix diff itself.

## Find → verify

13 raw findings, 13 unique, **11 confirmed** by ≥2 of 3 adversarial verifiers.
Two were **rejected** by verification and left alone:
- "Migration 0069 infers width from MIN() and abandons the wider subset" — the
  mixed-width state it needs is impossible (the width knob and the migration ship
  in one commit). 0/3.
- "Comment deletion has no confirmation" — a subjective UX preference, not a
  defect; the delete works correctly. 0/3.

## Fixed (10 of 11)

| # | Sev | Fix |
|---|-----|-----|
| 1 | High | `selectConversation` now guards `setMessages` with `activeConversationRef` — a slow thread's transcript can no longer land under a thread you switched to. (`handlers/chat.ts`) |
| 2 | High | `conversations.purge` now deletes the thread's `MemoryItem` rows — on Postgres the FK made the whole delete 500; on SQLite it leaked memories that kept being recalled. (`services/conversations.py`) |
| 3 | High | `purge`/`truncate_after`/`delete_space` now drop the `conversation_chunk` / `memory_item` embedding vectors of rows they delete — orphan vectors were making `coverage()` count dead owners as pending and refuse to activate a new embedding generation. (`conversations.py`, `spaces.py`) |
| 4 | Med | `forkThread` gets the same still-open guard as #1. (`handlers/chat.ts`) |
| 5 | Med | Staged (unsent) attachments are now scoped to the member who staged them, so a shared thread never feeds or binds one member's unsent file into another's turn. (`services/attachments.py`, `api/chat.py`, `services/agent_loop.py`) |
| 6 | Med | Detaching a non-text attachment now sweeps its file from disk after commit — before, every detach leaked an unreferenced file forever. (`services/attachments.py`, `api/attachments.py`) |
| 7 | Med | Sandbox-secret delete now confirms, like every other destructive action. (`handlers/sandbox-secrets.ts`) |
| 8 | Med | The add-secret form keeps the typed credential on a server rejection instead of discarding it. (`handlers/sandbox-secrets.ts`, `views/sandbox-secrets.tsx`) |
| 9 | Low | Attaching to an empty composer no longer races its own refetch to blank the chip — an epoch orders the append against the per-thread refetch. (`handlers/attachments.ts`, `use-workspace.ts`, `chat-pane.tsx`) |
| 11 | Low | Attachment upload now takes an Idempotency-Key and replays, so a retry/double-click can't duplicate the file, chip and on-disk copy. (`api/attachments.py`) |

**Deferred (1):** #10 (subject-thread get-or-create can race into duplicate
threads on a double-click, Low). The correct fix is a unique constraint on
`(workspace_id, subject_kind, subject_id)`, which needs a data migration to
collapse any existing duplicates first — not safe to bundle into this sweep on a
live DB. Documented for a dedicated change.

## Review of the fix diff

A second multi-agent pass reviewed the diff for regressions. Most agents stalled
on a resource issue, but one surfaced a **real edge case in the #9 epoch guard**:
a single global epoch couldn't tell "a newer write for *this* thread" from "an
upload for a *different* thread", so switching threads mid-upload could strand
the chip and block the new thread's refetch. **Fixed:** the optimistic append and
its epoch bump now happen only when the upload's thread is still on screen
(`currentConversationId()`), which is correct for both the rail (switchable) and a
pane (pinned). The three areas the stalled agents didn't reach
(purge/vector-drop callsites, attachment scoping, detach/idempotency) were
reviewed by hand and found sound — e.g. `delete_space` already routes its threads
through `conversations.purge`, so their vectors are covered.

## Tests
- Added/strengthened: shared-thread staging privacy (#5), purge-removes-memories
  (#2), detach-removes-the-disk-file (#6), plus a tenant-isolation allowlist entry
  for the idempotency replay lookup.
- Full API suite green; web unit suite 907 green; ruff, mypy, tsc, eslint clean.

## Shipped
All on `main` and deployed to UAT (green): `c2c7f12` (the 10 fixes) → `e177b36`
(regenerated the OpenAPI contract for the new Idempotency-Key param, which the
contract-drift gate caught) → `9f892cb` (made an unrelated scheduler test
order-independent — its `probe.calls == 2` assertion counted *global* orphaned
WorkflowRuns and CI hit `5 == 2` purely on test order; the fix retires sibling
WorkflowRuns so only the one under test is recoverable). CI's full suite —
Postgres-backed pytest, e2e, contract check, sandbox proofs — is green, and
`api.uat.grain.natejly.com` / `uat.grain.natejly.com` both return 200. Production
is unchanged; promote when ready.
