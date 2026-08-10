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
