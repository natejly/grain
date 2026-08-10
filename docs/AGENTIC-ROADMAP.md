# Agentic roadmap: competitive build list

Written 2026-08-10. Sources: four independent competitor surveys (ChatGPT, Claude,
Gemini, Copilot, plus Glean/Notion for the enterprise surface), reconciled against
this codebase. Every "our state" cell below was checked in the code, not taken from
the researchers. Where they were wrong, the correction is in §2 and the table
reflects the corrected value.

A note on register: this document commits to views. Where the researchers disagreed,
the disagreement is surfaced, not averaged.

---

## 1. The gap table

Sorted so the rows that matter come first: table stakes we do not have, then table
stakes we half-have, then differentiators, then the refusals. Effort is
engineering-days scale — S ≈ 1-3 days, M ≈ 1-3 weeks, L ≈ a month or more.

### Table stakes, missing

| Capability | Who ships it | Our state | Effort | Note |
|---|---|---|---|---|
| Hosted web search with citations | ChatGPT, Gemini, Copilot, Claude — all GA | missing | S | One entry in the `tools` array of the Responses API call we already make, on `gpt-5.5` which we already pin. Cheapest item on the list. |
| Attachments in the composer | Universal | missing | M | Chat has no file input at all. Files must be routed through the Sources view first. |
| Image input (vision) | Universal | missing | M | `gpt-5.5` is vision-capable; the gap is blob storage + composer + transcript rendering, not inference. |
| Downloadable .docx / .xlsx / .pptx | Claude (free tier), ChatGPT, Copilot | missing | M | Needs no sandbox. Pure serialisation. See §5 — we can do this client-side. |
| Scheduled / recurring runs | ChatGPT Tasks, Gemini Scheduled Actions, Copilot, Claude Cowork — all GA | missing | S–M | We are ~90% there and the docs do not say so. See §7. |
| Conversation search | ChatGPT, Claude — GA | missing | S | Zero search over `messages`. A product claiming to learn from you that cannot find your own conversation reads as false. |
| Remote MCP client with OAuth 2.1 | Anthropic, OpenAI — GA | missing | M | **The single highest-leverage item in this document.** See §4. |
| Curated connector directory | Anthropic (400+), OpenAI, Microsoft | missing | S | A seeded table plus a connect button, once OAuth exists. No per-service code. |
| Google Calendar read/write | Claude, ChatGPT — GA | missing | S | `IntegrationAccount` / `OAuthState` / Fernet plumbing already works; `connectors/gmail.py` is the template. |
| Token / cost accounting + spend caps | Anthropic (Jul 2 2026), OpenAI (Jun 18 2026), Notion | missing | S | Verified: zero occurrences of `prompt_tokens`/`completion_tokens`/`total_tokens` anywhere in `apps/api`. |
| Organization entity above Workspace | Every enterprise product | missing | M | Nothing can express "the org forbids this even though the workspace owner wants it." Blocks 5 other rows. |
| SOC 2 Type II | Universal | missing | L (calendar) | 9-14 months because of the observation window. Not engineering work. Start now. |
| SSO (SAML/OIDC) + SCIM | ChatGPT Enterprise, Claude Enterprise | missing | L | Buy, do not build. "Sign in with Google" is explicitly not enterprise SSO in a security review. |
| Data retention windows + deletion guarantees | All | missing | M | Mostly contractual. The engineering half is cascade-deleting derived artefacts. |
| Admin console | Anthropic, OpenAI, Glean | missing | L | Blocked on the Organization entity. Build it last or you ship empty screens. |

### Table stakes, partial

| Capability | Who ships it | Our state | Effort | What's actually missing |
|---|---|---|---|---|
| URL fetch ("read this page") | Via browsing in all four | **partial — ~90% built** | S | `services/tools.py::execute_read_only_get` already does HTTPS-only, host allowlist, DNS-resolved private-IP blocking, redirect cap of 4, body-size cap. Missing: a policy for the allowlist, a readability pass, and registration in `build_registry`. |
| PDF / document understanding in-conversation | OpenAI `input_file` GA | partial | S | We ingest PDFs, but `ingestion.py` is `pypdf` text extraction only — every figure, chart and image-rendered equation is lost. Also: no .docx, .xlsx or image formats accepted at all (`SUPPORTED_EXTENSIONS` is txt/md/markdown/csv/json/pdf). |
| Data analysis on an uploaded file | OpenAI `code_interpreter` GA, Copilot Analyst GA, Claude all tiers | partial | M | DuckDB covers typed aggregation over *ingested* tabular data. No pandas, no matplotlib, no "clean this export". The sandbox is JS/TS only. See §5. |
| Background execution + visible progress | OpenAI `background:true` GA; every competitor shows a step feed | partial | S | The execution half is done and better than the hosted version. `RunEvent` records every step and **nothing renders it**. That is a UI build over an existing data model. |
| MCP spec 2026-07-28 conformance | Spec final 2026-07-28 | partial | S | Mostly an SDK bump. `client.py:87,98` call `session.initialize()`, which the new spec removes. Statelessness is a gift — our client already opens and closes a connection per operation and apologises for it in a docstring. |
| Skills as an open standard (SKILL.md) | Anthropic (Dec 18 2025), OpenAI, Microsoft, ~32 tools | partial (designed, not built) | M | PRODUCT.md designed exactly the right thing. Adopt the published format rather than the bespoke `Skill`/`SkillVersion` schema. |
| Select-text-and-instruct | ChatGPT (inline blocks), Claude, Gemini Canvas | partial | S | We have the hard half — approval-gated diffs with version history. Missing only the selection-scoped entry point: pass a character range into `edit_document`. |
| Real RBAC | ChatGPT Enterprise, Claude Enterprise | partial | M | Verified: `Membership.role` is `String(24)` defaulting to `"member"`; `require_owner` is defined in `auth.py:252` and used in exactly two API modules (`generated_apps.py`, `integrations.py`). Most write paths have no role gate — only a membership check. |
| Audit export / SIEM streaming | Anthropic Compliance API, OpenAI Compliance Logs | partial | S | `AuditEvent` has the right columns and the right index (`workspace_id, created_at`). `api/audit.py` returns the last 100 rows, unpaginated, with no cursor, no schema doc, no immutability guarantee, no retention control. Best effort-to-procurement-value ratio on the list. |
| Admin-approved connector allowlist | OpenAI Connector Registry (still beta), Claude Code | partial | S | Blocked on the Organization entity. See also the correction in §2.5 — our MCP path is *less* defended than the researchers believed. |
| Sharing and publishing | Claude published artifacts, ChatGPT Sites | partial | S | We publish immutable app snapshots, which is the harder and better primitive. Missing: account-free viewing, an embed path, org-vs-public as an explicit choice. |

### Differentiating

| Capability | Who ships it | Our state | Effort | Note |
|---|---|---|---|---|
| Deep research mode | OpenAI, Gemini, Copilot Researcher — all GA | missing | M | The best strategic fit here. Mostly composition once web search lands. See §6. |
| Agent-scoped access policy | Glean (**beta**), Microsoft Agent 365 GA, Okta GA | partial | M | `ToolPolicy` is the right primitive pointed the wrong way: per-workspace, keyed on tool name only, no predicate over caller, arguments or response. Glean is still in beta — the window is open, measured in quarters. |
| Permission-aware / ACL-trimmed retrieval | Glean, M365 Copilot, ChatGPT/Claude Enterprise | missing | M (scoped) / L (full) | This is a data-leak shape, not a missing feature. See §4.3. |
| Google Workspace write with a diff | ChatGPT GA 2026-06-15; **Claude cannot edit an existing Doc**; Gemini native | missing | M | The clearest competitive opening in the whole survey. |
| Conversation branching | ChatGPT only (Sep 2025) | missing | S | Not yet assumed, unusually well matched to an approval-gated product. Do it in the same week as conversation search. |
| Run observability for admins | Anthropic Analytics API, OpenAI credit analytics, Notion, Dust | partial | M | Same build as the consumer "what is it doing" feed. One build, two roadmap lines. Gated on token accounting. |
| Repurposing a report (quiz / flashcards) | Gemini Deep Research GA | missing | S | Was "high value, low cost" in PRODUCT.md. Gemini shipped it, so it is now table stakes for the student wedge, not a differentiator. The differentiated version is aids generated from the user's *own* sources with passage provenance. |
| Customer-run agent evaluation | Copilot Studio GA 2026-03-31, Glean | partial | M | `evaluate_retrieval.py` and `evaluate_memory.py` exist and produce honest numbers. Turning an internal harness outward is a reframe, not a rebuild. |
| EU AI Act Article 14 positioning | Regulatory — enforceable since 2026-08-02 | **have** | S (docs) | Best cost-to-value ratio in this document. A mapping table from Article 14 clauses to our mechanisms. Days, not weeks. |
| Approval gate with rendered diff | Glean (May 6 2026), Notion Plan Mode, Copilot, Claude Code | **have** | — | See §3. Level, not ahead. |
| Canvas / artifact editing | ChatGPT, Claude, Gemini, Copilot — all GA | **have, arguably ahead** | — | Build nothing. The gap is that we never tell this as the canvas story. |

### Noise — do not build

| Capability | Who ships it | Effort if we did | Why not |
|---|---|---|---|
| Computer use / browser automation | OpenAI `computer` GA; Google **killed Project Mariner 2026-05-04** | L | Requires operating a browser farm. New always-on service. Market is retreating. |
| Voice / realtime | ChatGPT, Gemini Live, Copilot — GA | L | Needs a persistent bidirectional relay. Nobody approves a unified diff by voice. |
| Server-side context compaction | OpenAI, shipped 2026-02-10 | S | Contradicts the §7.3 invariant. Our turn is ~8k tokens against a 1M window. |
| Tool search (deferred tool surfaces) | OpenAI 2026-03-05, Anthropic | S | Solves a 584-tool problem. We have ~34. Buys a recall cliff. |
| OpenAI's hosted agent loop | OpenAI, Mar 2026 | — | The loop *is* the product. Use hosted tools, never the hosted loop. |
| Bespoke Slack/Notion/Linear/Jira/GitHub/… connectors | Anthropic directory, Glean | L × 11 | All reachable through one OAuth-capable MCP client. |
| Microsoft 365 (Graph) | Copilot native; Claude read-only | L | Anthropic has far more resources and shipped read-only. Best case is a worse Copilot. |
| Publishing as a ChatGPT app | OpenAI Apps SDK, submissions open | M | Trades the customer relationship for a directory listing, and none of our differentiators survive being a tool call in someone else's loop. |
| Agentic spreadsheet editor | Copilot Excel GA, Claude for Excel GA | L | SpreadsheetBench 2: best *product* scores 15.4%. |
| Real-time co-editing (CRDT/OT) | Google only | L | Absent from every chat-native AI product; none punished for it. Build inline *comments* instead if pressure appears. |
| Prompt A/B testing for customers | Braintrust, LangSmith, Langfuse — dev tools only | L | No workspace assistant ships it. Keep one column (`Run.prompt_fingerprint`) and stop. |
| Visual workflow / DAG builder | OpenAI Agent Builder — **winding down, removed 2026-11-30** | L | Launched Oct 2025, dead in 14 months at the company with the most distribution. |
| Single-tenant VPC / on-prem | Glean; ChatGPT Enterprise ships SaaS-only | L | Contradicts the ephemeral-disk constraint. |
| EU data residency | OpenAI, Google, GitHub; **Anthropic does not have it** | L | A market-selection decision, not a build. |
| HIPAA BAA | Anthropic, Glean | L | Cannot sign one while the subprocessor relationship with OpenAI is an API key in an env var. |
| ISO 42001 | Anthropic, Glean | L | A 2027 problem. Nobody rejects you for lacking it while accepting a vendor without SOC 2. |

---

## 2. Corrections to the research

The researchers were mostly accurate. Six claims were not, and three of them change
a recommendation.

**2.1 — There is no DuckDB-wasm in the browser.** The core-capabilities researcher
argued for Pyodide partly on the precedent that "this codebase already ships a 79 MB
TeX Live tier *and DuckDB-wasm* to the browser." Half true. `apps/web/public/latex`
is 82 MB and does ship. DuckDB is the **Python** package, imported in
`apps/api/app/services/analytics.py:14` and run server-side in an in-memory
connection. `apps/web/package.json` has no DuckDB dependency. The Pyodide
recommendation still stands — TeX Live alone is precedent enough, and the
scientific-stack Pyodide bundle is smaller — but the argument rests on one leg, not
two. Do not repeat the two-leg version in a design doc.

**2.2 — The citation validator is built and wired, not a proposal.** Three of the
four researchers described it as future work ("RESEARCH.md #5, ~40 lines, zero
tokens"). It exists: `apps/api/app/services/citations.py` is a deterministic,
zero-model-call validator with a careful spec (code spans masked, ranges expanded,
non-ASCII digits folded length-preservingly, markdown links counted). It is called
from `services/runs.py:191` inside `_record_citation_report`, which fires on **every
completed run** and writes both a `run.citations` RunEvent and a
`run.citations_validated` AuditEvent. This materially strengthens the case for
shipping web search first: the machine-checked-citations claim is not a build, it is
a pointer change.

**2.3 — URL fetch is not missing; it is ~90% built and gated.**
`services/tools.py::execute_read_only_get` already implements the part everyone gets
wrong: HTTPS-only, hostname allowlist, `getaddrinfo` resolution followed by an
explicit private/loopback/link-local/multicast/reserved IP rejection, a four-redirect
cap with re-validation at every hop, and a byte cap. It is wired into
`services/runs.py`. What is missing is a *policy* — `tool_host_allowlist` defaults to
`"api.github.com"` — plus a readability pass and a `build_registry` entry. Rescope
this from "small build" to "half a day plus a policy decision."

**2.4 — The MCP grep returns zero hits, not two.** The integrations researcher wrote
that grepping `oauth|Bearer|token` in `mcp/client.py` "returns two hits, both the
word `headers`," which is self-contradictory. The correct statement is stronger:
grepping the entire `services/mcp/` directory for those terms returns **nothing**.
`ServerConfig` (`client.py:44-54`) carries `name`, `transport`, `command`, `args`,
`env`, `url`, `headers` and no field that could hold a grant. The finding holds
completely.

**2.5 — We have *less* MCP transport defence than credited, not more.** The
enterprise researcher wrote that "Jasmine already has MCP connectors defaulting to
ask-before-running plus HTTPS-only exact-host allowlisting with SSRF defences,"
described as "arguably stronger than OpenAI's." Half of that is right and the good
half is somewhere else. Ask-before-running is real (`mcp/registry.py:156` sets
`read_only=False` on every discovered tool, so everything prompts). HTTPS-only is
real (`api/mcp.py:86-92`, with a `http://localhost` dev escape). But there is **no
host allowlist and no SSRF guard on the MCP path at all** — the allowlist and
private-IP checks live in `services/tools.py` and are not applied to MCP. A workspace
owner can point an MCP server at any HTTPS host, including one whose DNS resolves
into a private range. This is a real, small, unglamorous fix: apply
`validate_public_https_url`'s IP checks to the MCP URL on save and on connect.

**2.6 — The tool count has drifted.** RESEARCH.md §6.1 measured "exactly 32 tools,
14,201 JSON chars ≈ 3,550 tokens." Counting the registry today gives **34**
unconditional tools (5 core + 3 memory + 2 graph + 15 artifacts + 9 projects), with
dbconnect (4), connectors (up to 5) and MCP tools all registered conditionally on
top. Immaterial to the conclusion — 34 is still nowhere near the 584-4,000 range
where tool search pays — but a workspace with Gmail, Strava, a database and one
verbose MCP server is already well past 50, which is the number that matters for
§4.4.

---

## 3. Is the governance wedge actually a wedge?

Externally checked, and the honest answer is: **level on the visible part, ahead on
the invisible part, and aimed at the wrong buyer.**

The visible part converged in the market between May and July 2026. Glean shipped
write-action previews for single and batch flows in its 2026-05-06 release. Notion
shipped Plan Mode, where the agent drafts a plan and the user clicks "Approve plan"
before anything changes. Copilot ships multi-file summary diffs with granular
accept/undo in VS 2026 18.6. Claude Code is permission-gated and read-only by
default. OpenAI's Agents SDK pauses tool calls for approve/reject and resumes from
saved state. "We show you the change before it lands" is now a slide everyone has.
Stop building the pitch around it, and stop polishing the diff renderer — that
energy buys nothing now.

Four things survive as genuinely differentiated, and they are all structural rather
than visual:

1. **It is mandatory, not a mode.** Every competitor's gate is opt-in, or default-on
   but skippable. Ours falls out of `ToolSpec.read_only` and `ToolPolicy` in the loop
   itself — there is no code path that writes without parking first.
2. **It is bound to policy, not to UI.** `ask`/`allow`/`deny` per tool is a data
   decision the loop consults, not a modal the frontend renders.
3. **The record is actor-attributed and immutable in intent.** `AgentToolCall`
   carries the decision and the decider; `AuditEvent` records the action. (Note: the
   database does not currently *enforce* immutability — nothing prevents an UPDATE.
   That is one of the audit-export deltas in §7.)
4. **RESEARCH.md #50 is held on purpose.** We explicitly refuse to let approval
   statistics auto-relax a policy gate. Nobody else has articulated that position,
   and it is the thing to sell. A gate that learns to stop asking is not a gate.

And then the tailwind, which is the single best cost-to-value item in this entire
document: **EU AI Act Article 14 (human oversight) and Article 26 (deployer duties)
became enforceable on 2026-08-02** — eight days ago. They require that systems be
built so a natural person can monitor, intervene and override, with the authority and
not merely the technical ability to do so. Our approval gate is a native
architectural answer to a regulation that just landed, where every competitor bolted
theirs on last quarter. The build is a documentation page and a clause-by-clause
mapping table (Article 14 → approval gate, per-tool deny, audit event, `forget`).
Days of work. Caveat honestly: most of our use will not be classified high-risk, so
this is a credibility asset, not a compliance requirement our buyer must satisfy.

**Where the market actually moved, and we have not.** The frontier is one level up:
the agent as a governed principal that can do *strictly less* than the human who
invoked it. Glean's agent access policies (beta, docs updated 2026-06-09) evaluate
the live tool call — active tool, invoking user, agent scope, input arguments, *and
response payload* — in block / filter / flag-for-review modes, most-restrictive-wins,
able only to narrow user permissions and never to widen them. Microsoft Agent 365
went GA 2026-05-01 giving every agent an Entra Agent ID. Okta ships short-lived
scoped agent tokens. Our `ToolPolicy` gates on `tool_name` alone, per workspace, set
by the workspace, with no tier above it. The interception point already exists — we
would be adding predicates to a gate we own, not building a gate. Glean being still
in beta is the whole opportunity, and it will not last.

**The uncomfortable conclusion.** Everything we have built — gates, diffs, per-tool
policy, audit events, tenant isolation — persuades a security *engineer*. Nothing
built so far serves the *admin*, and the admin signs. The gap is not depth of
control; it is that nobody but the end user can currently exercise any of it.

---

## 4. Integrations: what MCP already reaches, and what it does not

This is the most consequential section in the document, because it changes the plan
from "build ten connectors" to "build one client and a directory."

### 4.1 The one fact

`apps/api/app/services/mcp/client.py:89-99` is the entire remote-server path:

```python
if config.transport == "http":
    if not config.url:
        raise McpError("An HTTP server needs a URL")
    async with streamablehttp_client(
        config.url, headers=dict(config.headers) or None
    ) as (read, write, _get_session_id):
```

A static header dict. No 401 handling, no `WWW-Authenticate` parsing, no RFC 9728
protected-resource-metadata discovery, no RFC 8414 authorization-server metadata, no
PKCE, no client registration, no RFC 8707 `resource` parameter, no refresh.
`ServerConfig` has no field that could hold a grant.

### 4.2 What that costs, in numbers

Against the official registry snapshot of 2026-07-28: 9,312 of 18,849 servers (49.9%)
advertise a remote endpoint. Of those remote servers, only 55.8% are unauthenticated,
and 27.0% will not even return a **tool list** without auth — we cannot enumerate
them, let alone call them.

| Integration surface | Reachable today | Reachable with OAuth MCP | Needs bespoke work |
|---|---|---|---|
| Slack, Notion, Linear, Jira, GitHub, Asana, Confluence, Sentry, Stripe | no | **yes** — all ship official remote MCP servers behind OAuth | no |
| Snowflake / warehouse | no | **yes** — Snowflake's own managed MCP server is the sanctioned path, and is how ChatGPT reaches it | no (a bespoke Snowflake connector would be wasted work) |
| Unauthenticated / static-API-key servers | yes | yes | no |
| Google Calendar, Drive/Docs/Sheets/Slides | no | not usefully — Google is not the MCP story | **yes**, and it is cheap: the `IntegrationAccount`/`OAuthState`/Fernet plumbing exists and `connectors/gmail.py` is the in-repo template |
| Microsoft 365 | no | read-mostly, if a deal demands it | no — do not build |

So: **one medium build substitutes for eleven large ones**, and the two things worth
hand-building (Calendar, Drive-with-diff) are the two MCP does not usefully cover.

The stdio half of our client deserves a plain statement too: it spawns `npx`/`uvx`
subprocesses. In a multi-tenant container with ephemeral disk and an ADR forbidding
server-side code execution, spawning a tenant-supplied subprocess is precisely what
ADR 0004 exists to prevent. **stdio is a localhost-dev feature that cannot ship.**
"MCP connectors (stdio + streamable HTTP) — built" in PRODUCT.md overstates what
actually reaches a customer; the honest version is "one transport, no-auth servers
only."

### 4.3 Three things to get right while building it

**Tokens are per-user-per-server, not per-workspace.** `McpServer.secrets_encrypted`
is a workspace-scoped Fernet blob with a `created_by` field — which means whoever
configures a server silently lends their identity to every member of the workspace.
Reusing `IntegrationAccount` unchanged reproduces the same bug, since it is
workspace+provider keyed. This is the leak shape a security review finds.

**Build against the 2026-07-28 rules, not the 2025-06-18 ones.** RFC 9207 issuer
validation is now mandatory and DCR is deprecated in favour of Client ID Metadata
Documents. Implementing the deprecated flow means migrating twice.

**Source ownership before connector ingestion.** `Source` and `Chunk` are
workspace-scoped. The moment one member connects a Drive folder, every member of that
workspace can retrieve it with passage-level citations regardless of Drive's
permissions — and our citation contract makes the leak legible and quotable, which is
worse than a silent one. The 130-odd isolation tests do not cover this, because it is
intra-tenant. Full query-time ACL evaluation is genuinely large and probably wrong to
attempt at our size. The proportionate build is a column and a filter: record the
connecting `IntegrationAccount` and an owning user on each `Source`, default
connector-derived sources to private-to-owner, make sharing an explicit act. Do this
**before** any Drive or SharePoint ingestion ships, not after.

### 4.4 Gate the tool surface first

`build_registry` is tight — ~34 tools at roughly 111 tokens each, 2-7× tighter than
typical MCP schemas. A single verbose third-party server can meaningfully move that
number, and an open-ended directory can double it. The skills / progressive-disclosure
design in PRODUCT.md stops being an optimisation and becomes a prerequisite the moment
connectors are open-ended. Ship `tool_choice: allowed_tools` plus deterministic group
gating before the directory, not after.

### 4.5 The Gmail scope is a live shipping blocker

`services/connectors/base.py:19` pins `GMAIL_SCOPE =
"https://www.googleapis.com/auth/gmail.readonly"`. **Every Gmail read scope is
restricted**, which means Google verification *plus* a CASA Tier 2 annual third-party
security assessment before serving more than 100 users. That is committed to main
today.

Meanwhile the surface we actually want is on the cheap tier. `drive.file` is
classified **non-sensitive/recommended** — basic verification only — and grants
create-and-edit on exactly the files the user picks through the Google Picker or that
we created. Docs/Sheets/Slides-specific scopes are merely *sensitive*: verification,
no CASA. So the expensive scope is built and the cheap one is not. Before adding
anything to Gmail, decide whether the existing `gmail.readonly` grant earns its
compliance cost at all.

Two further Google notes: never request `drive` or `drive.readonly` (restricted, drags
in CASA Tier 2), and handle granular consent explicitly — Google rolled per-scope
opt-out to web apps in Nov 2025 and to Chat apps on 2026-01-20, so a partial grant
must degrade rather than 403 at call time.

---

## 5. The browser-only sandbox: what it costs, plainly

ADR 0004's decision is that "generated code executes only in a boundary that carries
no workspace authority" — an opaque-origin iframe, `sandbox="allow-scripts"` without
`allow-same-origin`, CSP `connect-src 'none'`, data crossing one validated postMessage
protocol. The rationale is a threat model: do not run generated code on our servers
with our authority.

**What it costs today, stated without hedging.** DuckDB plus datasets plus dashboards
cover typed aggregation over *ingested* tabular data through `DatasetQuery`. They do
not cover arbitrary pandas or scipy work, statistical modelling, matplotlib, parsing
awkward formats, or "clean this messy export." And because the sandbox is esbuild-wasm
— JS/TS only — a student asking to run numpy homework gets **nothing at all**. That
is a real hole in the student wedge, and "analyse this spreadsheet and plot it" is the
single most common knowledge-work ask. Copilot's Analyst agent, ChatGPT Agent mode and
Claude all name that exact workflow in their marketing.

**The framing correction that matters.** The ADR's rationale is "do not execute
generated code on **our** servers." OpenAI's `code_interpreter` is a GA hosted
container running on OpenAI's infrastructure. It does not put execution on our
servers, so the ADR's actual threat model is untouched even though a literal reading
of "browser-only" is violated. That is a decision to re-make deliberately, not one
already made. It should be re-made in writing either way.

**Four options, ranked.**

| Option | Fits ADR 0004 | New service | Covers numpy/pandas/matplotlib | Verdict |
|---|---|---|---|---|
| Pyodide in the existing iframe | letter and spirit | no | yes | **Recommended.** Python + numpy + pandas + matplotlib in wasm, in the sandbox we already run, no vendor, keeps the offline story, extends a surface we own. The payload objection does not survive contact with an 82 MB TeX Live tier already shipping. |
| OpenAI `code_interpreter` | spirit yes, letter no | no | yes | Hold as the escape hatch for workloads too heavy for wasm. ~$0.03/container, billed by the minute, 5-minute minimum, 20-minute idle expiry, 100 RPM/org. Returns generated files via `container_file_citation`. |
| `input_file` spreadsheet auto-parsing | yes | no | partly | **Prototype this first.** The Responses API parses the first 1,000 rows per sheet and generates header metadata and a summary. It may satisfy a meaningful share of "analyse this file" for a day of work, with no sandbox at all. |
| Server-side Python sandbox of our own | no | yes | yes | Do not. This reverses an explicit architectural decision to match how competitors happen to be built. |

**And the part that needs no sandbox at all.** Generating .docx/.xlsx/.pptx is
deterministic serialisation from structured content — there is no untrusted code
anywhere in it. Competitors do it with python-pptx/openpyxl in a server sandbox
because they already have one. `pptxgenjs`, `docx` and `exceljs` are pure JavaScript,
bundle into what we already ship, and produce the same files client-side — *faster*,
because there is no server round-trip, and latency is the constraint that survives
("API spend is not a design constraint" does not extend to the user waiting). This is
the LaTeX→PDF-in-browser trick applied to OOXML, and we have already proved the
pattern once.

---

## 6. Where the researchers disagreed

Surfaced rather than averaged.

**Which gap is biggest.** The core researcher says the composer cluster — no file, no
image, no link, no Python — because "here, look at this" is the most common opening
move in knowledge work and it fails four ways. The work-product researcher says web
search, because every flagship competitor deliverable is web-grounded and a report
generator restricted to uploaded PDFs is a different, smaller product. The
integrations researcher says remote MCP OAuth, because it is the difference between
having a client and reaching an ecosystem. The enterprise researcher says the missing
`Organization` entity, because it blocks five other rows.

They are not in conflict; they are answering different questions. Biggest *user-felt*
gap is the composer. Cheapest gap with the most disproportionate damage is web
search. Biggest *leverage* is MCP OAuth. Biggest *blocker* is the org entity. The
sequence in §7 uses all four rankings rather than picking one.

**Whether scheduled runs need the policy layer first.** The core researcher says
scheduled runs are ~95% built and the approval semantics fall straight out of existing
machinery: read-only by default, park at the first write, land in an approval inbox.
The enterprise researcher says shipping scheduled runs *removes the human from the
gate*, which is the entire wedge, and therefore the agent-policy layer is a
prerequisite rather than a nice-to-have — Glean's alignment check exists precisely
because background agents run without per-run approval.

**Resolution: the core researcher's design is the enterprise researcher's
prerequisite, at a fraction of the cost.** "Read-only by default, park at the first
write" *is* an agent-scoped policy — the most restrictive one possible. It uses the
existing park/resume path verbatim, it preserves "no agent write without a human at a
diff" through automation instead of abandoning it there, and it matches what ChatGPT
Tasks and Gemini Scheduled Actions actually do in practice (they report; they do not
act). Ship scheduled runs with that restriction and the general policy layer becomes a
later relaxation rather than a blocker. What must never ship is the alternative: an
approval prompt that auto-approves when nobody is watching. That converts the
differentiator into a liability and is exactly what RESEARCH.md #50 is held to
prevent.

**Whether the governance wedge is real.** The enterprise researcher is bluntest —
"level, not ahead, and it became level roughly three months ago." Three others treat
the approval gate as a live differentiator. The enterprise researcher is right about
the diff and wrong to stop there; §3 splits it.

**Study aids.** PRODUCT.md calls flashcards and quiz generation "high perceived
value, low build cost," which was true when written. Gemini Deep Research now
reshapes any report into flashcards, a quiz, an infographic or an audio overview. It
is table stakes for the student wedge now, not a differentiator. Build it small.

**Deep research architecture.** Every competitor's visible shape is a multi-agent
swarm. RESEARCH.md #45 holds multi-agent decomposition on measured evidence — roughly
a 15× token premium, with single agents matching at equal budget. Nothing in the
competitive picture refutes that. Build deep research as **one** agent with
`MAX_ITERATIONS` raised for that path only (it is 6 today, at `agent_loop.py:25`).
Copy the output, not the architecture.

---

## 7. Sequenced plan

Weighted by (table stakes × already-partial). The cheapest route to parity is
finishing things that are nearly done, and this codebase has an unusual number of
those.

**Running from day one, in parallel with everything, because it is calendar time and
not engineering time: start the SOC 2 Type II observation window.** Nine to fourteen
months. It gates the most deals and shipping faster cannot fix it. Our existing
controls — workspace scoping, encrypted secrets, audit events, the isolation test
suite — are genuine evidence, so readiness is shorter than typical. Type I first as a
stopgap gets us into some pipelines.

### Wave 1 — the week that buys the most (days, not weeks)

| # | Item | Why here |
|---|---|---|
| 1 | **Hosted `web_search`** | One entry in the tools array of an API we already call, on the model we already pin. Route its `url_citation` annotations through `services/citations.py` rather than around it — either mapped into the existing Citation model or rendered on a second channel, never bypassing the validator. Parse `web_search_call` output items into an AuditEvent (it never passes through `ToolPolicy`, so nothing records it otherwise). Verify `store=false` compatibility first; expected fine, worth twenty minutes. Gate it behind explicit intent — agentic search adds seconds, and latency is the constraint. |
| 2 | **URL fetch** | ~90% built (§2.3). Relax the allowlist to a policy, add a readability pass, register the tool. Half a day. Also delivers the arXiv/DOI paper-ingestion line already in PRODUCT.md. |
| 3 | **Token / cost accounting** | Zero dependencies, nothing exists, and it unblocks quotas, chargeback and the observability UI simultaneously. The Responses API response already carries usage; this is one column set on `Run` and a rollup. PRODUCT.md ranks it first and it is still not done. |
| 4 | **MCP SSRF fix** | Apply `validate_public_https_url`'s private-IP checks to the MCP URL on save and connect (§2.5). An hour, and it closes something we have been telling ourselves is already closed. |
| 5 | **EU AI Act Article 14 mapping** | A documentation page. Best cost-to-value ratio in the document, and the regulation became enforceable eight days ago. |

Rationale for putting web search first rather than the composer: it is a day of work,
and its absence does damage out of proportion to its size. A product whose central
technical claim is a machine-enforced citation contract, which cannot check anything
against the world, reads as limited rather than rigorous. And unlike every other item
here, the differentiating half is already built (§2.2) — we are pointing an existing
validator at a new corpus.

### Wave 2 — the composer cluster (2-3 weeks)

| # | Item | Why here |
|---|---|---|
| 6 | **Attachments in the composer** | The highest-impact UX gap. Routing files through a separate Sources view taxes the most common opening move in knowledge work. Design the distinction competitors blur: an *attachment* is ephemeral context for one turn, a *Source* is chunked, embedded and permanently retrievable. Ship both with "add to sources" as one click. The citation/provenance story only holds for the ingested path and users must be able to tell which they are in — that turns a catch-up feature into a small differentiator. |
| 7 | **Image input** | Falls out of #6 once blob storage exists. Photographing a handwritten problem set and getting LaTeX back lands directly on our strongest existing surface. |
| 8 | **`input_file` for PDFs** | Near-free once attachments exist, and it fixes a real quality loss: `ingestion.py` is `pypdf` text-only, so every figure, chart and image-rendered equation in an academic paper is currently discarded. Page images cost tokens; spend is explicitly not a constraint. |
| 9 | **Spreadsheet parsing prototype** | Before committing to any sandbox decision, measure how much of "analyse this file" the Responses API's built-in 1,000-rows-per-sheet parsing already satisfies. A day. It may change the scope of #14. |

Blocking dependency: images and attachments need workspace-scoped object storage.
`objects_dir` is a local path today (`config.py:35`) and PRODUCT.md already lists
object storage as unfinished for container deploy. That is the real cost of this wave.

### Wave 3 — scheduled runs and the observability surface (1-2 weeks)

| # | Item | Why here |
|---|---|---|
| 10 | **Scheduled runs** | Best value-to-effort ratio on the whole list, and we are far closer than the docs say. The durable pausable loop that survives disconnect and restart is the hard part and it exists (`Run.agent_state_json`, `resume_agent_turn`). What is missing is a `ScheduledRun` row with `next_run_at` and a ticker; Vercel Cron hitting an authenticated claim endpoint is not a new always-on service. Ship it **read-only by default, parking at the first write-capable call**, landing in an approval inbox — the existing park/resume path used verbatim (§6). |
| 11 | **Run observability UI** | `RunEvent` already records every step and nothing renders it. One build serves the consumer "what is it doing right now" affordance and the enterprise run-observability line in PRODUCT.md. Gated on #3. |
| 12 | **Conversation search + branching** | Same surface, same week. Search should reuse the hybrid retrieval substrate rather than adding a third scorer — do it after hybrid lands so it inherits the fix. Branching is copying messages up to index N into a new conversation, carrying the memory scope. |

A quiet unlock: scheduled runs are the one execution path where latency is not a
constraint, which selectively un-blocks reranking (RESEARCH.md #30) and generous
retrieval budgets for that path only, without touching the interactive path.

### Wave 4 — the integration unlock (3-4 weeks)

| # | Item | Why here |
|---|---|---|
| 13 | **Skills as SKILL.md, with tool gating** | Prerequisite for #14 and #15, not an optimisation (§4.4). Adopt the published spec verbatim rather than the bespoke `Skill`/`SkillVersion` schema — it makes Anthropic's open-source pptx/docx/xlsx skills importable, which is most of the presentation ask already written. Keep per-skill tool allowlists as a namespaced frontmatter extension so skills stay portable. Resolve the Skill-vs-Agent abstraction collision PRODUCT.md already flags *before* building; two competing notions of "a configured way of behaving" is the predictable failure. Treat imported skills with the same review posture as an MCP server — they are executable instructions. |
| 14 | **MCP OAuth 2.1 client** | The highest-leverage build in the document. Per-user-per-server tokens, 2026-07-28 authorization rules, RFC 9207 issuer validation, CIMD not DCR. Pure request/response HTTP, no always-on service, no server-side execution; the Fernet plumbing and `OAuthState` table generalise directly. Also pick up the spec's MRTR — it is elicitation, and it maps almost exactly onto park/resume. |
| 15 | **Source ownership** | Ship with #14, not after. A column, a filter in `search_evidence`, and a UI affordance. Converts an unbounded liability into a documentable posture (§4.3). |
| 16 | **Curated connector directory** | Once #14 exists this is a seeded table of {name, icon, url, category, description} and a connect button, with no per-service code. Curate 20-30 servers we have actually tested — 17.2% of the public registry's advertised endpoints are dead and ~30% grade D or F. Curation beats breadth; catalogue size is not a race we enter. |

### Wave 5 — the enterprise spine (4-6 weeks)

| # | Item | Why here |
|---|---|---|
| 17 | **`Organization` above `Workspace`, then real RBAC** | The schema change is cheapest before production tenants exist. `get_actor` is already the single choke point, which is what makes RBAC medium rather than large. Precondition for #18-#21. |
| 18 | **Audit export + retention + immutability** | Small, because `AuditEvent` already has the columns and the right index. Needs: a documented stable schema, a cursor-paginated export endpoint, an actual immutability guarantee, configurable retention. Best effort-to-procurement-value ratio here — but audit events are close to meaningless in a review until roles exist to attribute them to, which is why it follows #17. |
| 19 | **Agent identity in the audit trail** | Small and specific: `AuditEvent` records an actor but nothing distinguishes the human, the agent acting autonomously, and the agent acting on a third-party credential. Add the acting identity and the `IntegrationAccount` used to each audit row and each approval preview. That is what answers "which of my users' Google accounts has this agent written through, and when." |
| 20 | **Agent access policies** | Extend `ToolPolicy` to org scope and to predicates over caller, arguments and response payload, with block / filter / flag modes and most-restrictive-wins. The one genuinely differentiating build on the enterprise list; the interception point already exists. Glean is in beta, so the window is roughly two quarters. |
| 21 | **Admin console** | The *rendering* of #3, #17, #18 and #20. Build it last or ship empty screens. |
| 22 | **SSO/SAML + SCIM** | Buy (WorkOS/Auth0), do not build, and only after RBAC. Note that SAML assertion handling shares almost nothing with the OAuth work in flight — that foundation is weaker than it looks. |

### Wave 6 — work product

| # | Item | Why here |
|---|---|---|
| 23 | **Deep research** | Mostly composition once #1, #2 and #10 exist: a long loop over search plus a document write, with cited retrieval and Documents already in place. One agent, raised iteration cap, not a swarm. The defensible claim nobody else can make: every citation deterministically checked. Independent evaluation found ~47-50% of references from Gemini and Perplexity deep research carried fabricated authors or titles, and the best performer (OpenAI) had only ~70% completely correct. Route fetched pages through the ingestion pipeline into Sources — chunked, embedded, provenance-anchored — not injected as raw context, or we inherit the same disease. |
| 24 | **File output (docx/xlsx/pptx)** | Client-side via `pptxgenjs`/`docx`/`exceljs` (§5). PDF is arguably already done. Sequence with #13 rather than as standalone tools. |
| 25 | **Presentation generation** | The owner's explicit ask, and the bar is low: PresentBench puts the best tested system at 70.8 with most in the 40s on visual design, and finds Content Completeness consistently exceeding Content Correctness — decks that look full and contain fabricated details. Gamma, the design leader, breaks its own PPTX export. Compete on grounding, not aesthetics: a deck where every claim traces to a cited chunk attacks precisely the failure the benchmark isolates. |
| 26 | **Google Calendar, then Drive/Docs write with a diff** | Calendar is the cheapest genuine win and users notice it immediately (`calendar.events` is sensitive — verification, no CASA). Drive write is the clearest competitive opening in the whole survey: ChatGPT shipped it 2026-06-15, and **Claude cannot edit an existing Doc or Sheet at all**. Ship Picker + `drive.file` and never request `drive` or `drive.readonly`. Handle incremental authorization over the existing Gmail grant explicitly rather than discovering it as a 403. |
| 27 | **Select-text-and-instruct** | Cheap, and OpenAI's Canvas retreat validates our dedicated-document bet — the reported weakness of inline blocks is multi-section navigation, which is our editor's home ground. Pass a character range into `edit_document`; the diff, approval and version-history path is untouched. |
| 28 | **XLSX import → DuckDB, XLSX export via exceljs** | The boring interop that removes the most common reason a user leaves. Explicitly *instead of* an agentic spreadsheet editor. |
| 29 | **Study aids over own sources** | Small. Table stakes now, differentiated only in the version Gemini cannot do: flashcards that link back to the exact chunk in the user's lecture notes, with spaced-repetition state in workspace memory across a term. |

### Positioning work, no engineering

- **Tell the canvas story.** Documents with markdown and LaTeX, live PDF compile,
  version history with restore, multi-file sandboxes, and every agent edit rendered as
  a unified diff and approved before it applies. That exceeds Canvas and Artifacts
  today and we describe it as "documents and sandboxes."
- **Sell the invariant, not the diff** (§3).
- **Copy Gemini's report-to-Canvas handoff** once deep research exists — a cited
  report becoming an editable document is a two-line change when both surfaces exist.

---

## 8. What not to build, and why

Written down so it is not re-litigated.

**Computer use / browser automation.** The hosted `computer` tool only emits actions
from screenshots — the caller supplies and operates the environment (Playwright, a
container with X11/VNC, a VM). That is a browser farm: a new always-on service with a
large per-tenant isolation surface, against an explicit constraint. The market is also
retreating. Google shut down Project Mariner on 2026-05-04 and folded it into Gemini
Agent; OpenAI collapsed Operator into Agent mode; the survivors sit behind $249/mo
tiers and ~40-message monthly caps, which is what rationing an expensive, unreliable
capability looks like. MCP connectors already serve the real "act in another system"
need at a fraction of the surface.

**Voice / realtime.** Needs a persistent bidirectional relay — a direct constraint
violation — for near-zero payoff on a surface made of LaTeX, diffs, documents and
dashboards. Nobody approves a unified diff by voice. If it is ever demanded, ship
one-way voice-note transcription into the composer, which is a file upload to a
transcription endpoint and needs no realtime infrastructure.

**Server-side context compaction.** RESEARCH.md #42 holds transcript compaction on
measured risk with unmeasured benefit, and §7.3 establishes that the system prompt
never enters the compactable region — grounded in the Governance Decay result, where
unsafe tool-call violations rose from 0% to 30% average and 59% worst-case after
compaction. Hosting it makes that strictly worse by removing control over what
survives. Our turn is ~8k tokens against a 1M-token window. Hosted availability
changes the convenience, not the analysis.

**Tool search.** Right technique, wrong scale. The papers measuring benefit run
584-4,000 tools; we run ~34, and the same literature measures a hard recall cliff
(fixed K=5 finds 0% of tools ranked 6-20). Buying a recall cliff to solve a token
problem we do not have is a bad trade. `tool_choice: allowed_tools` plus deterministic
group gating, with skills as the grouping, is the answer. Revisit past ~100 tools.

**OpenAI's built-in agent loop and hosted shell.** The loop *is* the product —
durable, resumable, approval-gated, audited, per-tool policy, rendered diff before
every write. Adopting the provider's loop surrenders exactly the property that makes
the enterprise story true, for convenience we do not need. Use hosted *tools*, never
the hosted *loop*. The hosted shell is a separate question that lands the same way as
`code_interpreter`, except worse: a general shell with network access is far harder to
reconcile with "sandboxes with no network" as a compliance claim than a Python
container for one analysis. If server-side execution is ever adopted, adopt the narrow
tool, never the shell.

**Eleven bespoke connectors** (Slack, Notion, Linear, Jira, GitHub, Asana, Confluence,
Salesforce, HubSpot, Zendesk, Figma). Every one ships an official remote MCP server.
Eleven large builds plus permanent maintenance against eleven third-party APIs, versus
one medium build that reaches all of them and several thousand more. There is no
version of this arithmetic that favours hand-building. The only defensible exception
would be an integration so central to our identity that MCP's generic tool shape loses
something essential — and our identity is documents, LaTeX, memory and governance.

**Microsoft 365.** Microsoft owns the identity, the data and the client. Anthropic,
with vastly more resources, shipped read-only. Entra app registration, admin consent
and Graph permission sets are a multi-month project whose best outcome is a worse
Copilot. Reach it through MCP if a deal requires it. Note that our unbuilt DOCX export
covers more genuine Microsoft-shaped demand than a SharePoint connector would.

**Publishing as a ChatGPT app.** The approval gate, the rendered diff, the durable
pausable loop and workspace-scoped memory are the product, and none of them survive
being reduced to a tool call inside someone else's agent loop under someone else's
approval model. You trade the customer relationship for a directory listing, and
digital-goods monetization is not even shipped. Reconsider only if we ever have a
single narrow capability worth exposing standalone — offline in-browser LaTeX-to-PDF
is the plausible candidate — and even then as a deliberate wedge, not a strategy.

**A connector marketplace with third-party publishing.** A curated directory is a
seeded table and a connect button. A marketplace is review pipelines, developer terms,
revenue share and a trust-and-safety function. Anthropic, OpenAI and Microsoft are
competing on catalogue size; 17.2% of the public registry's advertised endpoints are
dead anyway.

**Agentic spreadsheet editing.** On SpreadsheetBench 2 (end-to-end business
workflows), the best shipping product — Claude for Excel — scores 15.4%, and the best
model 34.89%. Even on the easier original benchmark, market leader Copilot reaches
57.2%. Chasing a capability where the leader fails five workflows in six, with an
approach requiring server-side execution we have refused, is a bad trade. Ship the
interop instead (#28).

**Real-time co-editing (CRDT/OT).** Multiplayer is expected of document *suites* and
conspicuously absent from every chat-native AI product; none has been punished for it.
The most expensive item discussed anywhere in this survey, and versioned async editing
with agent diffs covers the need. If pressure appears, note that inline **comments**
are a completely different and far cheaper feature — an anchored thread on a document
range, no CRDT — and are the piece enterprises actually ask about. Build comments,
never co-editing.

**A Gamma-style designed web deck.** Gamma spent three years becoming the design
benchmark and its PPTX export still flattens layouts to images and breaks slide
geometry. Competing on visual polish means competing with Google's native Slides
renderer and losing.

**A custom-instructions surface.** Skills, memory and the designed style profile
already cover it three ways. A fourth creates exactly the abstraction collision
PRODUCT.md warns about.

**Prompt A/B testing for customers.** Nobody in this category ships it — it lives in
Braintrust, LangSmith and Langfuse, which sell to the builder, not the buyer's admin.
PRODUCT.md correctly blocks it on measurement that does not exist. Keep exactly one
piece: the prompt fingerprint column on `Run` (RESEARCH.md #12), because adding it
later makes all historical data unattributable.

**Visual workflow / DAG builder.** OpenAI shipped Agent Builder in Oct 2025 and
announced its wind-down on 2026-06-03, removal 2026-11-30. Fourteen months from launch
to death at the company with the most distribution in the category. Saved,
parameterised, schedulable runs over the existing tools cover the demand.

**Single-tenant VPC / on-prem.** Contradicts the container + ephemeral-disk
constraint. ChatGPT Enterprise — the largest product in the category — ships SaaS-only
with residency options and no VPC. Revisit only against a named six-figure deal
contingent on it.

**EU data residency.** Anthropic does not have it and sells enterprise successfully
anyway, routing EU customers to Bedrock/Vertex when pushed. It is a hard blocker for
EU deals and irrelevant to every other deal, which makes it a market-selection
decision, not a build. But **do** prepare an answer to "whose OpenAI contract is our
data flowing under, and can we see its retention terms" — that comes up in every
review and "a key in an env var" is not an answer. Enabling ZDR on the account and
saying so is cheap and buys most of the credibility.

**ISO 42001 and FedRAMP.** Both real, both premature. 42001 is a follow-on logo for
vendors who already hold SOC 2 Type II and ISO 27001; no buyer rejects you for lacking
it while accepting a vendor without SOC 2. FedRAMP is a multi-year public-sector
programme.

**HIPAA BAA** unless healthcare is a deliberate target. The blocker is not code — you
cannot sign a BAA while the subprocessor relationship with OpenAI is an API key in an
env var, and OpenAI's own BAA terms would have to flow through. Note also that
HIPAA-readiness and zero retention are in tension: Anthropic's covered models require
30-day retention under HIPAA-ready service.

**Further polish on the diff preview.** It converged in the market between May and
July 2026. Move that energy up a level, to the policy layer, where Glean is still in
beta.

### A structural risk worth naming

Enterprise buyers ask for BYO-key and data residency. We are architecturally "OpenAI
only, one key, required at startup" (`config.py:42-43`, `openai_model: str =
"gpt-5.5"`). That key is a single-tenant assumption baked into a multi-tenant product,
and it is the constraint most likely to break under enterprise pressure — not because
BYO-key is a great feature, but because "whose contract is my data flowing under" is a
question every security review asks. Have an answer before it is needed.

---

## 9. Sources

Marked **[preview]** or **[unshipped]** where the capability is not GA. Dates are
ship/announcement dates as reported. Confidence note carried from the research: OpenAI
API facts come from developers.openai.com primary docs; ChatGPT Work / Agent-mode
consolidation (Jul 2026) and Claude Cowork dates come from secondary press and are
directionally reliable but not vendor-primary. Several benchmark citations are
preprints from the last two months and are marked **[preprint]**.

### OpenAI platform (primary docs)
- Web search tool — https://developers.openai.com/api/docs/guides/tools-web-search (GA; `web_search_preview` deprecated)
- Code interpreter — https://developers.openai.com/api/docs/guides/tools-code-interpreter (GA)
- Computer use — https://developers.openai.com/api/docs/guides/tools-computer-use (GA on gpt-5.6; `computer_use_preview` deprecated)
- PDF / file inputs — https://developers.openai.com/api/docs/guides/pdf-files (GA)
- Background mode — https://developers.openai.com/api/docs/guides/background (GA)
- Tools overview — https://developers.openai.com/api/docs/guides/tools
- Changelog (Skills 2026-02-10; hosted shell 2026-02-10; compaction 2026-02-10; `tool_search` 2026-03-05; agent loop Mar 2026; deep research models) — https://developers.openai.com/api/docs/changelog
- Pricing — https://developers.openai.com/api/docs/pricing
- Apps SDK monetization (digital goods **[unshipped]**) — https://developers.openai.com/apps-sdk/build/monetization
- Apps in ChatGPT / submissions open 2026-07-09 — https://openai.com/index/developers-can-now-submit-apps-to-chatgpt/ and https://openai.com/index/introducing-apps-in-chatgpt/
- AgentKit / Agent Builder (**winding down**, announced 2026-06-03, removed 2026-11-30) — https://openai.com/index/introducing-agentkit/
- Data residency expansion — https://openai.com/index/expanding-data-residency-access-to-business-customers-worldwide/
- Google Drive/Docs/Sheets/Slides write, GA 2026-06-15 — https://help.openai.com/en/articles/20001278
- ChatGPT Sites, 2026-07-09 (**public beta**, excludes Free/Go, not in EEA/CH/UK) — https://help.openai.com/en/articles/20001339-creating-and-managing-chatgpt-sites
- Chat history search — https://help.openai.com/en/articles/10056348-how-do-i-search-my-chat-history-in-chatgpt

### Anthropic / Claude
- File creation (GA paid 2025-10-21, free 2026-02-11) — https://claude.com/blog/create-files and https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude
- File upload — https://support.claude.com/en/articles/8241126-upload-files-to-claude
- Chat search and memory — https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context
- Release notes (in-place artifact editing Jun 2026; enterprise skill scanning 2026-08-06) — https://support.claude.com/en/articles/12138966-release-notes
- Custom connectors / remote MCP (GA all tiers) — https://support.claude.com/en/articles/11175166-about-custom-connectors-remote-mcp-servers
- Google Workspace connectors (**cannot edit an existing Doc or Sheet**) — https://support.claude.com/en/articles/10166901-use-google-workspace-connectors
- Published artifacts (2026-07-13; unpublish is one-way) — https://support.claude.com/en/articles/9547008-publish-and-share-artifacts
- Team/Enterprise: SSO/SCIM, seat management, spend limits 2026-07-02 — https://www.anthropic.com/news/claude-code-on-team-and-enterprise
- API data retention / ZDR — https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
- BAA — https://privacy.claude.com/en/articles/8114513-business-associate-agreements-baa-for-commercial-customers
- Claude Code security / MCP allowlists — https://code.claude.com/docs/en/security
- Compliance API (2025-08-20; 28 integrations 2026-05-21) — https://generalanalysis.com/guides/claude-compliance-api
- Office add-ins GA 2026-05-07 — https://releasebot.io/updates/anthropic/claude and https://thenewstack.io/claude-word-excel-powerpoint-outlook-microsoft-office/
- Open-source pptx skill — https://github.com/anthropics/skills/tree/main/skills/pptx

### Model Context Protocol
- Spec 2026-07-28 announcement (stateless, MRTR, routing headers, CIMD, RFC 9207; HTTP+SSE / Roots / Sampling / Logging deprecated on a 12-month clock) — https://blog.modelcontextprotocol.io/posts/2026-07-28/
- Streamable HTTP transport — https://modelcontextprotocol.io/specification/draft/basic/transports/streamable-http
- Authorization (2025-06-18, superseded) — https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- Registry snapshot 2026-07-28 (18,849 servers; 49.9% remote; 55.8% of remote unauthenticated; 27.0% gate the tool list; 17.2% dead endpoints) — https://mcpqueen.com/reports/state-of-mcp-2026-07
- AWS AgentCore Gateway migration notice — https://aws.amazon.com/blogs/machine-learning/how-agentcore-gateway-supports-the-mcp-2026-07-28-spec/

### Google
- Gemini in Slides, native editable decks, GA 2026-06-30 — https://workspaceupdates.googleblog.com/2026/06/create-fully-native-and-editable-presentations-with-Gemini-in-Google-Slides.html
- Gemini Workspace updates (Workspace Intelligence GA 2026-04-22) — https://blog.google/products-and-platforms/products/workspace/gemini-workspace-updates-march-2026/
- Scheduled Actions (GA, 10 concurrent) — https://blog.google/products-and-platforms/products/gemini/scheduled-actions-gemini-app/
- Deep Research API — https://ai.google.dev/gemini-api/docs/interactions/deep-research
- Granular OAuth consent, web apps Nov 2025 — https://workspaceupdates.googleblog.com/2025/11/granular-oauth-consent-in-webapps.html
- Drive API scopes (`drive.file` non-sensitive; `drive`/`drive.readonly` restricted) — https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- Granular permissions — https://developers.google.com/identity/protocols/oauth2/resources/granular-permissions
- Gemini Canvas — https://support.google.com/gemini/answer/16047321
- Google Docs + Gemini co-editing, 2026-03-10 — https://9to5google.com/2026/03/10/google-docs-gemini-upgrade/
- **Project Mariner shut down 2026-05-04** — https://www.androidheadlines.com/2026/05/google-shuts-down-project-mariner-ai-agent.html
- Gemini Enterprise FAQ (VPC-SC, CMEK; BYOK **[unshipped]**, planned 2026) — https://cloud.google.com/gemini-enterprise/faq

### Microsoft
- Copilot agentic capabilities in Word/Excel/PowerPoint GA 2026-04-22 — https://www.microsoft.com/en-us/microsoft-365/blog/2026/04/22/copilots-agentic-capabilities-in-word-excel-and-powerpoint-are-generally-available/
- Copilot Cowork GA 2026-06-16 — https://www.microsoft.com/en-us/microsoft-365/blog/2026/06/16/copilot-cowork-is-now-generally-available/
- Researcher and Analyst GA — https://www.microsoft.com/en-us/microsoft-365/blog/2025/06/02/researcher-and-analyst-are-now-generally-available-in-microsoft-365-copilot/
- Copilot connectors overview — https://learn.microsoft.com/en-us/microsoft-365/copilot/connectors/overview
- Federated Copilot Connectors GA May 2026 — https://techcommunity.microsoft.com/blog/microsoft365copilotblog/fueling-new-experiences-in-microsoft-365-copilot-with-expanded-copilot-connector/4493246
- Agent mode in Excel — https://techcommunity.microsoft.com/blog/excelblog/building-agent-mode-in-excel/4457320
- Copilot agent mode diffs in VS 2026 — https://learn.microsoft.com/en-us/visualstudio/ide/copilot-agent-mode
- Copilot Studio Agent Evaluation GA 2026-03-31 — https://techcommunity.microsoft.com/blog/copilot-studio-blog/agent-evaluation-in-microsoft-copilot-studio-is-now-generally-available/4507392
- Entra agent identities in Copilot Studio — https://learn.microsoft.com/en-us/microsoft-copilot-studio/admin-use-entra-agent-identities
- Agent 365 GA 2026-05-01 — https://m365admin.handsontek.net/microsoft-agent-365-becomes-generally-available-ga/
- Copilot data residency US/EU + FedRAMP, 2026-04-13 — https://github.blog/changelog/2026-04-13-copilot-data-residency-in-us-eu-and-fedramp-compliance-now-available/
- Copilot scheduled prompts — https://windowsforum.com/threads/microsoft-copilot-tasks-unified-scheduling-with-researcher-and-analyst-agents.401437/

### Glean, Notion, Okta, identity
- Glean agent access policies (**beta**, docs updated 2026-06-09) — https://docs.glean.com/administration/protect/ai-security/agent-access-policies
- Glean May 2026 release, write previews 2026-05-06 — https://www.glean.com/blog/may-2026-launch and https://docs.glean.com/release-notes/releases/2026-05-06-may-release
- Glean agent governance — https://www.glean.com/product/agent-governance
- Glean security (SOC 2 Type II, ISO 27001, ISO 42001, VPC) — https://www.glean.com/security
- Glean Enterprise ADLC, May 2026 — https://www.glean.com/press/glean-introduces-the-enterprise-agent-development-lifecycle-codifying-how-enterprises-build-govern-and-measure-ai-agents
- Notion Plan Mode — https://www.notion.com/help/review-and-approve-plans-before-notion-ai-runs
- Notion agents — https://www.notion.com/product/agents
- Okta for AI Agents (GA 2026-04-30) — https://www.okta.com/products/govern-ai-agent-identity/ and https://www.okta.com/newsroom/press-releases/showcase-2026/
- Dust agent logging — https://dust.tt/blog/secure-enterprise-platform-ai-agents

### Skills standard
- Agent Skills spec (open standard 2025-12-18; ~32 tools) — https://agentskills.io/
- Interoperability writeup — https://www.paperclipped.de/en/blog/agent-skills-open-standard-interoperability/
- Anthropic enterprise skills / open standard — https://venturebeat.com/technology/anthropic-launches-enterprise-agent-skills-and-opens-the-standard
- OpenAI Responses API skills support — https://venturebeat.com/orchestration/openai-upgrades-its-responses-api-to-support-agent-skills-and-a-complete

### Benchmarks and evaluations
- PresentBench (238 instances, ~54 rubric items, best 70.8) — https://presentbench.github.io/
- SpreadsheetBench 2 **[preprint]**, arXiv 2606.29955 — https://arxiv.org/html/2606.29955
- Excel agent benchmark comparison — https://gptforwork.com/blog/ai-agents-for-excel-benchmark
- Deep research reference-accuracy study **[preprint]**, arXiv 2604.03173 — https://arxiv.org/pdf/2604.03173
- DeepResearch Bench — https://deepresearch-bench.github.io/
- JMIR citation-accuracy study 2026 — https://www.jmir.org/2026/1/e88195
- Gamma PPTX export failure modes — https://www.slidegmm.ai/en/blog/gamma-export-powerpoint-quality-guide

### Compliance and regulation
- EU AI Act Article 14 (enforceable 2026-08-02) — https://artificialintelligenceact.eu/article/14/
- Article 14 practitioner reading — https://www.deepinspect.ai/blog/eu-ai-act-article-14-human-oversight
- SOC 2 Type II cost/timeline benchmarks — https://www.humanr.ai/intelligence/soc-2-type-2-cost-benchmarks-timeline-120k and https://www.strac.io/blog/soc-2-type-2
- AI vendor security questionnaires (SSO vs "Sign in with Google") — https://www.deepinspect.ai/blog/eu-ai-act-article-14-human-oversight and https://www.deepinspect.ai/blog/ai-vendor-security-questionnaire
- ChatGPT Enterprise admin controls — https://intuitionlabs.ai/articles/chatgpt-enterprise-admin-controls-security

### Secondary press (directionally reliable, not vendor-primary)
- ChatGPT Work / Agent-mode consolidation, Jul 2026 — https://releasebot.io/updates/openai/chatgpt and https://aitoolsreview.co.uk/insights/chatgpt-work
- ChatGPT Tasks — https://www.developersdigest.tech/blog/chatgpt-tasks
- Claude Cowork GA — https://www.testingcatalog.com/anthropic-launches-claude-cowork-in-general-availability/ and https://techcrunch.com/2026/07/07/the-coding-agent-wars-are-spilling-into-the-rest-of-the-office-claude-cowork/
- ChatGPT for PowerPoint GA 2026-07-06 — https://letsdatascience.com/news/openai-makes-chatgpt-for-powerpoint-generally-available-a5fc0a3e
- Canvas removal from GPT-5.5, 2026-05-28 — https://node-pad.com/blog/what-happened-to-chatgpt-canvas/ and https://theaicareerlab.com/blog/chatgpt-what-changed-june-2026
- Conversation branching, Sep 2025 — https://scalevise.com/resources/branching-conversations-chatgpt-examples/
- OpenAI spend controls 2026-06-18, agent metering 2026-07-06 — https://beyondtmrw.org/article/openai-adds-enterprise-spend-controls-as-chatgpt-adoption-scales and https://useauteur.com/blog/chatgpt-workspace-agents-pricing-credits-2026
- Snowflake Cortex Analyst — https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst and https://colrows.com/blogs/cortex-analyst-vs-genie/
- Claude connectors directory — https://www.usecarly.com/blog/claude-connectors/
- Gemini report output formats — https://www.datastudios.org/post/google-gemini-for-research-reports-structure-citations-and-output-formats
- LLM observability platform comparison — https://www.marktechpost.com/2026/08/09/top-llm-observability-and-evaluation-platforms-in-2026-langfuse-langsmith-braintrust-arize-and-more-compared/amp/

### Internal
- `docs/RESEARCH.md` §6.1 (tool budget), #5 (citation validator — **built**, see §2.2),
  #12 (prompt fingerprint), #30 (reranking), #42 (compaction), #45 (multi-agent), #50
  (approval statistics must not relax a gate), §7.3 (system-prompt invariant), §8
  (researcher disagreements), §11 (sources).
- `docs/PRODUCT.md` — segmentation, the governance spine, the measurement dependency.
- `docs/adr/0004-sandboxed-generated-apps.md` — the sandbox decision re-read in §5.
