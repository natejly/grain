# Harness-parity plan (drafted 2026-08-25; supersedes visual-harness-plan.md)

Goal: make Grain's agent ENCAPSULATE the functionality of Claude-Code-class
harnesses (Claude Code, Grok bot): a real file/search tool surface,
agent-callable web fetch, background + polyglot execution, deeper subagent
orchestration, context compaction, checkpoints/rollback, hooks,
model-discoverable skills — and then render it all visually (the previous
plan becomes the final phase).

Inventory baseline (2026-08-25): ~52 ToolSpecs across 12 families
(`services/llm_tools.py:317-451`); persistent sandbox with `run_command` /
`run_python` (`sandbox/tools.py:705-798`, session reuse in
`sandbox/session.py:257`); `delegate` with best-of-N + parallel batch
(`delegation.py:528`, `agent_loop.py:1290`); plan mode + 5 approval modes
with two-axis grants (`agent_loop.py:298-325`, `models.py:1231`); MCP client
(`mcp/registry.py:189`) and server (`api/mcp_server.py:175`); authored
agents, custom sandbox tools, skills, memory. Confirmed gaps listed per
phase below.

## Phase 1 — File & search surface (the core coding-harness tools)

Today: fs_* tools cover only a flat DB-backed project VFS (one project, no
glob/grep/multi-read, `projects/tools.py:363-489`); the sandbox FS is
reachable only via shell; content search is embeddings-only.

- [ ] 1.1 `fs_glob` + `fs_grep` (regex, ripgrep-style output: path:line +
      context) + multi-path `fs_read` over the project VFS; cross-project
      variants honoring the caller's visibility.
- [ ] 1.2 Typed sandbox file tools sharing the same names/shapes:
      `fs_*` gain a `target: project|sandbox` root, so read/edit/glob/grep
      work identically on a real tree (e.g. a repo cloned in the sandbox)
      with `fs_edit`'s existing exact-string-replace + diff-preview
      semantics. Implemented via short `run_command`-family execs under the
      hood; previews for sandbox edits ride the same `ToolSpec.preview`.
- [ ] 1.3 `git` affordance in the sandbox: not new tools — a doc'd recipe +
      default-allowlist entry for github.com egress on `allowlist` policy,
      and `sandbox_upload` accepting a project as a directory (exists —
      verify round-trip project→sandbox→project).
- [ ] Gate: pytest on the new tools (VFS + fake sandbox provider), grep
      output budgeted against MAX_RESULT_CHARS with head-clipping that says
      what was dropped (lesson: no silent caps).

## Phase 2 — Web fetch (agent-callable)

Today: hosted `web_search` only; the SSRF-hardened fetcher
(`services/tools.py:88-134`: HTTPS-only, redirect cap, post-connect peer
check, size cap) serves only the legacy `/tool` path.

- [ ] 2.1 `web_fetch(url, prompt?)` ToolSpec wrapping that fetcher —
      html→markdown conversion, result budgeted, `read_only=True` but
      default-ask via policy row (exfiltration channel), respecting the
      workspace egress posture. Screen fetched content through the existing
      prompt-injection screen before it reaches the model.
- [ ] Gate: unit tests reusing the fetcher's SSRF suite; injection-screen
      test with a hostile page fixture.

## Phase 3 — Execution upgrades

Today: Python + shell only, synchronous, 120s timeout, 20 execs/run,
sandbox disabled by default.

- [ ] 3.1 Background execution: `run_command(background=true)` returns a
      task id immediately; new `task_output(id)` / `task_kill(id)` tools
      poll/stop it (Claude Code's background-bash shape). State on the
      SandboxSession row; reaped with the session. Long builds/servers stop
      burning the 120s ceiling.
- [ ] 3.2 Node/TypeScript runtime in the sandbox image (run_command already
      covers it once node is present — image/bootstrap change, not a tool).
- [ ] 3.3 Enablement pass: sandbox on by default in dev compose with
      `allowlist` egress + pypi/npm/github defaults; production stays
      opt-in fail-closed (lesson: default to production posture).
- [ ] Gate: background task lifecycle tests incl. timeout-kill and
      session-reap orphaning; e2e that starts a server in the sandbox and
      fetches from it via run_command.

## Phase 4 — Orchestration depth

Today: delegate picks agent only (child inherits model/effort,
`delegation.py:225`); children are read-only, depth 1, event-invisible;
only delegate parallelizes (`agent_loop.py:1499`).

- [ ] 4.1 `delegate(model?, effort?)` overrides, validated against the org's
      harness bound ABOVE the model_step seam (lesson: guards inside
      replaceable seams get skipped).
- [ ] 4.2 Parallel drain for read-only tool batches generally (not just
      delegate): same allow-verdict-only condition, same executor pool —
      parallel greps/reads/fetches.
- [ ] 4.3 Write-capable children (DECISION NEEDED): children whose proposed
      writes park the PARENT run with the child's card surfaced for
      approval, riding the child-event envelope (visual plan A5). Larger
      lift; alternative is keeping children read-only and letting the
      parent replay writes. Plan assumes deferred until the presentation
      phase can show child transcripts.
- [ ] Gate: parallel-drain race tests (atomic sequence INSERT already
      covers event writes); delegation depth/bound tests.

## Phase 5 — Session state: compaction & checkpoints

Today: MAX_ITERATIONS=6 per turn, cross-turn context is last-N messages at
600 chars each (`runs.py:55-70`); zero rollback once a write lands.

- [ ] 5.1 In-turn compaction: when LoopState.input_items nears the model's
      context budget (or iteration cap), summarize the oldest tool
      results/exchanges into a compact digest item (keeping pending_calls
      intact — LoopState is already serialized, so compaction is a
      transform on it) and raise MAX_ITERATIONS (6 → configurable ~24).
      Must preserve the forged-answer/screen-notice invariants (notices
      lead, lesson: the thing that must survive clipping goes first).
- [ ] 5.2 Cross-turn: replace the flat 600-char clip with digest-aware
      selection (recent turns verbatim, older turns via the existing
      conversation-index summaries).
- [ ] 5.3 Checkpoints: a `RunWrite` audit row per successful write tool
      (tool, object type/id, before-version pointer) — documents/skills
      already have versions; boards/dashboards/todos get a before-snapshot
      JSON on the row. `POST /api/runs/{id}/revert` restores, newest-first,
      refusing when a later non-agent edit touched the object (report,
      don't clobber). UI hook lands in the presentation phase.
      Non-revertible tools (sql_execute, MCP writes, sandbox side effects)
      are marked so on the row — never claim an undo that can't happen.
- [ ] Gate: compaction round-trip tests (park/resume mid-compaction),
      revert tests incl. the conflict path, alembic up/down.

## Phase 6 — Extensibility: skills, hooks, agent config

- [ ] 6.1 Model-discoverable skills: `list_skills` + `use_skill(name, args)`
      ToolSpecs — use_skill splices the skill body into the CURRENT turn
      (system-prompt append on next iteration), honoring versions; stop
      ignoring `allowed-tools` frontmatter (narrow-only intersection, like
      agents).
- [ ] 6.2 Hooks: per-workspace pre/post tool-call hooks that run IN THE
      SANDBOX (never host — hooks are arbitrary commands), matching on tool
      name glob; pre-hook nonzero exit converts the verdict to deny with
      the hook's stderr as reason, tighten-only like force_ask. Owner-only
      to configure.
- [ ] 6.3 Authored agents gain `model`, `effort`, `approval_mode` columns
      (narrow-only vs org bound); agent-creator UI follows in presentation.
- [ ] Gate: hook matrix tests (allow/deny/timeout/missing sandbox),
      skill-splice park/resume test, migration up/down.

## Phase 7 — Presentation layer (the prior visual plan, updated)

The superseded visual-harness-plan.md phases A–D apply nearly verbatim,
now rendering the new surface too: typed event union; thinking + guardian +
usage events; child-event envelopes (needed by 4.3); turn-tree reducer;
shiki highlighting; per-tool renderers (grep results list, terminal pane
for run_command incl. background task tail, web_fetch card, checkpoint/
revert affordance on write cards); live activity timeline; pinned plan
panel; nested delegate transcripts with best-of-N tabs; in-chat usage
meter. Screenshot-review every visual change.

## Sequencing

1 → 2 → 3 are independent of each other after 1.1 lands (fan-out safe, one
track per family file). 4.2 before 4.1. 5 and 6 independent. 7 last but its
Phase-A event contracts can land any time after 4's decisions. Backend-first
within every phase (lesson: the frontend is the resumable half). Contracts
(new columns, event models, tool names) land serially before any fan-out.

## Decisions needed from you

- 4.3 write-capable children: bubble-to-parent approvals, or keep children
  read-only for now?
- 5.3 checkpoint depth: before-snapshots for board-family objects
  acceptable, or documents-only first?
- 3.3 default-on sandbox in dev: yes/no.

## Full gate per phase

make lint · pytest · pnpm test · pnpm build · pnpm test:e2e · alembic
upgrade/downgrade base on a scratch DATABASE_URL for schema phases.
