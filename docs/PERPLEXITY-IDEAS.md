# Perplexity research → 13 harness/capability ideas

Researched 2026-09-21 (web-verified agent report; evidence labels: [V] vendor,
[R] credible reporting of vendor detail, [I] independently verified, [S] speculation).
This file is the implementation brief for the `worktree-perplexity-harness` cycle.

## The grounding facts that matter

- Perplexity's pipeline: hybrid BM25+dense retrieval over chunk-level web index,
  multi-stage ranking (lexical, vector, authority, freshness, engagement), Sonar
  models fine-tuned to summarize-with-attribution over supplied snippets with
  stable citation ids; a complexity classifier routes cheap vs frontier models.
  Pro Search = plan-then-execute: visible step plan, per-step query batches,
  sequential steps with prior results fed forward; they keep planner prompts
  SHORT and inspect intermediate steps when iterating. [R]
- **The corrective [I]:** "cite every sentence" is a generation-time convention,
  not a verification-time guarantee. DeepTRACE (arXiv 2509.04499): 23–47%
  unsupported statements, citation accuracy 40–68% industry-wide, uniform
  overconfidence, one-sided on debate queries — deep-research modes improve
  citation thoroughness but stay one-sided. Tow Center: Perplexity best-in-class
  yet still ~37% incorrect answers. Nobody deterministically checks entailment
  of cited passages. Grain's deterministic citation validator is exactly that
  missing stage.
- Agent API [V/R]: managed runtime with `web_search` tool carrying domain
  allow/deny (≤20), recency + last-updated date filters, per-call token budgets
  (low/medium/high or explicit), max_results, stable result ids; six named
  presets (fast/low/medium/high/xhigh/wide-research) bundling model + prompt +
  tools + effort + budget; sandbox sessions; MCP.
- Model Council [V/R]: N models answer one query; synthesis organized as
  convergence / disagreement / unique findings, raw responses inspectable.
- Comet browser's independently proven failure [I]: indirect prompt injection —
  page content (even hidden text in screenshots) reached the LLM with action
  authority, no trust boundary (Brave research). The lesson: untrusted retrieved
  content and action authority must never meet without provenance-aware mediation.
- Spaces = retrieval scope + standing instructions + model policy as one object.
  Pages = thread → publishable document. Labs = budgeted deliverable runs with an
  asset list. Finance = schedule + watched source + extraction schema + standing
  dashboard. search_evals = open eval harness; eval-first culture. [V/R]

## The 13 ideas (implementation order: 1 → 6 → 2 → 8, then 9, 5+7, 3, 12, 4, 10, 11, 13)

1. **Verified-grounding score on every answer.** Post-generation, run the
   deterministic citation validator over EVERY sentence: verified /
   cited-but-unsupported / uncited. Stamp answers with a groundedness score and
   per-sentence badges in the thread UI; cited-but-unsupported sentences trigger
   one targeted regeneration pass constrained to the offending sentences.
2. **Plan-then-execute retrieval mode ("Pro Search over the workspace").** The
   model first emits a visible step plan; each step issues batched hybrid-RAG
   queries; step results summarized into the next step's context; plan renders
   live in the thread (existing run streaming). Use the delegation subsystem for
   independent sub-questions. Keep the planner prompt short; log intermediate
   steps for regression triage.
3. **Council delegate with deterministic adjudication.** N models answer from the
   SAME frozen retrieval set; synthesis structured agree/disagree/unique; each
   candidate gets a verified-grounding score (idea 1) and unsupported-heavy
   candidates are demoted BEFORE the judge model sees them.
4. **Corpus-grounded follow-up chips.** 3 follow-ups per answer, each filtered by
   a cheap retrieval probe (surface only if top-k clears the per-width dense
   floor); candidates seeded from knowledge-graph neighbors of cited entities.
5. **Provenance-frozen Pages.** "Publish as Page" on a thread: a rendered
   document whose citations freeze to the exact chunk version + embedding
   generation validated at publish time, hover-to-see-passage; a scheduled
   re-validation sweep flags published pages contradicted by newer document
   versions.
6. **Retrieval tool parameter parity.** Extend the RAG tool schema: source
   allow/deny lists (documents/folders/spaces), ingested_after/document_date
   filters, explicit per-call token budget (cheap reconnaissance vs deep reads),
   stable chunk ids flowing into the validator.
7. **Coverage ledger + counter-evidence pass for deep research.** Reports carry:
   sub-questions posed, sources consulted vs available in scope, sub-questions
   with no support. Debate-shaped questions (classifier) force a dedicated
   counter-evidence retrieval step; the report cites both sides or states the
   corpus is one-sided.
8. **Answerability eval harness as a merge gate.** Fixture corpora with gold
   chunk ids: gate on retrieval recall@k, verified-grounding score, citation-
   validator pass rate; per-step traces of plan-mode runs logged. Scripted/local
   providers only in CI (standing rule).
9. **Taint-labeled context, provenance-gated actions.** Label every context block
   by provenance class (user message, workspace chunk, web fetch, MCP result,
   memory item); guardian blocks/escalates risky actions (writes, egress,
   networked sandbox, MCP calls) whose triggering instruction has untrusted-class
   provenance. Extends the existing guardian-provenance/forged-answer ledger from
   memory to RAG chunks and MCP outputs.
10. **Deliverable runs with an output manifest.** Workflow preset: plan →
    retrieve → sandbox (charts/spreadsheets/tables from workspace data) →
    manifest (files + the chunks/queries each derives from + sandbox session
    ids) attached to the space; declared time/token budget; partial results ship
    at exhaustion with the idea-7 ledger.
11. **Watch-and-brief primitive.** A "watch" on any source/space: scheduled
    re-ingest + diff of new versions, extraction schema over the delta, results
    written as KG nodes + supersession-aware memory updates, a standing brief
    page per watch, material-change pings to the space.
12. **Run presets as policy bundles.** Named intents (quick lookup / grounded
    answer / deep research / deliverable) pinning model tier, retrieval budget,
    tool allowlist, approval mode, guardian strictness; an "auto" preset routes
    by a complexity classifier. Space default-agent seeding stays a composer
    seed (never a run-path layer).
13. **Grounded-answer API with verification receipts.** Workspace-grounded
    answering over the existing MCP/REST surface: request carries source
    filters + JSON-schema output; response carries chunk-id citations plus the
    validator's per-claim verdicts and embedding-generation versions. Metered
    per retrieval invocation. Web surface: receipt viewer on the API tokens page
    (full-stack-feature rule).
