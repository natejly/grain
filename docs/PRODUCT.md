# Product segmentation

One platform, one codebase, two packages. This document lists what each segment
needs, and marks honestly what already exists versus what is a real build.

Legend: **[built]** works today · **[partial]** substrate exists, needs finishing ·
**[new]** genuine build.

---

## The shared spine

Everything below is common to both segments and already works. Segmentation is
mostly packaging plus a small number of real builds — not two products.

| Capability | State |
|---|---|
| Streaming chat with an agent loop over 32 tools | built |
| Tool approval with a diff preview before any write | built |
| Durable resumable runs (survive disconnect and restart) | built |
| Memory: extraction, recall, remember/forget | built |
| Knowledge graph with typed relations + 3D view | built |
| Documents: markdown + LaTeX math, version history, agent editing | built |
| LaTeX projects compiling to real PDF, offline, in-browser | built |
| Code sandboxes (multi-file, esbuild-wasm, locked-down iframe) | built |
| Kanban boards | built |
| Sources + cited retrieval with passage provenance | built |
| Datasets and dashboards (DuckDB) | built |
| MCP connectors, database connectors, OAuth integrations | built |
| Workspaces, membership, invites | partial (auth in flight) |
| Audit events on every action | built |

### The value proposition
"A platform that learns from you" is not aspirational here — the memory system is
real, workspace-scoped, and the agent can read and write it deliberately. That is
the through-line for both segments; what differs is what it learns *about*.

---

## Students

The wedge is **LaTeX and documents**, which are genuinely better here than in a
general chat product: real TeX Live compiling to PDF in the browser, offline, with
an agent that can edit the source and show you a diff before it applies.

### Already there
| Feature | State |
|---|---|
| LaTeX editor with live PDF compile | built |
| Markdown documents with KaTeX math and live preview | built |
| Version history with restore on every document | built |
| Agent edits shown as an inline diff, approved before applying | built |
| Upload papers and ask cited questions about them | built |
| Memory that accumulates across a term | built |
| Kanban for coursework and deadlines | built |
| Code sandboxes for problem sets | built |

### To build
| Feature | Effort | Note |
|---|---|---|
| Bibliography management — BibTeX/Zotero import, `\cite` autocomplete | new | The single most-requested LaTeX feature after compile. `refs.bib` already exists in the starter. |
| tikz / beamer / biblatex support | new | Blocked on the asset tier: core is 79 MB, the tier carrying these is 506 MB. Needs a middle tier or lazy per-package fetch, which conflicts with offline. **Decide deliberately.** |
| PDF/DOCX export of documents | new | Documents export as `.tex` today; students need Word for submission. |
| Paper ingestion — arXiv/DOI URL → source | new | Small; the ingestion pipeline already handles PDFs. |
| Study aids: flashcards, spaced repetition, quiz generation from sources | new | Natural agent tools over existing memory + sources. High perceived value, low build cost. |
| Real-time collaborative editing | new | Expensive (CRDT or OT). Versioned async editing may be enough — validate before building. |
| Citation checking — does this claim match the cited source? | new | Uses the existing provenance machinery. Genuinely differentiated. |
| Academic integrity posture | new | Not a feature: a stated position on what the tool will and won't write. Ship it as documentation. |

### Cost posture — decided
Every install requires a key, and **API spend is not a design constraint**. The
product optimises for quality. This is a real decision with teeth: it means we
spend tokens freely at ingest and on the write path where they buy quality, and it
removes cost as an argument against several techniques in `RESEARCH.md`.

It does **not** remove latency as a constraint. A user waiting is a product
problem regardless of the bill, which is why reranking stays deferred (2-5s per
turn) even though we no longer care what it costs.

Cost accounting is therefore demoted from a pricing blocker to an enterprise
observability feature — still worth building, no longer on the critical path.

---

## Enterprise

The wedge is **governance**, and it is unusually strong by accident: this system
already refuses to let an agent write anything without showing the user a diff and
recording it. Most AI chat products cannot tell that story.

### Already there — the governance spine
| Feature | State |
|---|---|
| Every agent write is approval-gated with a preview of the change | built |
| Per-tool policy: `ask` / `allow` / `deny`, per workspace | built |
| Audit events on every action, with actor, resource and detail | built |
| Workspace scoping on every artefact | built |
| Read-only-by-default database access with four layers of enforcement | built |
| Secrets encrypted at rest, never returned by the API | built |
| Sandboxes with no network egress by default; server-side execution is opt-in and runs in a container with no network, a read-only root and all capabilities dropped (ADR 0005) | built |
| MCP servers default to ask-before-running | built |

That list is a compliance narrative already. It needs surfacing, not inventing.

### To build
| Feature | Effort | Note |
|---|---|---|
| **RBAC** | partial | `Membership.role` exists but only owner/member, and `require_owner` is the only gate in the codebase. Needs a real permission model: roles → permissions → enforcement at `get_actor`, plus per-resource sharing. |
| **Admin console** | partial | `/api/admin` and a web view ship, covering usage and budget. Still missing: user and member management, workspace list, seat usage, policy defaults set org-wide rather than per workspace. |
| **Workflow creation** | built | Compiled from a description into a DAG and executed on the agent run loop, on a schedule, at a `workflow` policy scope narrower than a workspace (ADR 0007). |
| **Agent creator** | partial | `Agent` exists with name/instructions/enabled. Needs: per-agent tool allowlists (today `ToolPolicy` is per *workspace*), model and effort selection, skills, and a test harness per agent. |
| **Monitoring and observability** | partial | `RunEvent` records every step, `AuditEvent` every action, and `ModelUsage` every model call — tokens, model, operation and `cost_usd` (null, never zero, for an unpriced model), surfaced at `GET /api/admin/usage`. Missing: latency percentiles and tool success rates. |
| **Prompt management + A/B testing** | new | Versioned prompts, assignment per workspace or per cohort, and outcome comparison. **Blocked on measurement** — see below. |
| **SSO / SAML, SCIM provisioning** | new | Table stakes above ~200 seats. The OAuth work in flight is the foundation. |
| **Data retention and residency controls** | new | Per-workspace retention windows, deletion guarantees, export. |
| **Usage quotas and cost controls** | built | Per-workspace spend caps (`WorkspaceBudget`), per-window USD and token ceilings, unattended runs held to a narrower fraction, and a run *parked* rather than killed when it hits one (ADR 0008). |
| **Compliance export** | partial | Audit data exists; needs an export path and a documented schema. |
| **Private / VPC deployment** | partial | Container-deployable, but object storage and secrets management need finishing. |

---

## The dependency almost everyone misses

**A/B testing and prompt monitoring cannot ship before measurement exists.**

We just learned this the hard way with retrieval: the eval harness scored 100% on
four questions and could not fail, so no change could be shown to help. Rebuilding
it to 28 categorised questions dropped the honest score to 82.1% and made five
specific failures visible.

The same applies here, doubled. To A/B a prompt you need:
1. An outcome metric per turn that is not "the user did not complain".
2. A way to attribute an outcome to a variant.
3. Enough volume for the difference to clear noise.

We have an unusually good candidate for (1) sitting unused: **the approval stream**.
Every proposed write is explicitly judged by a human — approved or denied. That is a
clean labelled signal most products do not have. Approval rate per prompt variant is
a real metric, available today, with no annotation cost.

**Build order for the enterprise measurement story:**
1. ~~Token and cost accounting per run~~ — **done** (`ModelUsage`, ADR 0008)
2. Run observability UI over the existing `RunEvent` data
3. Outcome metrics, starting with approval rate and regeneration rate
4. Prompt versioning and assignment
5. A/B comparison

Steps 1–3 are useful on their own. Steps 4–5 are worthless without them.

---

## What I would not build

- **Two codebases.** `Workspace` already carries the right shape for a tier flag.
  Fork the packaging, never the product.
- **Real-time collaborative editing, yet.** Expensive, and versioned async editing
  with agent diffs may already cover the need. Validate first.
- **A general workflow builder before a narrow one.** Saved parameterised agent
  runs solve most of the demand at a fraction of the cost of a visual DAG editor.
- **SSO before RBAC.** SSO tells you who someone is; RBAC decides what they may do.
  Doing them in the wrong order gives you authenticated users with unbounded access.
- **AI-detection or plagiarism scoring.** Unreliable, and it puts the product in an
  adversarial relationship with its own users.

---

## Sequenced

**Now** (both segments, shared) — all three have since shipped
1. ~~Finish auth and the cross-tenant isolation audit~~ — done; the audit is a
   standing test that fails when a route joins the app without a verdict
2. ~~Hybrid retrieval~~ — done; BM25 + dense + RRF, on by default
3. ~~Token and cost accounting~~ — done; `ModelUsage` plus enforced ceilings

**Next** (student)
4. Bibliography management and PDF/DOCX export — the two concrete asks the LaTeX
   work does not yet cover
5. Study aids as agent tools — cheap, high perceived value

**Next** (enterprise)
6. Real RBAC, then the admin console
7. Agent creator with per-agent tool allowlists
8. Run observability over existing `RunEvent` data
9. Saved workflows
10. Prompt versioning and A/B, once 3 and 8 make outcomes measurable


---

## Implementing this with the methods in `RESEARCH.md`

`docs/RESEARCH.md` grades 51 techniques. This section maps the ones that survive
to product outcomes, and records what the cost decision above did and did not
change.

### What "cost is not a constraint" actually unlocks

Only four verdicts move. The rest of the holds in `RESEARCH.md` are held because
the technique **does not work**, not because it is expensive — which is worth
internalising before reaching for the next expensive idea.

| Technique | Was | Now | Why |
|---|---|---|---|
| `text-embedding-3-large` (#20) | trial, contested — 6.5× embed bill | **trial, run it** | Cost was half the objection. The other half stands: one preprint measures +6.2pp overall and −10pp on *preference* questions. Measure per category; be willing to revert. |
| LLM-written rolling summary (#19) | trial — doubles write-path calls | **trial, run it** | +10pp on one benchmark, −5.2pp on another. Now purely an empirical question. |
| Raise `openai_max_output_tokens` (#10) | adopt | **adopt, generously** | 1200 truncates real answers. No reason to be careful now. |
| Contextual Retrieval at full corpus scale (#3) | adopt | **adopt without sampling** | Was going to be applied selectively to control ingest spend. Apply to everything. |

**Unchanged, and worth restating:**
- **Reranking (#30) stays deferred** — latency, not cost. 2-5s per turn is a chat
  product problem at any price.
- **HyDE / query rewriting (#34) stays held** — measured *negative* for strong
  retrievers (−9.0% nDCG@10). Free money would not make it work.
- **Multi-agent decomposition (#45) stays held** — single agents match it at equal
  token budget. The premium buys nothing.
- **Reflexion / self-critique (#46) stays held** — the most-refuted idea in the
  research document.

### Method → product outcome

| Research method | Ships as | Segment | Measured by |
|---|---|---|---|
| Hybrid retrieval + BM25 + Contextual Retrieval (#1, #2, #3) | Documents and papers that answer questions asked in your own words | both | `evaluate_retrieval.py` — baseline paraphrase 70%, indirect 80% |
| Supersession keys (#4) | "It learns from you" is true rather than aspirational — corrections stick | both | `evaluate_memory.py` — stale-served rate, **measured at 100% today** |
| Citation-contract validator (#5) | Answers whose citations actually support them | both, sold to enterprise | deterministic check, zero tokens |
| `tool_choice: allowed_tools` + group gating (#6) | The agent creator: per-agent tool allowlists that are enforced, not advisory | enterprise | tool-selection accuracy per agent |
| Prompt fingerprint on `Run` (#12) | Prompt versioning, then A/B | enterprise | attribution of outcome to variant |
| Cross-run denial memory (#11) | The system stops proposing what you keep rejecting | both | denial rate per tool over time |
| GEPA on extractors (#23) | Better memory and graph extraction without hand-tuning prompts | both | extraction quality on a held-out set |
| Prompt cache key = workspace (#9) | Latency, which we still care about | both | p50 turn latency |

### Sequenced implementation

**1. Measurement first** — both rulers now exist and both report an honest number.
   `evaluate_retrieval.py` 82.1% overall; `evaluate_memory.py` **100% stale-served**.
   Neither existed a day ago and neither could have been trusted before.

**2. Memory supersession (#4).** The cheapest large win in the document: one extra
   field on an LLM call we already make, one migration, zero added latency, and it
   shrinks the active set. Target: stale-served 100% → ~0%.

**3. Hybrid retrieval (#1, #2, #3).** `Chunk.embedding`, a portable `chunk_terms`
   table for real BM25 with IDF, RRF fusion, then Contextual Retrieval at ingest.
   Target: paraphrase 70% → 85%+, indirect 80% → 90%+.

**4. Context hygiene (#6, #7, #8, #9, #10).** All zero- or near-zero-cost, and #6
   is the substrate for the agent creator. Raise the output budget here.

**5. Agent creator (enterprise).** Per-agent tool allowlists enforced through #6,
   plus model and effort selection. This is where research and product converge:
   the mechanism that makes tool-gating correct is also the feature enterprises ask
   for.

**6. Observability (#12) → prompt versioning → A/B.** In that order. The approval
   stream is the outcome metric; nothing before this makes A/B meaningful.

**7. Student surface.** Bibliography management, PDF/DOCX export, study aids. These
   need no research — they are ordinary product work on top of a spine that already
   compiles LaTeX to PDF offline.

### The through-line
The research and the product point at the same thing. "A platform that learns from
you" fails today for one specific, measured reason: it cannot represent a fact that
changed. Fixing that is item 2, costs almost nothing, and is the difference between
a memory feature and a memory *product*.

---

## Self-learning: per user, per distribution

The system already has an unusually clean training signal and does nothing with it.
Every proposed write is explicitly judged by a human — approved or denied — and
recorded on `agent_tool_calls` with the tool name, the arguments, and the rendered
preview. That is a labelled dataset most products would have to pay annotators for.

### The layers, and why they must stay separate

| Layer | Learns from | Writes to | Risk |
|---|---|---|---|
| **Per user / workspace** | approvals, denials, regenerations, edits after a write, explicit `remember` | `memory_items`, a style profile | low — already reversible via `forget` |
| **Per distribution (cohort)** | aggregate approval rate, tool success, abandonment, per prompt variant | prompt variant defaults, skill surfacing order | medium — affects people who never consented individually |
| **Product (offline)** | held-out eval sets | extraction prompts, tool descriptions | none at runtime; dev-time only |

Keeping these apart matters because they have different consent stories. A
preference learned from *your* denials may steer *your* agent. The same signal
aggregated across a cohort steers people who never made that choice, so it belongs
to defaults and ordering, not to anything irreversible.

### The rule that makes this safe

**Learning may change what is suggested, ranked, or pre-filled. It may never change
what is permitted.**

`RESEARCH.md` #50 (auto-promoting tool policies from approval statistics) is graded
**hold** for exactly this reason: the evidence is one synthetic run with no variance,
and the failure mode is a security gate that relaxes itself. A system that notices
you approved `edit_document` nine times and quietly stops asking has converted a
consent mechanism into a nuisance counter. The user promoting a policy by ticking
"always allow" is consent; the system inferring it is not.

So: denials become *negative preferences the model reads* (#11, adopt), never
*permissions the loop skips*.

### What to build, in dependency order

1. **Denial memory** (`RESEARCH.md` #11, adopt). A denied write becomes a memory
   item: "the user rejected renaming files in bulk". Costs one row per denial, needs
   no new signal, and directly reduces the most annoying failure — being asked the
   same rejected thing repeatedly. Measurable as denial rate per tool over time.

2. **Style profile per user.** Writing style analysis is both a skill the user asked
   for and a learned artefact: derive tone, length, structure and vocabulary
   preferences from documents the user *kept* versus edits they reverted. Store it
   as a typed memory kind so `forget` already works on it and the user can read it.

3. **Prompt fingerprint on `Run`** (#12, adopt). One column. Without it no outcome
   can be attributed to a variant, and every A/B claim afterwards is unfalsifiable.

4. **Outcome metrics.** Approval rate, regeneration rate, denial rate, abandonment.
   All derivable from data already recorded. This is the measurement layer that
   items 5 and 6 depend on and that nothing else can substitute for.

5. **Cohort defaults.** Once 3 and 4 exist, prompt and skill variants can be
   assigned per cohort and compared. Not before.

6. **GEPA on extractors** (#23, trial). Offline optimisation of the memory and graph
   extraction prompts against a held-out set. Dev-time, re-run when the model
   changes. This is where automatic prompt optimisation belongs — not in the
   request path.

### Explicitly not doing

- **Reflexion / self-critique retries** (#46) — the most-refuted idea in the
  research document.
- **Trajectory reuse** (#48) — measured at the wrong scale; our turns are 6
  iterations, not 20-50 steps, and it introduces silent planner bias.
- **Explicit thumbs** (#49) — measured 1-5% participation. The approval stream is
  a better signal that costs the user nothing extra.
- **Anything that learns a permission.** See the rule above.

---

## Skills and Google Suite — design, to build once auth clears

### The fork: is a skill a tool, or a mode?

Two readings, and the choice matters because one of them solves a problem we
already have.

**As a mode** — the user picks a skill for a conversation, and it swaps the system
prompt and available tools. Simple, but it makes skills a UI affordance the model
cannot reach for on its own.

**As progressive disclosure** — the model always sees skill *names and
descriptions*, and loading a skill reveals its full instructions and its tool
subset. This is the better answer, because of what it fixes:

We currently send **all 32 tools with full JSON schemas on every single turn**.
`RESEARCH.md` #6 (`tool_choice: allowed_tools` + deterministic group gating) is
graded **adopt** precisely to address that, and skills are the natural grouping.
So skills are not just a feature — they are the mechanism that keeps the tool
surface from growing unboundedly as we add connectors. A "PowerPoint creation"
skill carries the Slides tools; nothing else pays for them.

Decision: **progressive disclosure**, with the mode behaviour available as a user
override (pin a skill to a conversation).

### Skill shape

    Skill: workspace_id, name, description (when to use), instructions,
           tool_allowlist, model/effort override, visibility, enabled, created_by
    SkillVersion: content history, so a published skill can be rolled back

`Agent` already exists with name/instructions/enabled and is the closest thing —
decide at build time whether Skill subsumes it or sits beside it. Do not end up
with two competing abstractions for "a configured way of behaving".

Defaults to ship: **document review**, **presentation creation**, **writing style
analysis**. The last one doubles as the per-user learned artefact described in the
self-learning section above — it reads the style profile rather than inventing one.

### Google Suite

Extends the existing Google integration rather than replacing it:
`IntegrationAccount` + `OAuthState` + Fernet token storage already work for Gmail
(`services/connectors/gmail.py` is the pattern to follow).

| Surface | Use |
|---|---|
| Calendar | read and create events; the agent answering "when am I free" |
| Drive | list and read files as sources |
| Docs | read and **edit inline** — every edit goes through the approval + diff path |
| Sheets | read as datasets, feeding the existing DuckDB analytics |
| Slides | presentation creation |

Three things to get right:
1. **Incremental scopes.** Adding Calendar and Docs to an existing Gmail connection
   requires re-consent. Handle the upgrade explicitly rather than silently failing
   on a 403.
2. **Login consent and data consent stay separate.** Google is becoming a login
   provider in the auth work. A user signing in with Google has not thereby granted
   access to their Drive, and the two token sets must not be conflated.
3. **Edits are writes.** Editing a Google Doc is exactly as consequential as editing
   a local document, so it goes through `read_only=False` with a preview showing the
   diff. No exceptions for being "just a doc".

---

## A semantic layer

### Which kind
The phrase means two things. We have one and lack the other.

- **Knowledge semantics** — entities and typed relations over *documents*. Built:
  `graph_entities`, `graph_edges`, `graph_lookup/neighbors/path`.
- **Data semantics** — entities, dimensions, measures, joins and canonical metric
  definitions over *tables*. This is the dbt Semantic Layer / Cube / LookML sense,
  and it is missing.

### Where the gap actually is
There are two query paths and they fail in opposite directions.

`DatasetQuery` (schemas.py:452) is a typed contract — filters, group_by, metrics,
order_by, limit, no SQL. That is a proto-semantic layer, but it operates on **raw
column names from an uploaded CSV** and only over uploaded datasets. It has
structure without meaning.

`sql_query` (services/dbconnect/tools.py) lets the agent write arbitrary read-only
SQL against a customer's production schema. It has power without meaning, and this
is where accuracy dies. `describe_schema` tells the model column names and types.
It does not tell it that `status = 'C'` means completed, that revenue excludes
refunds, which of three date columns is the business date, how the tables join, or
that every query must carry `deleted_at IS NULL`. The model guesses, plausibly and
wrongly, and the answer arrives with the same confidence as a correct one.

A semantic layer is the standard fix for exactly this, and it is the single largest
lever on text-to-SQL reliability.

### The design that fits this product

Do not ask users to author a semantic model. Nobody wants to write LookML, and a
model nobody maintains is worse than none.

**Draft it with the agent, approve it like any other write.** Introspect the schema,
sample the data, propose entities, measures and joins, and render the proposal as a
reviewable artefact through the machinery that already exists: a preview, a diff, an
approval. The user corrects "revenue excludes tax" once, and that correction becomes
durable.

That is this product's core loop applied to data modelling, and it is the strongest
version of the "learns from you" claim — a semantic model that accumulates an
organisation's own definitions is genuinely sticky, and it is an enterprise moat in
a way that a chat wrapper is not.

Shape:

    SemanticModel   workspace_id, connection_id, version, status
    Entity          name, table, primary_key, description, synonyms
    Dimension       entity, column, type, description
    Measure         name, expression, aggregation, description, filters
    Relationship    from_entity, to_entity, join keys, cardinality

### Rules

1. **Compile, do not execute.** A semantic query compiles to SQL and the database
   runs it. Do not reimplement joins or aggregation — that is a query engine, and
   writing one is a different product.
2. **It sits above both backends.** "Revenue last month" must resolve identically
   whether the data is an uploaded CSV in DuckDB or a Postgres warehouse. This is
   what makes it a layer rather than a feature of one connector.
3. **`sql_query` stays.** The semantic layer is the preferred path, not a cage. An
   analyst with a question the model does not cover still needs raw SQL, and the
   read-only guards already make that safe.
4. **Schemas drift.** A model that silently references a dropped column is worse
   than no model. Detect drift on connection test and mark the model stale, the way
   `GraphProjection` already handles staleness.

### The honest cost
This is the largest single build discussed anywhere in this document — dbt and Cube
exist as companies because it is hard. It should not start until there is an
evaluation harness for it, for the same reason the retrieval work did not: a
text-to-SQL accuracy set with questions and expected result sets, measured before
and after. Without that, "the semantic layer improved accuracy" is unfalsifiable,
and we have already learned that lesson twice on this codebase.

Sequence: eval set first, then agent-drafted model over ONE connection type, then
the compiler, then unify with `DatasetQuery`. Do not start it while auth, retrieval
and memory are in flight.
