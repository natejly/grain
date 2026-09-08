# Chat file attachments + editing (worktree-screen-order-fix, 2026-08-27)

Two asks:
1. Remove the "Using 1 source passages" status line.
2. Enable attaching and editing files inside chats.

User steers on (2):
- Text files should become **editable Documents**; the attachment must be openable
  and editable, not read-only evidence.
- A file uploaded in a chat **must not just land in the general knowledge base** —
  "if a user uploads something for a chat likely it is going to be relevant to that
  chat". So chat uploads are conversation-scoped.
- Editing happens by **opening the file in a split pane**, not an inline editor.

## Design — dissolve into existing mechanisms, do not build parallel ones

Three mechanisms already ship and do most of this:
- `services/subjects.py` — injects a file's contents into a turn, framed as user
  material (never instructions), screened for prompt injection, and narrows the
  tool registry. Scoped threads use it; the rail chat has no way to say "this turn
  is about this file".
- `retrieval._live_sources(space_id)` — the ONE tuple every search arm shares.
  Already scopes sources by space with `Source.space_id.in_(("", space_id))`.
  Conversation scoping is the same predicate with another column.
- `edit_document` + `PUT /api/documents/{id}` + proposal-diff / hunk approval —
  the entire "edit a file" surface, human and agent side, already built.

So: an attachment is a polymorphic pointer (document | source) from a conversation
to a file, mirroring `Conversation.subject_kind/subject_id`.

# Fix: the run-failure leak and the citation gap (bg/chat-readiness, 2026-08-27)

The two findings the live-model audit turned up, now fixed and measured.

## Plan
- [x] Stop `_fail_run` publishing internal exception text to clients
- [x] Keep the messages that were written for a person (OrgBoundExceeded says so)
- [x] Close the citation gap in the prompt
- [x] Prove both against the real model, not just the unit suite

## Review

**The leak.** `_fail_run` catches every exception an agent turn can raise and
put `str(exc)` on `run.error`, which is published twice — into the `run.failed`
event the browser streams, and into the member-facing Inbox. A driver error
therefore became SQL, bound parameters and row ids on a user's screen.

Fixed with an allow-list, not a sanitiser: there is no reliable way to scrub
identifiers out of arbitrary exception text, so nothing is said about the cause
unless the exception opts in by TYPE. `UserFacingError` (services/errors.py) is
the marker; `OrgBoundExceeded`, `ModelConfigurationError` and `ScriptError`
carry it. Everything else gets one honest sentence naming the run id, and the
detail goes to the log where operators can correlate it.

The allow-list is not optional politeness: `OrgBoundExceeded`'s own docstring
argues that telling a user "the request failed", when the honest answer is
"your organization does not allow this", sends them debugging the wrong thing.
A blanket generic message would have been its own regression. `ChildAborted` is
deliberately NOT tagged — delegation.py:322 catches it, so it never reaches
`_fail_run`; that was checked rather than assumed.

**The citation gap.** `CHAT_INSTRUCTIONS` promises "attach [n] after each claim
supported by passage n", and the evidence block then introduced itself as
"Optional source passages from the user's library". The passages are optional
to USE; citing the ones you do use is not. The header now restates the rule
where the model is actually reading.

## Measured, not asserted

Same scenario, same harness, before and after:

    BEFORE   markers per turn: [0, 0, parked, 0, 2]
    AFTER-1  markers per turn: [2, failed, 2, 3, 4]
    AFTER-2  markers per turn: [1, 2, 2, 2, -]
    AFTER-3  markers per turn: [1, 2, failed, 2, 4]

Twelve completed evidence-bearing turns after the change, none with zero
markers, none out of range. Before, three of four cited nothing.

A second effect, n=3 and not the thing being tested, so stated as observation
rather than claim: the absent-fact question used to park on `ask_user` asking
permission to search a memo it had already retrieved. It now answers directly —
"The provided Atlas memo passages do not state: the p50 checkout latency... the
dollar cost of the Redis caching migration". Dropping "Optional" plausibly made
the passages read as the authoritative set rather than a suggestion.

The leak fix was proven by the bug that motivated it recurring: a turn died on
the same SQLite lock and the client received "The assistant could not finish
this turn. Try again — if it keeps happening, quote run f22a9606-… to your
workspace owner." No SQL in any of the three runs.

## Still open, deliberately

The lock that produced the original leak is NOT fixed. An audit append of a
`message.delta` failing still destroys an in-flight answer, and still 500s the
send. That is the separate "make run_events append non-fatal" item — worth
doing, and a bigger change than this one.

# Chat response-quality audit (bg/chat-readiness, 2026-08-27)

Second pass, after the composer fixes: does the assistant give GOOD answers, not
just does the app carry them. The Playwright suite runs against MODEL_PROVIDER=
scripted, so it can never answer this — canned replies have no quality.

## Plan
- [x] Stand up a live-model harness (`scripts-serve-live.py`, port 8011, real provider)
- [x] Build one shared probe client so scenarios are data, not bespoke HTTP code
- [x] Six scenario families, each probed and then INDEPENDENTLY re-judged
- [x] Verify the headline findings myself rather than relaying agent claims

## Review

29 turns against real gpt-5.5. The model is the strong part: zero fabricated
numbers on grounded questions, both planted "tempting adjacent fact" traps held,
the injection payload was ignored in three scenarios (and named as untrusted
content in one), formatting constraints passed 5/5 under mechanical checking,
and a mid-conversation correction was honoured with the dependent arithmetic
right. Full report published as an artifact; raw transcripts in `qa-results/`.

What is NOT ready is around the model. Two verified directly against the source:

- `_fail_run` (services/runs.py) sets `run.error = str(exc)[:1000]` and puts it
  in the `run.failed` payload, so a raw SQLAlchemy exception — SQL text, bound
  parameters, workspace and run ids — is streamed to the browser. Worse, the
  write that failed was an audit append of a `message.delta`: a correct answer
  in flight was destroyed because a log row could not be written. Backend-
  independent defect; the lock that triggered it here is dev-SQLite only.
- `CHAT_INSTRUCTIONS` (services/model.py) promises "attach [n] after each claim
  supported by passage n", and the evidence block is then labelled "Optional
  source passages from the user's library". Three of four grounded answers
  restated the memo verbatim with `marker_count: 0`, so the UI badges them
  "This answer cites nothing" — a warning label on accurate answers.

## Harness notes, learned the hard way

- The first workflow ran six scenarios CONCURRENTLY and drove dev SQLite past
  its own 30s `busy_timeout` into "database is locked" mid-conversation. That
  does not merely lose a turn: it silently removed a correction the next turn
  depended on, and the transcript then reads as a model ignoring the user.
  Serialise anything that drives chat turns. Cancelling a client does not stop
  the server-side run, so orphaned runs from a killed workflow keep contending.
- The session cookie is issued `Secure`; `http.cookiejar` will not replay it
  over plain http, so a python probe arrives signed out. Browsers exempt
  127.0.0.1, which is why the app itself is fine. Carry the cookie by hand.
- `ask_user` is `force_ask=True` — the park IS the feature. A probe must treat
  `run.waiting_for_approval` as a terminal outcome or it burns its whole
  timeout waiting for a human who is not coming.

# Main chat readiness (bg/chat-readiness, 2026-08-27)

Goal: drive the primary chat as a user and make sure it is fit for people to
use — not "the specs pass", but "nothing silently does the wrong thing".

## Plan
- [x] Baseline the existing chat coverage (workspace.spec.ts) before touching anything
- [x] Drive the real app in a browser: send/stream/settle, reload, rail titling
- [x] Probe what the specs do NOT cover: composer guards, thread switching,
      stop/regenerate/edit, steering, copy, failure paths, mobile, keyboard
- [x] Fix what is actually broken, with a regression test per fix
- [x] Re-verify: unit suite, lint, typecheck, full chat e2e sweep

## Review

The covered paths were already healthy — the baseline run of `workspace.spec.ts`
was green before any change, and streaming, cancel, regenerate, edit-and-rerun,
steering, slash commands, copy, the approval cards, mobile layout and composer
tab order all behaved correctly under a live browser. Two things did not, and
both failed *quietly*, which is why neither had been noticed.

### Fixed
- **A draft was sent into the wrong conversation.** `use-workspace.ts` held one
  shell-level `draft`, never keyed on the active thread, so a half-typed message
  followed you to whatever thread you clicked next and the next Enter posted it
  there — on a shared thread, in front of the wrong people. Proven end to end
  before fixing. Drafts are now keyed by conversation id (with `""` for the
  composer that renders before a thread exists); they are isolated *and*
  remembered, and a send clears only the thread that sent. This also stops the
  typing chip reporting you as typing in a thread you had merely visited.
- **A failed send told the user nothing.** `describeError` returns `""` for an
  unreachable API on the grounds that the health banner covers it — but that
  banner is driven by a separate `/health` poll on a 15s cadence, so it never
  fires for a single blipped request, and never for a 500 that unwinds past the
  CORS middleware and reaches the browser stripped of its status. The send just
  sat there, indistinguishable from an ignored keystroke. Added
  `describeActionError` for failures somebody is *waiting on*; the chat turn
  engine's seven user-initiated catches use it. Background refreshes still keep
  quiet during a real outage, which is what the original silence was for.

### Tests
- `apps/web/tests/action-error.test.ts` — pins the split in both directions
  (background describer stays silent, action describer is never empty).
- `apps/web/e2e/chat-composer.spec.ts` — the two behaviours above, in a browser.
  Both verified to FAIL against the unfixed code before being kept.

### Not a product bug, but worth knowing
`subject-chat.spec.ts` ("the project panel writes a file…") fails in any fresh
clone or worktree with "React runtime asset is missing (404)".
`playwright.config.ts`'s webServer runs `next dev` **directly**, which skips the
`pnpm sandbox-assets` step that `pnpm dev` runs first — so the spec passes only
where an earlier run happened to leave `apps/web/public/sandbox` behind. Running
`pnpm --filter @workspace/web sandbox-assets` fixes it locally; pointing the
webServer command at the `dev` script would fix it for everyone. Left alone here
because it is neither main chat nor caused by this branch.

# Embedding contract + generations + 256-dim (worktree-rag-embedding-generations, 2026-08-26)

Inspired by HF's Papers-with-Code search write-up. Grain already had the post's core
design (BM25 + dense + RRF at k=60/depth=50, lexical degradation); this closes the four
gaps that post exposes. Explicitly OUT of scope: pgvector/HNSW, which would break the
one-ranking-function-serves-both-backends rule that `ChunkTerm` exists to uphold.

## Measured first (probes, real OpenAI key, `apps/api/evals/corpus.json`)
- [x] 256-dim preserves answer quality exactly: GT@1 .964 / GT@3 1.000 / GT@5 1.000,
      identical to the 1536 reference across all three question strata
- [x] float16 costs nothing: rankings identical to float32 -> 512 B/vec, 12x cut
- [x] `cosine(API dims=256, local truncate+renormalize) = 0.999997` -> a generation can be
      materialized from stored 1536-dim blobs with ZERO API calls
- [x] Dense floor does NOT survive dimension change: 0.30 admits 11.5% of pairs at 1536 but
      24.4% at 256 (noise/query 1.54 -> 4.36). Empirical equal-selectivity floor at 256 is
      **0.3535** (gold 28/28, noise/query 1.54, matching the reference exactly).
      The closed-form sqrt(d) rule is WRONG here: 0.3*sqrt(1536/256)=0.735 keeps 1/28 gold.

## Plan
- [x] `embedding_generations` table: model, revision, dimensions, storage_dtype,
      normalization, input_format, **dense_floor**, status, timestamps
- [x] `embedding_vectors` table — one row per (owner, generation). **Replaced** the
      original plan of contract *columns* on the three owning tables: a column holds one
      vector, so build-beside would have overwritten the corpus being served and the
      documented rollback would have been a lie. Caught mid-implementation.
- [x] `embeddings.py`: dimension-aware embed, float16 pack/unpack, local MRL truncation,
      dtype-aware `ranked_cosine_scores`
- [x] Readers filter by ACTIVE generation, not by model-name string; floor comes from the
      generation, with an explicit `RETRIEVAL_DENSE_FLOOR` still overriding it
- [x] Migration 0068 + backfill; deriving a narrower generation costs zero API calls
- [x] Activation: build beside -> verify coverage -> flip atomically -> keep prior for rollback
- [x] Content hash so an edited chunk cannot silently keep a stale vector
- [x] Eval gate: `evaluate_retrieval.py --dimensions` measures truncation end to end
- [x] Web surface: `GET /api/org/retrieval-contract` + `RetrievalContractPanel`
- [x] Tests; commit + push

## Review

**Result: 256-dim float16 retrieves identically to 1536-dim float32 on this corpus,
at 1/12th the bytes** — measured through the production path with a live provider:

| contract | lexical | paraphrase | indirect | overall | bytes/vec |
|---|---|---|---|---|---|
| 1536d f32 | 100% / 1.000 | 100% / 0.933 | 100% / 1.000 | 100.0% | 6,144 |
| 512d f16 | 100% / 1.000 | 100% / 0.933 | 100% / 1.000 | 100.0% | 1,024 |
| **256d f16** | **100% / 1.000** | **100% / 0.933** | **100% / 1.000** | **100.0%** | **512** |
| 128d f16 | 100% / 1.000 | 100% / 0.883 | 90% / 0.900 | 96.4% | 256 |

128 degrading is what makes 256 a real pass rather than a saturated one: the benchmark
has resolution immediately below the recommended width.

### What was wrong before this
- A vector's only provenance was `embedding_model`, a bare name. Same-width vectors from
  two models compared cleanly and ranked wrongly — the dense arm's own comment admitted it.
- Editing that setting migrated nothing; it made every stored vector fail the reader's
  filter at once. Hybrid search became lexical search with **no error anywhere**, because
  degrading to lexical is a designed behaviour. Quality dropped; the system looked healthy.
- A re-ingest rewrote `content` in place and kept the old vector. Retrievable, confident,
  and describing words the chunk no longer held.

### Two things the work itself corrected
- **The vector-per-row flaw** above. Fixed before shipping, not after.
- **The floor is not portable across widths.** `retrieval_dense_floor = 0.30` admits 11.5%
  of query-document pairs at 1536d but **24.4%** at 256d — junk per query 1.54 -> 4.36,
  rescuing zero additional true answers. So the floor travels with the generation.
  The tempting closed form is wrong: `0.3 * sqrt(1536/256) = 0.735` keeps **1 of 28**
  true answers. The equal-selectivity floor is **0.3535**, measured. Measure, don't derive.

### Deliberately not done
- **pgvector / HNSW.** The dense arm still scans up to `retrieval_vector_candidate_cap`
  vectors per query in numpy. pgvector would break the one-ranking-function-serves-both-
  backends rule `ChunkTerm` exists to uphold. 256d f16 cuts that scan 12x as a stopgap;
  the ANN index is a separate decision.
- **The default is still 1536.** Lowering it opens a new generation nothing reads until
  backfilled and activated. Flipping a live corpus is an operator's call, not a deploy's:
  `scripts/rebuild_embeddings.py --dimensions 256 --dtype float16 --activate`.
- **The web panel is read-only.** Activation has corpus-wide blast radius; it stays in the
  script where it is logged and hard to do by accident.

### Verification
- api suite **2,777 passed** (18 new), web **869 passed** (5 new), ruff + mypy + eslint +
  tsc clean, `make eval` exit 0 at the documented floors.
- Migration proven on SQLite via 6 tests: dominant model detected, width inferred from a
  stored vector, minority-model rows correctly left behind, bytes byte-identical, source
  columns untouched, upgrade/downgrade/upgrade replays to one generation.
- Postgres: the full alembic chain was **not** run (the `psycopg` extra is not installed
  and mutating the shared venv was out of scope). The two backend-dependent constructs
  were verified directly on PG16 — `length(bytea)` returns bytes, and the partial unique
  index rejects a second `active` row. **Run the chain on Postgres before deploying.**
- The staleness test was mutation-checked: reverting the predicate to "does *a* vector
  exist" makes it fail, so it is not vacuous.

# Security audit + rate limiting (bg/security-audit, 2026-08-25)

## Plan
- [x] Migration `0068_chat_attachments`: `chat_attachments` table +
      `Source.conversation_id` (indexed, default "")
- [x] `Source.conversation_id`: "" = workspace library (today's behaviour);
      non-empty = uploaded in that chat and invisible to every other thread
- [x] Extend `_live_sources` to take `conversation_id` and thread it through every
      arm + `search_evidence` — all arms or it is a bypass with extra steps
- [x] `services/attachments.py`: upload → text becomes a `Document` (editable),
      non-text becomes a conversation-scoped `Source` (indexed evidence)
- [x] API: POST/GET `/api/conversations/{cid}/attachments`, DELETE `/api/attachments/{id}`
- [x] Turn context: inject attached documents into the turn (bounded, screened),
      reusing the subjects.py framing
- [x] api-client: attachment types + methods
- [x] Composer: attach popover gains "Attach to this chat"; chips above composer
- [x] Split pane: clicking a document chip opens `AttachmentPane` beside the chat
- [x] Tests: 14 api + 12 web
- [x] Verify: pytest, pnpm test, typecheck, lint (ruff/mypy/eslint), build, alembic

## Review

### 1. "Using 1 source passages" — removed

A transient run-status line in `handlers/thread.ts`, set on the
`retrieval.completed` SSE event. The branch is gone; a comment records why
retrieval is not surfaced there — the evidence still reaches the reader as `[n]`
citations on the finished answer, which is where it means something. The backend
still emits the event, so `test_golden_path` is untouched.

### 2. Attach + edit

**The scope is the load-bearing half.** `Source.conversation_id` mirrors
`space_id` exactly, so `_live_sources` filters both in one tuple — a scope
enforced by some ranking arms and not others is a bypass with extra steps. All
five arms carry it (`legacy_lexical_ranking`, `bm25_ranking`, `dense_ranking`,
`rank_arms`, `search_evidence` plus the hydrate spread), and so do the three
other places a file could leak: `_search_sources` (the tool entry, the real
would-be bypass), the graph projection, and `GET /api/sources`.

The Sources listing defaults the *opposite* way from the space filter, on
purpose: absent means the library alone. A space's files are still the
workspace's files, so an unfiltered call showing them is right; a file attached
to one chat is not, and the Sources page calls that route with no arguments.

**Routing.** `.txt/.md/.markdown` → `Document` (editable, injected whole);
everything else on the upload allowlist → conversation-scoped `Source` (indexed,
quotable, not editable — there is no text to edit, and re-chunking a file under
live citations would strand them). `.csv`/`.json` sit on the Source side despite
being text: their value is being queried and quoted, and CSV already has a
destination of its own in datasets.

**Editing needed almost no new code.** A document that arrived as an attachment
is an ordinary document, so `edit_document` (with its diff review and hunk-level
approval), versions and undo all already applied, and the rail chat already had
the `artifacts` family. The only new surface is `AttachmentPane`, a small editor
column that writes through the same `PUT /api/documents/{id}` the Documents page
uses.

**Injection.** The attachment context and the subject context are now one local
(`spliced_context`) that is both injected *and* screened — two expressions that
must stay equal are one edit away from not being, and the failure mode there is
splicing an unscreened upload straight into the prompt.

### Three defects the repo's own tripwires caught

- **`detach` cleared `Source.conversation_id` to `""`** — which in this schema is
  *the workspace library*, so removing a file from one chat would have published
  it to every other one: the exact leak the feature exists to prevent. Now runs
  `purge_source`, the same teardown the Sources page uses. Caught by the test
  written for it.
- **`test_every_db_get_call_site_is_reviewed`** — four new `db.get` sites needed
  recorded justifications; each re-checks `workspace_id` on the next line.
- **`test_route_table_matches_the_app`** and **`theme-tokens`** — the three new
  routes needed isolation cases and a seeded victim attachment, and a
  `var(--danger, #b3261e)` fallback was a hardcoded colour.

A fourth, caught by a web test: `AttachmentPane` rendered an empty textarea when
the load *failed*, which invites the user to "fix" the blank and save it over a
healthy document. The editor is now gated on having the content.

### Not done / worth knowing

- **`pnpm test:e2e` was not run.** Everything else in `make verify` was
  (pytest, `pnpm test`, typecheck, ruff, mypy, eslint, build, alembic up+down).
  Playwright needs a live API + web + database and this worktree has no `.venv`
  of its own.
- **Spaces still leak into the graph.** `rebuild_graph` filters the conversation
  axis now but has never filtered `space_id` — pre-existing, and left alone
  because changing it would alter Spaces behaviour that is not this task's.
- `subject-chat.tsx` deliberately gets no `attachToChat`: a panel beside a
  document already has a subject, and a second way to say what the conversation
  is about would be one too many. `chat-pane.tsx` originally shared that fate,
  but the reason never applied to it — an extra split pane is a free-standing
  thread, not a subject panel — so it now carries the paperclip with pane-local
  attachment state (2026-09-08).
- File panes are **not persisted** and close on a thread switch — a working
  surface is not a layout, and a revived editor would reopen files the user had
  closed. This is why they are a separate list from `chat-panes`, whose store,
  pruning and saved layouts keep the one shape they already have.

---

# Merge to main + follow-ups (main, 2026-09-08)

## Plan
- [x] Fast-forward `worktree-screen-order-fix` into main (chat attachments + screen-order fix)
- [x] Close the two cross-conversation scope gaps the branch left: sandbox `_workspace_sources` and analytics `_source_for_dataset` now honour `Source.conversation_id`; tests for both in `test_chat_attachments.py`
- [x] Fix the misindented `conversation_id` kwarg in `retrieval.search_evidence`
- [x] Give the extra split panes the paperclip: `ChatPane` holds pane-local attachment state (chips, attach-to-chat, detach) on top of the shell's workspace-level `attach` halves, threaded through `ChatSplit`
- [x] `alembic upgrade head` (0067 → 0068) on the dev DB
- [x] e2e: `workspace-create.spec.ts` (the switcher's New-workspace row, previously untested in a browser) and `chat-attachments.spec.ts` (chip strip, split-pane editor, scoped upload absent from the library)

## Review
- Full api suite, full vitest suite, and the workspace e2e specs all green after the merge and fixes.
- Workspace creation needed no feature work — it shipped with the live-cursors merge; what was missing was browser-level proof, which the new spec now provides.
