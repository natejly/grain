# Lessons

- A relative `sqlite:///./data/x.db` means a *different file per working
  directory*. `uvicorn` ran from the repo root, `make migrate` cd'd into
  apps/api — so every migration landed on a phantom apps/api/data copy while the
  app kept a schema frozen at whatever `create_all` first produced. Symptom was a
  500 ("no such column: generated_apps.app_type") in one feature, months after
  the migration "ran". Rule: anchor relative data paths to the repo root in
  settings, and have the dev entrypoint run `alembic upgrade head` before boot so
  drift fails loudly at startup instead of silently inside a feature.

- Never call `setView(...)`/navigation state setters *after* an awaited chain in
  an async handler — by the time the awaits resolve, the user may have navigated
  and the setter clobbers their view. Set view state synchronously at the start
  of the interaction. (Found via the e2e race in `uploadFiles()`: adding two API
  calls to `refreshExpansion` made the post-await `setView("sources")` land
  after the test clicked into Dashboards.)
- A postMessage handshake between a host page and an iframe must be
  order-independent in BOTH directions. `SandboxFrame` only sent data in reply
  to the frame's "ready" ping, which works when React creates the iframe
  (listener first) but not on a server-rendered page, where the frame can load
  and ping before hydration — published code apps silently rendered "Waiting for
  data…". Fix was two-sided: host posts init on mount + load + ready, and the
  injected runtime makes `onData` a setter that replays already-delivered
  snapshots. Rule: never assume the other side is listening yet; make delivery
  idempotent and replayable.
- "It won't build/run" reports deserve reproduction before code changes: here
  typecheck/lint/build/boot were all green, and the real fix was DX (one-command
  `make dev` + an API-down banner), not code.
- This repo verifies with: `make lint` (ruff+mypy+eslint+tsc), pytest suite,
  `pnpm test`, `pnpm build`, `pnpm test:e2e`, plus alembic upgrade on a scratch
  `DATABASE_URL` for schema changes. Run all of them before calling a phase done.
- `pip install garminconnect` on the py3.9 venv resolves to 0.2.8 (newer needs
  3.10+); pin `<0.3` and avoid `client.login()` (stdin MFA prompt) — call
  `client.garth.login(email, password, prompt_mfa=<raiser>)` instead.
- The repo-root anchoring lesson has a second home: `tests/conftest.py` computed
  `Path("./data/test_workspace.db")` from the *cwd* while `Settings` anchors the
  same relative URL to the repo root. So the pre-test `unlink()` deleted a
  phantom `apps/api/data` copy and the suite kept reusing a stale repo-root
  database whose schema was frozen at whatever `create_all` first produced —
  invisible until a migration added a column and every test died on "table runs
  has no column named agent_state_json". Rule: any code that resolves a path the
  settings also resolve must use the same anchor (`app.config.project_root()`),
  never a bare relative path.
- Streaming deltas do not need a new transport. The durable RunEvent + sequence
  cursor already carried fake `stream_words` output, so real model tokens ride
  the same rails; what does need care is write volume — one event row per token
  means a `max(sequence)` query and a commit each, feeding a reader that only
  polls every 250ms. Batch to a size/time threshold (`DeltaBuffer`) and the
  transport stays untouched.
- When a loop can pause, its state has to be serializable *before* you design
  the pause. Tool calls arrive in batches, so a run parked on call 2 of 3 must
  remember the undrained queue as well as the transcript — otherwise the resumed
  turn sends the model a function_call with no matching output and the API
  rejects the whole turn. Model `pending_calls` explicitly rather than inferring
  it from the transcript.
- Bridging an async SDK into this sync API needs a private loop in a private
  thread, not `asyncio.run()`. `asyncio.run` raises if a loop is already running
  in that thread, so the same helper would work from `process_run` (threadpool,
  no loop) and blow up from an async endpoint. Submitting to a one-shot
  ThreadPoolExecutor works from both, and the outer `.result(timeout=)` is the
  backstop for a server wedged before `wait_for` can bound it.
- Bumping the Python floor surfaces new lint, and `zip(..., strict=)` is a real
  decision, not a silencer: use `strict=True` where the lengths are provably
  equal (`headers` built from `fieldnames`, DB-API `description` vs row) so a
  future mismatch fails loudly, and `strict=False` where the count comes from an
  external API and truncating beats losing the batch.
- A feature request can dissolve into an existing mechanism. "Inline writing
  diff" looked like an editor feature; it is really the presentation of an
  approval. Adding `ToolSpec.preview` — render what a call *would* do, without
  doing it — made the diff fall out of the M1 park/resume path and gave board
  moves a readable preview for free. Look for the request that is a property of
  a mechanism you already have before building a parallel one.
- Model-facing tools need human-style addressing. An id-only `edit_document`
  forces the model to call `list_documents` first and remember a uuid across
  turns; accepting a title (and letting a lone board go unnamed) removes a whole
  class of failed turns. Verified against a real model: it chose the tool, quoted
  a whole sentence as `find` to make it unique, and never needed the id.
- Parallel agents cannot share a file. Two tracks that both "add a model, a
  migration, a router line" will each read-modify-write the same files and the
  second silently wins. Landing the contracts first (models + one migration +
  the interfaces) and giving each track only its own new files made the fan-out
  safe; the integration edits stayed serial and small.
- Adversarial review earns its cost, and so does re-checking it. The reviewers
  found a real read-only bypass (dollar-quoted strings deleting rows on DuckDB)
  and two unmounted subsystems. But reviewing their work found two more they
  missed: MySQL executable comments `/*! … */`, which the server runs rather
  than ignores, and `sqlite:///` + an absolute path resolving to a *relative*
  file. Treat a clean verdict as a prior, not a proof.
- A stripped comment is not always an inert comment. Any guard that normalises
  SQL before inspecting it must ask whether the database also ignores what was
  stripped — MySQL's `/*!…*/` is the counterexample that turns "strip then
  check" into a bypass.
- Put the unknown in its own phase. The whole LaTeX feature hinged on one
  question no amount of code could answer — does any wasm TeX engine compile
  offline — so it became a research agent with a mandate to spike, and an
  explicit licence to come back negative. It came back positive with an engine
  nobody would have guessed, and the build agents inherited a decision instead
  of a gamble.
- Parallel tracks converge on "someone else will wire it". Three agents each
  built a correct half, each reviewer verified that half, every gate was green —
  and all three features were invisible in the running app. Routers unmounted,
  props unpassed, a client missing six methods. Green tests plus a green build
  is not evidence a user can reach the feature; only booting the app and calling
  it is. Budget for the join, and verify at the seam.
- Tests can encode the bug. `test_starter_in_a_subdirectory_still_finds_its_
  bibliography` asserted the exact string that makes bibtex exit 2, with a
  confident comment explaining why it was right. A passing suite proves
  consistency with what someone believed, not correctness — which is why the
  compile had to be run.
- A regression test can be blind on the machine that matters. The guard against
  "utcnow() now returns local time" passed under TZ=UTC even with the helper
  deliberately sabotaged — and CI runs UTC, so it would have protected nothing.
  A test for an environment-dependent bug has to force the environments; assert
  under several zones, not the one you happen to be in.
- Mutation-test new coverage. Six deliberate breakages of the approval flow each
  had to make the new e2e fail before the coverage counted. Two of them (deny
  ignored; deny reported but the write still executed) are exactly the failures a
  test asserting "a card appeared" would sail past.
- Extracting a hook can turn a constant into a reactive value. Passing the shared
  `WorkspaceApi` into useWorkspace(api) kept it in the original file but made it a
  dependency, producing nine exhaustive-deps warnings whose fix would have meant
  editing dependency arrays mid-refactor. A module-scope constant in its own file
  keeps it non-reactive and breaks the cycle.
- A benchmark can be measured correctly and still miss the regime that matters.
  The memory reviewer proved its change was faithful up to 2,000 rows against a
  5,000-row cap — the divergence it was checking for could not appear below the
  cap. Whenever a change introduces a threshold, the test corpus has to cross it,
  or the harness is structurally blind to the only bug worth finding.
- "Faster, semantics unchanged" deserves suspicion when a bound is involved. A
  candidate cap ordered by recency is not a performance knob, it is a recency
  filter: anything reachable only by vector similarity vanishes past the window
  instead of ranking lower. Silent absence beats slow every time in a benchmark
  and loses badly in a product.
- Gating an improvement behind an optional API key can mean shipping nothing. The
  graph's new extractor is real, but `use_llm=(provider == "openai")` means the
  default clone gets a byte-identical graph. Verify a feature in the
  configuration users actually run, not only the one that shows it working.
- For anything visual, look at it. The 3D graph passed "canvas is visible" while
  rendering black nodes and zero edges. Two real bugs — a material flag that
  defeated per-instance colour, and d3-force rewriting link.source from an index
  into a node object — were invisible to every assertion and obvious in a
  screenshot. Screenshot the thing and read it.
- A WebGL canvas is blank to drawImage once composited. Without
  `preserveDrawingBuffer: true` (which costs performance), reading pixels back
  after a frame is presented returns nothing, so a pixel-counting test reports a
  working renderer as broken. Playwright's screenshot captures the composited
  output and tells the truth.
- Tests that share a workspace must leave no trace. A source uploaded by a new
  spec and never deleted made an existing spec's `getByTitle("Delete source")`
  match two elements. Create-and-clean-up is the contract when the backing store
  is shared, and the cleanup doubles as coverage of the delete path.
- Kill orphaned dev servers before believing a red test run. Several "failures"
  here were servers I had killed mid-run still holding ports 3010/8010 and
  serving stale state; the same suite passed 11/11 once the ports were clear.
- A filter that is right on the write path can be wrong on the read path. The
  calendar-word guess correctly refuses to *create* a "Friday" node from
  capitalization alone, and then wrongly deleted "Friday" from the user's
  question before looking it up. Asymmetry is the point: creating something on
  weak evidence is a risk, matching something that already exists is not.
- Check that the expensive thing is actually consumed. Recall ran a vector scan
  and a graph digest on every turn, and the offline answer path took only
  `evidence` — so the whole memory context was computed and dropped. Nothing
  failed, nothing logged; the cost was invisible because the output was too.
- "Improved X by 3.1x" needs the denominator interrogated. The cache's win was
  real but measured against a baseline where the network call dominated; once
  the corpus was genuinely embedded, the local scan became the floor and the
  ratio fell to 1.9-2.2x — and on the default no-key config the feature could
  not execute at all.
- Removing a fallback turns latent path bugs fatal. `env_file=".env"` had been
  wrong for as long as `make migrate` had cd'd into apps/api, but nothing noticed
  while the app could boot without a key. The moment the key became mandatory the
  documented deploy step broke. When you delete a graceful degradation, re-walk
  every entry point that used to survive on it.
- A breaking config change needs an error that names the change. An existing
  `.env` with `MODEL_PROVIDER=auto` now dies on "Input should be 'openai' or
  'scripted'", which tells an upgrader the valid values and not that their value
  was deliberately removed.
- Brief an agent with your assumptions marked as assumptions. I asserted
  `stream_words` only served the deterministic path; it served three `/tool`
  replies too. Because the brief said "confirm before deleting", the agent
  checked and kept it. A flat instruction to delete would have removed working
  code on my say-so.
- Fix the ruler before you measure the thing. The retrieval eval scored 100%/100%
  on 2 documents and 4 questions with limit=5 — the metric could not fail, so no
  retrieval change could ever be shown to help or hurt. Rebuilding it to 22
  documents and 28 categorised questions dropped the honest score to 82.1% and
  made five specific failures visible. A benchmark that cannot go down is not
  measuring anything.
- Categorise eval questions by what they test. Collapsing lexical, paraphrase and
  indirect questions into one number hides the exact signal a retrieval change
  moves: here lexical is 100% and paraphrase is 70%, so a single average would
  have looked healthy while the half that needs dense retrieval was failing.
- Check a harness fails before trusting it passes. Stubbing search_evidence to
  return nothing had to drive every category below its floor and exit 1; without
  that check a harness that silently swallows results reports green forever.
- A development auth bypass hides the feature it is standing in for. With
  `DEV_AUTO_LOGIN=true` the browser suite never touched a cookie, so "sign out"
  could not be tested at all — a cookie-less request still resolved to the seed
  identity. Turning it off and giving the *seeded* user a password (in the e2e
  server script, not the app) kept the shared demo workspace the 11 existing
  specs depend on — its agent, its github-zen grant — while making every one of
  them exercise the real cookie-and-CSRF path. A fresh signup would have been a
  tenant with none of that furniture. Rule: when a bypass exists so a missing UI
  can be skipped, deleting it is part of shipping the UI.
- A background load that finishes late will happily undo a foreground write.
  `loadWorkspace()` fetches twelve lists in parallel and the sidebar renders
  before any of them land, so "New thread" clicked in that window created a
  conversation and then watched the in-flight (older, empty) list overwrite it.
  Snapshot the ids the load is *allowed* to replace before awaiting, and keep
  anything that appeared since — same shape as the existing
  `if (!activeConversationRef.current)` guard three lines below it.
- Default the environment to production, not development. Every relaxation in
  config.py was gated on `is_dev_env`, and `app_env` defaulted to "development" —
  so the whole set was opt-OUT. A deployment that merely forgot to set APP_ENV came
  up with a credential-free login endpoint live, demonstrated not theorised. The
  guards were all individually correct; the default inverted every one of them.
  Fail-closed means the omission is the safe case.
- A convenience endpoint becomes a vulnerability the moment it ships in the
  contract. The dev sign-in was exported in openapi.json, implemented in the
  api-client, and rendered as a button in the login UI — so it was not a local
  affordance, it was product surface with no credential and no CSRF check.
- Reviewers report the gate they ran, not the gate that exists. Two reviewers
  independently claimed "FULL GATE green, exit 0" while the tree had ruff, mypy and
  pytest failures, and neither noticed the work was entirely uncommitted. Re-run the
  gate yourself on the actual tree before believing a green report.

- Playwright's `getByRole(..., { name })` matches a *substring* by default, so
  "Boards" also matches "Dashboards" and a tab-strip assertion becomes a
  strict-mode violation the moment a sibling's label contains another's. Anchor
  with `new RegExp(`^${label}`)` rather than reaching for `.first()`, which would
  have silently asserted the wrong tab. (Two failures in the nav restructure.)
- Run `npx playwright test` from the repo root. Run it from `apps/web` and
  Playwright never finds the root config, defaults `testDir` to the cwd, and
  tries to execute the *vitest* files — the error ("Vitest cannot be imported in
  a CommonJS module") reads like a broken test, not a wrong cwd.
- When a popover's panel holds a multi-step form, put the step state in a
  component that only renders *inside* the panel. State in the parent survives
  the close and greets the next open half-filled; state in the panel is reset by
  the unmount for free.
- A "create it and take me there" action that hands the work to a self-fetching
  panel has to be a request the panel *consumes*, not a boolean it reads. A
  plain flag boots another machine every time the view remounts while it is set.
- One word must name one thing. "LaTeX" named both a *document format* (markdown
  with KaTeX maths, compiles nothing) and a *project kind* (wasmtex → PDF). A
  user picked the shallower one, got no PDF, and reported the compiler broken —
  twice. The compiler was never wrong. When two features legitimately share a
  word, the ambiguity is a bug in the product even though every unit test passes:
  rename the one that does less, and make the real one reachable from the surface
  the user actually looked at (the Create menu).
- A CSP a test cannot get around is usually the CSP working. `fetch()` of the
  preview's `blob:` URL fails because `connect-src` has no `blob:` — only
  `frame-src` does, deliberately. Wrap `URL.createObjectURL` in an init script
  and inspect the Blob instead; reading a Blob is not a fetch. Do not widen the
  policy to make an assertion convenient.
- Never `git stash` in a working tree another agent is editing. Diagnosing
  whether a flake predated my changes, I stashed the whole tree — which also
  reverted the web agent's in-flight `apps/web/**`, `packages/api-client` and
  `tasks/todo.md` for the ~90s the baseline suite ran. The pop restored it
  cleanly, but that was luck, not design. Use `git worktree add <tmp> HEAD` and
  run the comparison there: it answers the same question, touches nothing shared,
  and `git worktree remove --force` cleans up.
- Before attributing a newly-failing test to your own change, measure a rate, not
  a single run. `test_overlapping_session_creation_cannot_exceed_the_workspace_quota`
  failed twice in a row on my branch and passed three times in a row at HEAD,
  which reads as "I broke it". Ten runs in a HEAD worktree failed four times: a
  pre-existing race, and the passing baseline was the coincidence. A test whose
  passing path costs a 2s barrier timeout and whose failing path returns
  immediately will always look correlated with whatever changed the suite's
  timing.
- A response model that declares a field with a default hides a serializer that
  forgets it. `WorkflowRunOut.paused_reason: str = ""` is on the schema, in the
  OpenAPI document and in the generated client — and `_run_out` builds the model
  field by field and never copies the column, so every workflow run answers `""`
  no matter what the database says. Nothing fails; the default fills the hole.
  Rule: when a column reaches a UI through a hand-written `X_out()`, assert the
  round trip (row → endpoint → field), not just the schema — and prefer
  `model_validate(row)` over field-by-field construction for exactly this class
  of omission. Found by reading a `budget` in sqlite next to a `""` in the JSON.
- Prefer decisive evidence over a cached answer when the cache has no natural
  expiry. Working around the above by memoising "is this run held by the
  ceiling?" per run id was correct until a released run re-parked on an approval
  between two polls: the memo had no reason to expire and the graph claimed to
  be held forever. The reliable signal was structural — a budget park writes no
  `AgentToolCall` — so a run with a proposed call is parked on a person, and the
  fetched list only decides when there is no proposal at all.
- A descendant selector reaches into a popover. `.budget-hold-foot p { flex: 1 1
  240px }` was written for one paragraph beside a button, and the disclosure
  panel rendered *inside* that foot is itself a column flex container — so the
  "ask an owner" sentence inherited a 240px flex-basis along the vertical axis
  and sat in a 266px box of whitespace. Every gate was green; only the
  screenshot showed it. Scope layout rules meant for a component's own children
  with `>`.
- Fixing a serializer promotes a mirror column from decoration to contract.
  `WorkflowRun.paused_reason` mirrors the backing run's, and every writer
  maintained it except the resume path: `resume_after_agent_turn` returned early
  on `Paused` because "agent_loop has already written which kind of waiting it
  is" — true of the `Run`, false of the mirror. It never showed while `_run_out`
  dropped the field; the moment the field reached the client, a graph released
  from the ceiling that walked on to a write rendered the spend panel *instead
  of* the approval card, so the proposal was on screen with nothing to decide it
  with. Rule: when two rows must agree, the paths that change one and not the
  other are the whole bug surface — enumerate the writers, not the readers. And
  a status that does not change between two states (`waiting_for_approval` for
  both parks) means the *reason* is the only thing carrying the difference, so
  every transition between them has to move it.
- Order a UI's signals by which one can go stale, not by which one is most
  specific. The rule "a proposed `AgentToolCall` means a person is being waited
  on" was already in the code, but *below* `paused_reason` — so it only guarded
  against a stale fetched list and not against the stale field, which is the one
  that actually lied. Structural evidence that cannot lag belongs first;
  mirrored fields and cached lists belong behind it.
- One failing e2e test is not one failure. The budget spec parked a real write
  and saved a workflow before the assertion that failed, so its inline cleanup
  never ran, and the residue produced two more failures in files that come later
  in the suite order — a duplicate workflow row, and a workspace-wide
  `create_document` proposal that put a second card in another spec's
  `.document-pending`. Diagnose the *earliest* failure first and expect the rest
  to evaporate; and put a shared-workspace spec's cleanup in `afterAll`, where a
  failed assertion cannot skip it, rather than at the end of the test body.
- A cleanup that only ever runs on a clean workspace is untested code. The new
  `afterAll` sweep looked right and was half broken: `DELETE /api/conversations`
  requires an `Idempotency-Key` and the two routes beside it do not, so that one
  call 422'd into a response nobody read and the conversation survived. It was
  only visible by injecting a deliberate failure into the spec and watching
  which of the three leaked things came back. Make the mess on purpose before
  believing the tidy-up.
- Three separate things had to be true before any chart the sandbox drew could
  be seen, and each was invisible on its own. The descriptor carried no address;
  the web `SandboxArtifact` type still described an inline-base64 contract the
  server had stopped honouring, so `artifact.data` was `undefined` and the panel
  rendered `null`; and our own `img-src 'self' data:` CSP blocked the blob: URL,
  logging to a console nobody was reading. Fixing the first two produced a
  broken-image icon that `toBeVisible()` passes on. Rule: for anything visual,
  assert a property only a *working* render has — `naturalWidth > 0`, a non-zero
  canvas, a computed colour — and look at the screenshot.
- A type that describes a contract nobody validates drifts silently and takes a
  feature with it. `SandboxArtifact` declared `url?` and `data?` as optional, so
  when the server switched to object-store descriptors, every consumer's "do I
  have bytes?" check answered no and nothing failed anywhere. The fix was to
  type the response model server-side (`ToolArtifact`) so it lands in the
  OpenAPI, and to make `url` required on both sides — an optional field is a
  place for two systems to disagree forever.
- `<img src>` to an authenticated API on another origin does not work and cannot
  be made to work by widening CORS. It carries no `X-Workspace-Id`, so the API
  falls back to the caller's oldest membership and 404s for anyone in two
  workspaces; and it is a third-party subresource whose cookie Safari blocks and
  Chrome is withdrawing, `SameSite=None` notwithstanding. Fetch through the API
  client and hand the `<img>` a blob: URL. Note the local e2e stack cannot
  detect either problem — `127.0.0.1:3010` and `127.0.0.1:8010` are the same
  *site* — so this is a case where a passing browser test proves nothing about
  production and the reasoning has to carry it.
- A truncated summary is not a place to keep structured facts. `result_preview`
  is clipped to 500 characters and the sandbox renderer puts the artifact ids
  *last*, so on any chatty run the ids were the first thing cut — parsing them
  back out would have worked in every test and failed in the field. Give the
  structured thing its own column.
- Do not render a verdict the checker declined to make. `CitationReport.is_valid`
  is true when passages go uncited — the model is told to cite claims, not to use
  everything — so the first cut, which painted "cites nothing" in warning amber
  with `role="alert"`, was the UI inventing a violation. Read the validator's own
  notion of failure and let the styling follow it exactly.
- The e2e cleanup check has a second half worth doing: inject a failure that
  skips cleanup and confirm *only* your specs fail. It confirmed these three are
  self-contained, and it also surfaced a latent flake — a reload assertion that
  silently depended on which conversation the shell happens to open. Assert
  through an explicit selection, not through someone else's default.
- "The tree is yours" is a claim to verify, not to trust. A session that started
  on a clean `git status` found, an hour in, 40 modified files it had not
  touched — a second agent working on generated-app backends and workflow
  inputs, editing `views/chat.tsx`, `globals.css` and `schemas.py` in the same
  minutes. Nothing crashed; both sets of edits happened to land in different
  regions of the same files, which is luck and not a mechanism. The tell is
  cheap and should be routine before a large change: `find apps packages -type f
  -newermt "<session start>"` and compare against what you have written. If it
  is not empty, stop before touching a shared file — a whole-file Write by
  either side silently destroys the other's work, and the loser finds out from a
  test failure that names the wrong culprit.
- Sequence a multi-part feature backend-first when the parts share a service.
  Parts 2 and 3 here both needed `documents.py` and the approval path, and doing
  the whole backend before any UI meant that when the session had to stop early,
  what existed was a complete, migrated, tested API rather than three half-wired
  vertical slices. The frontend is the resumable half; the schema is not.
- A partial approval must not be written over the model's own arguments.
  `AgentToolCall.arguments_json` is the record of what was *asked for*; the
  hunks a human accepted are the record of what was *allowed*, and they belong
  on the audit row and in the resume payload (`LoopState.pending_calls[0]
  ["amendment"]`), merged into the arguments only on the way to the executor.
  Conflating them loses the ability to say what the model proposed.
- A lint warning is a symptom; read what it points at before calling it
  pre-existing. Three separate track reports this session dismissed the same two
  `no-unused-vars` warnings as "pre-existing, in another agent's file". They were
  neither pre-existing nor cosmetic: at the baseline commit both symbols were
  *used*, and a stray trim pass had deleted the UI that consumed them — the
  "Live" pill on the audit log and the model-provider pill in the header — while
  leaving the destructured prop and the entire CSS rule behind. An unused
  variable beside live CSS for a class nobody renders is the signature of an
  accidental deletion, not of dead code. The check costs one command:
  `git show HEAD:<file> | grep <symbol>`. If the symbol was used at HEAD and is
  unused now, a feature was removed, and nobody wrote that down.
- "Pre-existing" and "another agent's" are claims with an owner, and copying them
  from a sibling report launders them into fact. Each of the three reports plausibly
  read it from the previous one. Verify against the commit, not against the
  neighbouring summary — a shared tree makes attribution *harder* to inherit,
  not easier.
- The lint-warning lesson above has a second half: the fix is to restore what was
  deleted, not to delete the rest. Following that rule caught the regression this
  session — `sources` was unused in `chat.tsx` because a trim pass had replaced the
  composer's `placeholder={…}` with `placeholder=""` — but the first thing I did
  with the finding was remove the now-unused prop from three files, which would
  have made the deletion permanent and tidy. `git show HEAD:<file> | grep <symbol>`
  is the check; it only works if you run it *before* you clean up, not after.
- An `aria-label` and a `placeholder` are not substitutes for each other. A pass
  that added accessible names to four composers blanked all four placeholders on
  the way past, because the e2e locators had been switched from `getByPlaceholder`
  to `getByRole("textbox", {name})` and the placeholder looked redundant. It is
  not: the accessible name is for a screen reader, the placeholder is the empty
  state's only instruction, and one of the four was the sole example anywhere in
  the product of how to phrase a workflow. Every gate stayed green through the
  deletion — the tests had just stopped looking at the thing that broke.
- A migration guarded on the way up must be guarded on the way down. `0001_initial`
  calls `Base.metadata.create_all()`, so a database migrated from empty arrives at
  revision 0001 holding every column in today's `models.py`, and every later
  `if not exists` guard correctly does nothing. Any downgrade that unconditionally
  drops what its upgrade conditionally added then fails on exactly the databases
  that were built cleanly from scratch. It also means `upgrade head` from empty
  proves far less than it appears to: it exercises `models.py`, not the DDL.
- "Verified the migration round trip" needs a stated range. A prior report claimed
  upgrade → downgrade → upgrade at head; the chain was in fact broken four
  revisions down, because only the top step had been run. `downgrade base` is the
  claim worth making, and it is one command.
- When a shared e2e suite fails, ask which spec *created* the condition before
  fixing the spec that tripped over it. The failure named `workspace.spec.ts`; the
  bug was `dashboards.spec.ts` never deleting its upload, because that DELETE needs
  an `Idempotency-Key` its sibling deletes do not and the helper discarded the
  status. Fix both ends — the leak at its source, and the over-broad locator that
  made an unrelated leak fatal — then re-inject the leak to confirm the failure now
  lands on the spec that caused it.
- A cleanup block that deletes without asserting is a cleanup block that has
  probably stopped working. This one had been silently failing for its whole
  existence and was only visible as a strict-mode violation two files later.
- A commit message is a claim about the tree, not the tree. f57547f's message
  says "the document chat panel and the todo/approval-mode UI are NOT in this
  commit"; `git show --stat f57547f` lists `use-document-thread.ts`,
  `views/todos.tsx`, `views/approval-mode.tsx` and both their e2e specs among the
  files it *adds*. A session briefed from that prose spent its first hour ready
  to rebuild ~1,800 lines that were already there and already green. The check is
  two commands and belongs before the plan, not after it:
  `git show --stat <base> -- <area>` and `git log --oneline -- <the file the
  feature would live in>`. If the file's only commit is the base, the feature
  landed in the base — whatever the base says about itself.
- A post-connect peer check is not a substitute for a pre-connect one, and where
  it sits changes what it can promise. On `execute_read_only_get` the request
  carries nothing, so refusing after the connect loses nothing. On the OAuth token
  leg the authorization code and client secret are already on the wire by the time
  the peer is knowable, so the same check can only stop the *reply* being believed
  — it cannot un-send the credential. Write that limit into the comment; a guard
  described as preventing something it cannot prevent is how the next reader
  decides the door is shut.
- `key` is a correctness feature, not a list-rendering chore. `DocumentReview`
  holds the reviewer's staged per-hunk rejections in its own state; rendered
  unkeyed, a second proposal arriving for the same document reuses the instance,
  so the new diff opens with hunks crossed out that nobody crossed out and the
  Apply count agrees with them. Any component whose state is only valid for one
  identity of its props needs `key={that.id}` — and the test for it has to render
  the *parent* and rerender with a new id, because the reset is React's job and
  the child alone cannot show it.
- A client that reads a field the server never sends fails silently and
  confidently. `handlers/thread.ts` read `data.approved_by_mode` off the tool
  stream events with a comment calling the event "the authority on it";
  `agent_loop.py` put it in no payload. So the bypass banner said "Nothing has
  gone through unreviewed yet" for the whole of every turn in which writes were
  going through — correcting itself only at the settle-time refetch, which a run
  that parks or fails never reaches. Grep both ends of any field a comment claims
  arrives on an event: `grep -rn "<field>" apps/api apps/web` should hit a writer
  as well as a reader.
- A SQLAlchemy column `default=` is evaluated *during* the INSERT, not at
  construction. `Organization(name=...)` leaves `.id` as `None` in Python, so a
  `before_flush` listener that wrote `workspace.organization_id = org.id` wrote a
  NULL and every `Workspace()` in the suite failed the NOT NULL. Anything that
  needs an id before the flush — a listener wiring two new rows together, a
  return value, a log line — must pass `id=new_id()` explicitly.
- Changing the signature of a function tests inject around breaks the tests, and
  that is a design signal rather than a chore. Adding `db` to `_default_model_step`
  to enforce the org's harness bound failed four suites whose `model_step=` lambdas
  take three arguments — and the same fact meant the bound would have been skipped
  by every injected step, including the workflow executor's. The check belonged
  *above* the seam, on the path every turn takes. If a guard has to go inside a
  replaceable seam, ask who replaces it.

- A tool that reads a "user-typed" value out of its own arguments is forgeable
  by construction: the model authors `arguments_json` end to end, so any
  in-band key or flag ("answer", "answer_from_user") is the model's to write.
  The only sound channel for human-typed input is out-of-band — the decision
  amendment — and the executor's argument surface has to be SANITIZED of the
  reserved key at the execution boundary, not merely documented as human-only.
- An auto-approval gate that runs after the policy decision cannot see where
  the decision came from, and provenance is the whole question: a default
  "ask" may be softened, a person's or an org's explicit "ask" may not, and
  the two are the same string by the time they reach the park site. Compute
  the provenance where the rows and the ceiling are already in hand (a flag on
  the Verdict), never re-derive it downstream.
- read-max-then-insert against a unique (parent, sequence) key is fine with
  one writer and a latent bug with two; a feature that adds a second routine
  writer (steer, beside the loop's own deltas) is what converts "rare cancel
  race" into "user action kills the turn". The root fix is making the
  sequence assignment atomic in the INSERT itself (scalar subquery), which
  also quietly fixes every pre-existing racer.
- A worker thread that cannot write the observability record must hand back
  the EVIDENCE instead of a clean verdict sentence, so the serial path can
  make the record: a child screen hit that returns only "content failed the
  screen" erases the detection it is reporting — no event, no escalation, no
  scorecard count. Carrying the flagged excerpt in the result lets the
  parent's existing screening write all three.
- With 10 concurrent sessions in one tree, the discipline that worked: land
  shared contracts serially yourself, give fan-out agents only new files,
  Edit (never Write) shared files with freshly-read anchors, and grep your
  key symbols across every contested file before calling the work done. Two
  concurrent-session collisions were survived this way; a peer even hardened
  my migration in place (absent-table guard) — take such edits as current
  state, not as damage.
- The e2e/vitest gate in a many-session tree can be red for someone else's
  half-written test file; re-run it yourself before diagnosing (608/612 with
  exit 1 became 612/612 exit 0 five minutes later with zero changes of mine).

- `json.loads` accepts NaN/Infinity by default and Starlette's JSONResponse
  dumps with allow_nan=False, so any value echoed from a request body into a
  JSON response is a latent 500: the parse succeeds inside your try, the crash
  happens at render time OUTSIDE it. Reject non-standard constants at the parse
  boundary (`parse_constant=` raiser) for any endpoint that reflects
  request-controlled scalars — a robustness test that only tries int/string/null
  ids sails past it. Found by adversarial review after my own malformed-body
  test missed exactly this input class.
- A "carry the evidence back" fix has to cover every EXIT, not just the happy
  path. The child screen-record fix worked on the clean-answer return and was
  silently dropped on the abort return, because the two paths built their
  ToolResult independently. When a value must ride out of a function, route
  every return through one helper that attaches it — here `_notice_first`, used
  by both the answer and the abort — rather than remembering to attach it at
  each `return`.
- When a downstream stage clips content to a budget, the thing that must
  survive belongs at the FRONT. The shadow-screen notice sat at the tail of the
  child result and best-of-N clipping (3600//N chars) truncated it away before
  the parent re-screened; leading with it makes "the one thing clipping keeps"
  and "the one thing that must survive" the same thing.
- A stalled review workflow returns `confirmed: []` that means "never ran", not
  "clean" — the agents timed out under a loaded box (8 peer sessions). Re-run it
  smaller (fewer agents, medium effort, no verify fan-out) AND self-verify the
  highest-risk surfaces by hand in parallel; the lighter re-run then found the
  real NaN-500 the stall had hidden.
- Size a tool's payload to ITS transport, not to a lookalike surface's limits.
  graph_export copied GET /api/graph's row caps (200 entities/800 edges), but
  tool content rides through bounded_content()'s 4000-char clip that the HTTP
  route never meets — a full-size export was cut mid-JSON while its head-of-dict
  `truncated: false` flags survived, lying to the client. Fit the serialized
  payload to MAX_RESULT_CHARS before returning (drop items, set the flags), and
  the regression test must round-trip through `bounded_content()` on a graph big
  enough to overflow — my unit tests called the executor directly and my e2e
  used a 3-entity graph, so both sailed past the clip. Caught by QA review.
- A tenant-isolation suite with per-tool cases is a CHECKLIST: adding a tool
  means adding its two-tenant case in the same commit, and a bulk-read tool
  (one call = the whole map) needs it most, precisely because the walk-tool
  cases next to it already existed and made the gap easy to miss.
- "Deployed" is not "reachable": verify the URL a user would type, not the one
  the tool prints. I reported grain.natejly.com live because `vercel deploy
  --prod` printed "Aliased https://grain.natejly.com" and a curl of
  `grain-web-ten.vercel.app` returned 200 — but the apex zone had no DNS record
  for `grain` at all, so the real hostname was NXDOMAIN for the whole session
  and the user hit a dead site twice while I described it as working. A Vercel
  domain can be attached AND verified in the project API and still not resolve;
  in a Vercel-managed zone the subdomain needs its own `CNAME -> cname.vercel-
  dns.com`. Curl the customer-facing hostname before saying it is up, and when
  the user says "it isn't working", re-probe rather than re-explaining the
  wiring — the wiring was in fact correct, and the missing record was one dig
  away the whole time.
- A schema constraint that only one engine enforces is a production-only bug
  waiting for the first real deploy. Alembic hardcodes
  `alembic_version.version_num` as VARCHAR(32); three revision ids in this tree
  are longer (up to 42 chars). SQLite ignores VARCHAR lengths, so 63 migrations
  passed locally and in CI forever, and the chain died at 0013 -> 0014 on RDS
  with StringDataRightTruncation. Fix in `alembic/env.py` by widening (or
  pre-creating) the version table before `run_migrations()` — renaming applied
  revisions would break every database that already recorded them. General
  rule: when dev is SQLite and prod is PostgreSQL, prove the migration chain
  against a real postgres container before calling a deploy done; one local run
  found it in 90 seconds after three ~10-minute ECS round-trips found it once.
- A swallowed exception does not protect you from a blocked port. `send_quietly`
  wraps SMTP in `except Exception` precisely so mail cannot break signup — but a
  closed security-group egress does not raise, it blackholes the SYN, and
  smtplib then waits out its 15s timeout for connect, EHLO and login in turn.
  Signup took 45 seconds and the UI sat on "Working…"; every API-level check I
  ran passed, because `/health` sends no mail and `curl --max-time 25` cut the
  request off before it could tell me. Two habits from this: when a request
  "hangs", measure it with a generous timeout and read the number, and when an
  app is moved behind a restrictive egress policy, enumerate every outbound port
  the code can use, not just the ones the happy path needs.
- Reproduce user-facing bugs in a real browser, not with curl. curl proved
  signup+login worked at the API; the browser showed the form stuck on
  "Working…" with the POST sent and no response — the actual complaint. Driving
  Chromium with Playwright while logging every response and console error found
  it in one run, and also proved the fix by watching the UI reach the workspace.
- `NEXT_PUBLIC_*` is inlined at build time, so changing the env var is not
  enough — a cached Vercel build keeps the old value. Use `vercel deploy
  --force` after changing one, and verify by reading the deployed page's CSP
  `connect-src` rather than trusting the dashboard.
- When a manual workaround is needed to make a deploy work, that workaround IS
  part of the procedure — put it in the automation immediately, in the same
  sitting. I discovered by hand that the API task (1024 MiB) and the migrate
  task (512 MiB) cannot coexist on a t4g.small (ECS registers 1334 MiB), and
  scaled the service to 0 to get the migration through. Then I wrote
  deploy-uat.yml and deploy-prod.yml with a bare `run-task` and no window, so
  both would have failed on their first run — `run-task` returns empty tasks[]
  with a RESOURCE:MEMORY failure, `--query 'tasks[0].taskArn'` yields the string
  "None", and `aws ecs wait` dies on "taskId length should be one of [32,36]".
  A peer QA session caught it before the merge. The tell I should have heeded:
  I had already written the scale-to-0 dance twice in throwaway scripts.
- Query a placement failure, don't infer it from a hang. `describe-container-
  instances` reporting remainingResources MEMORY=310 against a 512 MiB request
  is a one-command answer to "why won't this task start", and the same call
  exposes stale accounting (memory still reserved with runningTasksCount=0),
  which an ECS agent restart clears.
