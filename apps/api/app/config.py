from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The reasoning-effort ladder, named once so the setting and the per-turn request
# override cannot drift. It was inline on `openai_reasoning_effort`; a second copy
# on `SendMessageRequest` would be a Literal that silently disagreed the day this
# list changed, and a request validated against the stale one is a 422 for a value
# the model actually accepts (or worse, a pass for one it rejects).
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]


class ModelPrice(BaseModel):
    """What one model costs, in USD per million tokens.

    Rates live in settings rather than in a constant because a price is not a
    property of the code: OpenAI reprices models without asking, and a hardcoded
    table would make "our invoice moved" a deploy instead of an env change.

    `cached_input` is optional and falls back to `input`. That fallback is the
    conservative direction — a cache discount we do not know about makes the
    recorded cost too high, never too low, and a silently under-reported bill is
    the failure mode worth avoiding.
    """

    input: float = Field(ge=0.0)
    output: float = Field(default=0.0, ge=0.0)
    cached_input: Optional[float] = Field(default=None, ge=0.0)

    @property
    def effective_cached_input(self) -> float:
        return self.input if self.cached_input is None else self.cached_input


def project_root() -> Path:
    """Repo root, so relative paths mean the same thing from any working dir."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "Makefile").is_file() and (candidate / "apps").is_dir():
            return candidate
    return Path.cwd()


class Settings(BaseSettings):
    # Defaults to production, and that inversion is the point. Every relaxation in
    # this file — the credential-free dev sign-in, DEV_AUTO_LOGIN, the scripted
    # model, create_all + seeding, printing email links to the console — is gated
    # on `is_dev_env`. When this defaulted to "development" the whole set was
    # opt-OUT: a deployment that simply forgot to set APP_ENV came up with the
    # doors open, which was demonstrated, not theorised. Local work sets
    # APP_ENV=development explicitly (scripts/dev.sh and .env.example both do);
    # forgetting it now costs a developer a clear error instead of costing a
    # deployment its front door.
    app_env: str = "production"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://localhost:3000"
    database_url: str = "sqlite:///./data/workspace.db"
    objects_dir: Path = Path("./data/objects")
    tool_host_allowlist: str = "api.github.com"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_tool_response_bytes: int = 256 * 1024
    model_provider: Literal["openai", "scripted"] = "openai"
    # Path to a JSON script for MODEL_PROVIDER=scripted. See services/scripted_model.py.
    scripted_model_script: Optional[Path] = None
    openai_api_key: Optional[SecretStr] = None
    openai_model: str = "gpt-5.5"
    openai_reasoning_effort: ReasoningEffort = "low"
    # Optional deployment override for the per-turn model allow-list, comma
    # separated. Unset derives the list from the priced models (see
    # `selectable_models`); set it when a deployment prices models it does not
    # want user-selectable, or wants to offer one it has not priced.
    selectable_models_raw: str = ""
    openai_timeout_seconds: float = 60.0
    openai_max_output_tokens: int = 1200
    openai_embedding_model: str = "text-embedding-3-small"
    openai_codegen_max_output_tokens: int = 16000

    # --- Model pricing -----------------------------------------------------
    # {model name: {"input": …, "output": …, "cached_input": …}} in USD per
    # million tokens, e.g.
    #   MODEL_PRICES='{"gpt-5.5":{"input":1.25,"output":10,"cached_input":0.125}}'
    #
    # Empty by default, and that emptiness is the honest answer rather than a
    # gap. Token counts are facts this app measures; prices are facts only the
    # provider's price list knows, and shipping a guessed default would put a
    # fabricated number on an admin dashboard labelled "cost". With no rate for a
    # model, `model_usage` rows still record every token and leave `cost_usd`
    # null, and GET /api/admin/usage names the unpriced models so an operator can
    # see exactly what configuring this would buy.
    #
    # The lookup is by exact model name, including the dated snapshot if that is
    # what OPENAI_MODEL names — a prefix match would price `gpt-5-nano` off a
    # `gpt-5` entry, which is wrong by an order of magnitude in the direction
    # nobody notices.
    model_prices: Dict[str, ModelPrice] = {}

    # --- Spend ceiling (ADR 0008) ------------------------------------------
    # A rolling per-workspace cap on model spend. Unset is unlimited, and that
    # has to stay true: a deployment that upgrades into this release must not
    # discover a cap nobody chose. Everything below is opt-in.
    #
    # The window is rolling, not calendar: "the last N hours", evaluated at the
    # moment of the check. A calendar day would let a runaway loop spend the
    # whole ceiling at 23:59 and the whole ceiling again a minute later.
    budget_window_hours: int = Field(default=24, ge=1, le=8760)
    # USD across the window, over every operation the workspace performed.
    budget_usd_per_window: Optional[float] = Field(default=None, ge=0.0)
    # Total tokens across the window. The ceiling that still works when there is
    # no price list — see `_guard_budget` — and the only one that can bound a
    # model this deployment cannot price.
    budget_tokens_per_window: Optional[int] = Field(default=None, ge=0)
    # What fraction of the ceiling unattended work (workflow nodes) may spend.
    # A tighter default for automations without imposing an absolute one: a
    # deployment with no ceiling still has no ceiling, and a deployment that
    # sets one gets a scheduled DAG stopped at half of it, well before a person
    # typing is. 1.0 makes the two equal.
    unattended_budget_fraction: float = Field(default=0.5, gt=0.0, le=1.0)

    # --- Web search --------------------------------------------------------
    # OpenAI's hosted web_search tool, which runs on the provider side and comes
    # back with url_citation annotations. Those annotations go through the same
    # citation validator as retrieval evidence rather than around it: a product
    # whose central claim is an enforced citation contract cannot have one class
    # of source that is exempt from it.
    web_search_enabled: bool = True
    web_search_max_results: int = 8
    # "low" trades recall for latency. Search is on the interactive path and the
    # stated constraint is that spend is fine and latency is not.
    web_search_context_size: Literal["low", "medium", "high"] = "medium"

    # --- Prompt-injection screen (threat model) ----------------------------
    # A classifier over the UNTRUSTED content a turn ingests — retrieved
    # passages, the open document, web_search results, tool/MCP output — none of
    # which the user typed. Off by default, and that default is the fail-safe
    # one: when off, `classify` is never called and a turn behaves byte-for-byte
    # as it does today. Opt in, and `_guard_screen` then insists the config can
    # actually run, the same discipline as `_guard_sandbox`.
    screen_enabled: bool = False
    # shadow — record every hit (event + audit) and change nothing. The
    #          measurement posture: see what would trip before it gates anyone.
    # enforce — a hit escalates THAT turn to the strictest approval posture
    #           (ask_all: every tool call parks for a human), so an injection
    #           cannot ride a thread's bypass into an auto-approved write.
    screen_mode: Literal["shadow", "enforce"] = "shadow"
    # builtin — the cheap context model with a tight classification prompt.
    # proxy   — an external HTTPS endpoint, SSRF-guarded like the tool fetch.
    screen_backend: Literal["builtin", "proxy"] = "builtin"
    # The proxy endpoint, required for SCREEN_BACKEND=proxy. A server-side
    # destination, never exposed on bootstrap — same class as WORKFLOW_CRON_SECRET.
    screen_proxy_url: str = ""
    # Score at or above which a chunk is an injection. The any-high-means-high
    # combine rule then makes one tripped chunk trip the whole passage.
    screen_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- MCP authentication ------------------------------------------------
    # Remote MCP servers authenticate with OAuth 2.1 + dynamic client
    # registration, so there is no per-server configuration to put here — the
    # client discovers and registers itself. This only bounds it.
    mcp_oauth_enabled: bool = True
    # Refresh this far before expiry rather than on a 401, so a long agent turn
    # does not fail its ninth tool call on a token that expired mid-run.
    mcp_token_refresh_leeway_seconds: int = 120

    # --- Sandbox (server-side execution, ADR 0005) -------------------------
    # Execution runs in per-session microVMs at a provider, never on this host.
    # SANDBOX_PROVIDER=fake is the in-process test double; it executes nothing
    # and exists so the tool, quota and approval layers are testable without a
    # key. It is not an offline mode — "fake" refuses to boot outside dev/test.
    # Off by default, and this one is deliberately NOT the fail-closed inversion
    # the auth and model settings use. Those default to their safe state and the
    # unsafe state is opt-in; here the "safe" state and the "off" state are the
    # same thing, because execution needs a provider key that no existing deploy
    # has. Defaulting this to True made Settings refuse to boot everywhere the
    # key was absent — which is every environment that predates this feature,
    # including the test suite. So: opt in, and once you have opted in,
    # _guard_sandbox insists the configuration actually works.
    sandbox_enabled: bool = False
    # subprocess — this host, this uid, NO isolation. Development only, and
    #              _guard_sandbox makes that structural rather than advisory.
    # container  — Docker with --network none and dropped capabilities. The
    #              real boundary, and the deployment target.
    # e2b        — hosted Firecracker microVMs. Kept behind the seam as a
    #              managed option; not the default.
    # fake       — executes nothing. Test double.
    sandbox_provider: Literal["subprocess", "container", "e2b", "fake"] = "subprocess"
    sandbox_api_key: Optional[SecretStr] = None
    # Blank means the provider's default image (Python + pandas/matplotlib).
    sandbox_template: str = ""

    # --- Container driver --------------------------------------------------
    # Built from infra/sandbox/Dockerfile, which is also the package policy:
    # the sandbox has no network, so anything importable has to already be in
    # the image. `make sandbox-image` builds it.
    sandbox_container_image: str = "jasmine-sandbox:latest"
    sandbox_docker_binary: str = "docker"
    # Interpreter for the `subprocess` driver. Blank means the API's own venv,
    # whose package set is NOT the container image's — code that imports scipy in
    # development may fail in deployment. Point this at a venv built from
    # infra/sandbox/requirements to make the two agree.
    sandbox_python_binary: str = ""
    sandbox_memory_mb: int = 2048
    sandbox_cpus: float = 2.0
    # Fork-bomb ceiling. Low enough to matter, high enough that a multiprocessing
    # Pool over a few cores still works.
    sandbox_pids_limit: int = 256
    # Per-session scratch on the host, bind-mounted at /workspace. Files written
    # here survive between executions, which is what makes a session a workspace
    # rather than a one-shot. Anchored to the repo root like every other path.
    sandbox_workdir: Path = Path("./data/sandboxes")
    # How long a sandbox stays up between uses before the provider reclaims it.
    # Sessions are paused rather than killed on timeout, so this is a cost dial,
    # not a data-loss one: a paused sandbox resumes with its filesystem intact.
    sandbox_session_timeout_seconds: int = 900
    sandbox_exec_timeout_seconds: float = 120.0
    # Quotas, enforced before creation rather than discovered on the invoice.
    sandbox_max_concurrent_per_workspace: int = 3
    sandbox_max_execs_per_run: int = 20
    # A paused sandbox costs storage indefinitely, so idle ones get reaped.
    sandbox_session_idle_days: int = 7
    # none      — no egress at all. THE DEFAULT, and the reason this feature has
    #             no exfiltration story to worry about: prompt injection through
    #             an uploaded document cannot send anything anywhere if there is
    #             no route. The cost is that packages cannot be installed at
    #             runtime, which is why infra/sandbox/Dockerfile pre-bakes them.
    # allowlist — only SANDBOX_HOST_ALLOWLIST. For a workspace that genuinely
    #             needs to reach a named API.
    # open      — full egress. Deliberate opt-in. Turning this on re-opens the
    #             path ADR 0005 describes: a sandbox holding the user's
    #             documents, with a socket, running code an injected document
    #             may have influenced.
    # Cloud metadata and link-local ranges are denied in every mode including
    # `open`; that denial lives in code (services/sandbox/policy.py), not here,
    # because it is not a policy an operator should be able to switch off.
    sandbox_network_policy: Literal["open", "allowlist", "none"] = "none"
    sandbox_host_allowlist: str = "pypi.org,files.pythonhosted.org,registry.npmjs.org"
    sandbox_max_output_bytes: int = 64 * 1024

    # --- Retrieval ---------------------------------------------------------
    # Three independently switchable stages, so each can be ablated and measured
    # on its own (RESEARCH.md §4). Every one of them degrades to the stage below
    # it rather than to nothing: no term index still ranks lexically, no vectors
    # still ranks lexically, no blurb still embeds the author's text.
    #
    # RETRIEVAL_BM25=0 falls back to the pre-BM25 term-frequency scorer, which is
    # the measured lexical baseline the hybrid has to beat.
    retrieval_bm25: bool = True
    # RETRIEVAL_HYBRID=0 turns off the dense arm and RRF fusion, leaving whichever
    # lexical scorer is configured to rank alone.
    retrieval_hybrid: bool = True
    # RETRIEVAL_CONTEXTUAL=0 stops ingest generating the situating blurb. Off by
    # default, and not merely because it is an LLM call per chunk: measured on our
    # corpus it *costs* a question — overall recall@5 96.4% against 100% without
    # it, the same indirect question missed in 3 of 3 runs (tasks/todo.md). The
    # eval documents average 246 characters and are already self-contained, so the
    # blurb has no lost context to restore and only dilutes the embedded text.
    # Switch it on for a workspace whose documents are long enough for a chunk to
    # have lost its context, and re-measure there before trusting it.
    retrieval_contextual: bool = False
    # The blurb is a one-sentence classification job, not a reasoning one, so it
    # runs on the cheapest model rather than the product model.
    openai_context_model: str = "gpt-5-nano"
    # RRF's rank-smoothing constant. 60 is the value from the original paper and
    # the one every hybrid baseline in the literature uses; it decides how much a
    # rank-1 hit in one arm outweighs a rank-3 hit in both.
    retrieval_rrf_k: int = 60
    # How deep each arm's ranking goes into the fusion. Deeper costs nothing at
    # query time (both rankings are already computed) but does let a chunk that
    # neither arm liked reach the answer on two mediocre ranks.
    retrieval_fusion_depth: int = 50
    # Ceiling on postings the term index returns for one query, highest tf first.
    # A stopword-ish term that survives tokenization can match the whole corpus.
    retrieval_posting_limit: int = 20000
    # Ceiling on chunk vectors one search scores, newest first. Same shape and
    # same trade as memory_recall_candidate_cap: it is a recency window.
    retrieval_vector_candidate_cap: int = 20000
    # Minimum cosine for a chunk to enter the fusion through the dense arm.
    # Without it the dense arm ranks the *entire* corpus for every query — cosine
    # is rarely exactly zero — so a question the workspace has no answer to would
    # still come back with five confident-looking citations, where the purely
    # lexical retriever correctly returned nothing. 0.3 is not a new guess: it is
    # the same floor `memory.recall` already applies to its semantic candidates.
    #
    # It also turns out to *help* ranking, measured A/B on one embedded corpus so
    # the floor is the only variable (overall recall@5 / paraphrase MRR):
    #   0.0 -> 96.4% / 0.900    0.2 -> 100% / 0.925
    #   0.3 -> 100%  / 0.950    0.4 -> 96.4% / 0.900, and indirect drops to 90%
    # RRF weighs a rank, not a score, so every barely-related chunk the dense arm
    # admits at rank 6-50 is a chunk competing on equal footing with a real match.
    # Above ~0.4 the floor starts cutting true matches instead.
    retrieval_dense_floor: float = 0.3

    memory_enabled: bool = True
    memory_max_items_per_run: int = 5
    memory_recall_limit: int = 6
    # MEMORY_SUPERSESSION=0 restores the pre-claim-key write path so the effect
    # can be measured both ways (scripts/evaluate_memory.py). It switches two
    # things together, because either one alone is a behaviour that was never
    # measured: memories are keyed on a hash of their content again, and a newer
    # claim never retires an older one. Measured on evals/memory_corpus.json,
    # off = 100% stale-served (5/5), on = 0% (0/5) at unchanged recall.
    memory_supersession: bool = True
    # Ceiling on the vectors one recall scores, newest first. 0 means uncapped —
    # exact, but linear: a 100k-row workspace costs ~560ms and ~1.4GB of transient
    # RSS per turn. See services/memory.py.
    #
    # 20000, not 5000. A cap is a recency window, so any memory older than it is
    # invisible to semantic scoring no matter how relevant: measured, five
    # memories at cosine 0.97 sitting past a 5000-row window went from 5/5
    # recalled to 0/5 — the assistant silently forgetting something it knew.
    # 20000 reproduced the unbounded ordering exactly on a 10k-row corpus and
    # still runs ~14x faster than the old full scan. The extra ~35ms buys back
    # correctness, which is the wrong thing to trade away here.
    memory_recall_candidate_cap: int = 20000
    # Ceiling on rows returned by the LIKE prefilter that feeds lexical scoring.
    memory_lexical_candidate_limit: int = 400
    memory_transcript_messages: int = 10
    run_lease_seconds: int = 90

    # --- Workflow schedules ------------------------------------------------
    # The bearer token an external cron presents to POST /api/workflows/tick.
    # Unset means no dispatch at all, and that default is the honest one: ADR
    # 0007 says a schedule the system does not keep is worse than a missing
    # feature, so a deployment that has not configured a ticker has genuinely
    # inert schedules rather than an endpoint anyone can poke.
    workflow_cron_secret: Optional[SecretStr] = None

    google_client_id: str = ""
    google_client_secret: Optional[SecretStr] = None
    strava_client_id: str = ""
    strava_client_secret: Optional[SecretStr] = None
    oauth_redirect_base: str = "http://127.0.0.1:8000"
    integrations_encryption_key: Optional[SecretStr] = None

    # --- Authentication ----------------------------------------------------
    # The web app is deployed to Vercel and the API to a separate host, so the
    # session cookie is cross-site. SameSite=None is therefore mandatory (a Lax
    # cookie is simply not sent on the API's own XHR from another registrable
    # domain), and SameSite=None is only honoured alongside Secure.
    session_cookie_name: str = "fieldnote_session"
    session_cookie_domain: Optional[str] = None
    session_cookie_secure: bool = True
    session_ttl_hours: int = 24 * 14
    # Refresh last_seen_at at most this often: it is a liveness signal, not an
    # audit log, and writing it on every request turns each read into a write.
    session_touch_seconds: int = 60
    # The double-submit header. Named here so the web client and the tests read
    # the same string instead of hardcoding it in three places.
    csrf_header_name: str = "X-CSRF-Token"
    password_min_length: int = 12
    # Bound the work an unauthenticated caller can buy with one request. argon2
    # hashes the whole input (no bcrypt-style 72-byte truncation), so a megabyte
    # password would be a megabyte of hashing per attempt.
    password_max_length: int = 1024
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    auth_rate_limit_attempts: int = 20
    auth_rate_limit_window_seconds: int = 300
    email_sender: Literal["console", "smtp"] = "console"
    # Unlike session_cookie_name above, this one *is* a user-visible surface:
    # it is the From address a recipient reads in their mail client, so it
    # carries the product name and nothing depends on its old value.
    email_from: str = "no-reply@jasmine.local"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: Optional[SecretStr] = None
    smtp_starttls: bool = True
    # Login is a separate Google client from the Gmail data integration above.
    # Same vendor, different consent: the integration asks for mailbox scopes and
    # its refresh token is a data credential, while login asks only for identity.
    # Sharing one client would let a mailbox grant double as proof of who you
    # are, and would force every visitor to a login screen that requests Gmail.
    google_login_client_id: str = ""
    google_login_client_secret: Optional[SecretStr] = None
    # Where Google sends the browser back. Must match the console entry exactly.
    google_login_redirect_uri: str = ""
    # Where the API bounces the browser after a federated login completes.
    auth_post_login_redirect: str = ""
    # Development door: resolve a cookie-less request to the seeded dev identity.
    # This is the header-trust hole in a smaller box, so it is opt-in and the
    # validator below makes it unreachable outside development/test.
    dev_auto_login: bool = False
    # Local sign-in override. When set to an allowlisted handle (currently only
    # "lyn"), the login screen shows a one-click "Continue as …" button that
    # issues a real session without email/password. Refused outside
    # development/test the same way DEV_AUTO_LOGIN is.
    dev_user: str = ""

    # Anchored to the repo root for the same reason the sqlite and objects paths
    # are: a bare ".env" resolves against the working directory, and `make
    # migrate` runs alembic from apps/api where no .env exists. That was
    # survivable while the app had an offline fallback; once OPENAI_API_KEY
    # became mandatory it made `make migrate` fail outright.
    model_config = SettingsConfigDict(
        env_file=project_root() / ".env", extra="ignore"
    )

    @field_validator("database_url")
    @classmethod
    def _anchor_sqlite_path(cls, value: str) -> str:
        """Pin a relative sqlite file to the repo root.

        `uvicorn` runs from the repo root while `alembic` runs from apps/api, so
        a relative URL silently pointed each at its own database file — schema
        migrations landed on a phantom copy while the app kept an old schema.
        """
        prefix = "sqlite:///"
        if not value.startswith(prefix):
            return value
        path = value[len(prefix) :]
        if not path or path == ":memory:" or Path(path).is_absolute():
            return value
        return f"{prefix}{(project_root() / path).resolve()}"

    @field_validator("objects_dir", "sandbox_workdir")
    @classmethod
    def _anchor_objects_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (project_root() / value).resolve()

    @field_validator("scripted_model_script")
    @classmethod
    def _anchor_script_path(cls, value: Optional[Path]) -> Optional[Path]:
        if value is None or value.is_absolute():
            return value
        return (project_root() / value).resolve()

    @model_validator(mode="after")
    def _guard_model_provider(self) -> Settings:
        """Refuse to boot a provider that cannot actually answer.

        There is one product mode — OpenAI, with a key — and one test double.
        Both failures are fatal at startup rather than on the first chat turn:
        an app that boots and then 500s on every message is a worse lie than an
        app that says what is missing before it accepts traffic.

        The scripted model answers from a file on disk instead of a provider, so
        reaching it outside a development/test environment would mean the app is
        silently serving canned text.
        """
        if self.model_provider == "openai":
            if not self.has_openai_key:
                raise ValueError(
                    "MODEL_PROVIDER=openai requires OPENAI_API_KEY. Set it in "
                    ".env (see .env.example) — there is no offline mode."
                )
            return self
        if self.app_env not in {"development", "test"}:
            raise ValueError(
                "MODEL_PROVIDER=scripted requires APP_ENV to be development or test"
            )
        if self.scripted_model_script is None:
            raise ValueError(
                "MODEL_PROVIDER=scripted requires SCRIPTED_MODEL_SCRIPT"
            )
        return self

    @model_validator(mode="after")
    def _guard_budget(self) -> Settings:
        """Refuse a money ceiling this deployment has no way of measuring.

        `MODEL_PRICES` ships empty, so a deployment that sets only
        `BUDGET_USD_PER_WINDOW` has asked to be stopped at a dollar figure that
        every call rounds to zero — the ceiling would be configured, displayed,
        and inert. That is the failure this guard exists to make impossible:
        a limit that quietly does nothing is worse than no limit, because the
        operator stops watching.

        Three ways out, and the error names all three, because which one is
        right depends on facts only the operator has: price the models, add a
        token ceiling to bound the spend nobody can price, or drop the USD
        ceiling and stop believing there is one.

        Same structural gate as `_guard_model_provider` and `_guard_sandbox`,
        for the same reason — "we will remember" has already failed here once.
        """
        if self.budget_usd_per_window is None:
            return self
        if not self.model_prices and self.budget_tokens_per_window is None:
            raise ValueError(
                "BUDGET_USD_PER_WINDOW is set but MODEL_PRICES is empty, so "
                "every call records a null cost and the ceiling can never be "
                "reached. Configure MODEL_PRICES, or set "
                "BUDGET_TOKENS_PER_WINDOW to bound spend that has no price, or "
                "unset BUDGET_USD_PER_WINDOW."
            )
        return self

    @model_validator(mode="after")
    def _guard_sandbox(self) -> Settings:
        """Mirror _guard_model_provider for the execution provider.

        Same shape, same reason: a sandbox that cannot reach its provider should
        say so at startup, not on the turn where someone asks it to plot a CSV.
        The fake provider executes nothing, so reaching it in production would
        mean silently telling users their code ran when it did not.
        """
        if not self.sandbox_enabled:
            return self
        if self.sandbox_provider == "e2b":
            if self.sandbox_api_key is None:
                raise ValueError(
                    "SANDBOX_ENABLED=1 with SANDBOX_PROVIDER=e2b requires "
                    "SANDBOX_API_KEY. Get one at e2b.dev and set it in .env "
                    "(see .env.example), or leave SANDBOX_ENABLED unset to run "
                    "without server-side execution."
                )
            return self
        if self.sandbox_provider == "container":
            return self
        # `subprocess` runs generated code as this process's own user, with this
        # process's own filesystem access — .env, the database, ~/.aws. `fake`
        # executes nothing and would silently tell users their code ran. Neither
        # belongs in production, and neither is merely defaulted off: this is the
        # same structural gate that stops MODEL_PROVIDER=scripted and
        # DEV_AUTO_LOGIN, and it exists because "we will remember to set the
        # variable" has already failed once in this codebase.
        if self.app_env not in {"development", "test"}:
            raise ValueError(
                f"SANDBOX_PROVIDER={self.sandbox_provider} requires APP_ENV to be "
                "development or test — it provides no isolation. Deploy with "
                "SANDBOX_PROVIDER=container."
            )
        return self

    @model_validator(mode="after")
    def _guard_screen(self) -> Settings:
        """Refuse an incoherent prompt-injection screen at boot.

        Same structural gate as `_guard_sandbox`: a screen the deployment cannot
        actually run is worse than no screen, because an operator who configured
        one in enforce mode believes untrusted content is being gated when it is
        silently passing through. So a proxy backend with no reachable, allowed
        HTTPS URL fails at startup, not on the first turn that ingests a source.

        Only structural checks here — scheme, and host-on-allowlist as a string.
        No DNS: the runtime call does the full `validate_public_https_url` +
        `peer_is_blocked`, so a resolution that changes between boot and use is
        caught where it matters rather than trusted here.
        """
        if not self.screen_enabled:
            return self
        if self.screen_backend != "proxy":
            return self
        url = self.screen_proxy_url.strip()
        if not url:
            raise ValueError(
                "SCREEN_BACKEND=proxy requires SCREEN_PROXY_URL. Set the screen "
                "proxy endpoint, or use SCREEN_BACKEND=builtin."
            )
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ValueError("SCREEN_PROXY_URL must be an https URL")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or host not in self.allowed_tool_hosts:
            raise ValueError(
                "SCREEN_PROXY_URL host must be on TOOL_HOST_ALLOWLIST — the "
                "screen proxy is fetched through the same SSRF guard as every "
                "other tool destination."
            )
        return self

    @model_validator(mode="after")
    def _guard_auth(self) -> Settings:
        """Refuse the configurations that would hand out identities.

        Mirrors _guard_model_provider: the dangerous settings are not merely
        defaulted off, they cannot be switched on outside development/test. A
        misconfigured deploy fails at startup instead of serving every anonymous
        request as the seed user, or minting session cookies a network attacker
        can read off the wire.
        """
        if self.is_dev_env:
            if self.dev_user and self.dev_user.strip().lower() not in {"lyn"}:
                raise ValueError(
                    "DEV_USER must be empty or one of the allowlisted handles "
                    "(currently: lyn)"
                )
            return self
        if self.dev_auto_login:
            raise ValueError(
                "DEV_AUTO_LOGIN requires APP_ENV to be development or test"
            )
        if self.dev_user.strip():
            raise ValueError(
                "DEV_USER requires APP_ENV to be development or test"
            )
        if not self.session_cookie_secure:
            raise ValueError(
                "SESSION_COOKIE_SECURE=false requires APP_ENV to be development "
                "or test. Cross-site cookies need SameSite=None, which browsers "
                "only accept with Secure."
            )
        # The default, and the one dev setting whose failure is *silent*. The
        # console sender outside development logs "email suppressed" and returns,
        # while the route still answers with the deliberately-uninformative
        # acknowledgement that hides whether an address exists — so password
        # reset and email verification are broken and every observable signal
        # says they worked. Its siblings above fail at boot; so does this.
        if self.email_sender == "console":
            raise ValueError(
                "EMAIL_SENDER=console requires APP_ENV to be development or "
                "test — outside it, password reset and email verification mail "
                "is silently dropped. Deploy with EMAIL_SENDER=smtp."
            )
        if self.email_sender == "smtp" and not self.smtp_host.strip():
            raise ValueError("EMAIL_SENDER=smtp requires SMTP_HOST")
        return self

    @property
    def is_dev_env(self) -> bool:
        """True only for the two environments allowed to relax auth."""
        return self.app_env in {"development", "test"}

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(hours=self.session_ttl_hours)

    @property
    def lockout_duration(self) -> timedelta:
        return timedelta(minutes=self.login_lockout_minutes)

    @property
    def primary_web_origin(self) -> str:
        """The origin that email links and OAuth redirects point back at."""
        for origin in self.web_origin.split(","):
            trimmed = origin.strip().rstrip("/")
            if trimmed:
                return trimmed
        return "http://localhost:3000"

    @property
    def google_login_ready(self) -> bool:
        return bool(self.google_login_client_id and self.google_login_client_secret)

    @property
    def dev_override_handle(self) -> str:
        """Lowercased DEV_USER when the local override is allowed, else ''."""
        handle = self.dev_user.strip().lower()
        if not handle or not self.is_dev_env:
            return ""
        return handle

    @property
    def google_login_callback_url(self) -> str:
        if self.google_login_redirect_uri:
            return self.google_login_redirect_uri
        return f"{self.oauth_redirect_base.rstrip('/')}/api/auth/google/callback"

    @property
    def post_login_redirect_url(self) -> str:
        return self.auth_post_login_redirect or self.primary_web_origin

    @property
    def allowed_tool_hosts(self) -> set[str]:
        return {
            host.strip().lower()
            for host in self.tool_host_allowlist.split(",")
            if host.strip()
        }

    @property
    def sandbox_allowed_hosts(self) -> List[str]:
        """Egress allowlist for SANDBOX_NETWORK_POLICY=allowlist.

        A list rather than a set: the provider takes an ordered rule list, and a
        set would make the sandbox's configuration differ run to run for no
        reason, which is miserable to debug.
        """
        seen: List[str] = []
        for host in self.sandbox_host_allowlist.split(","):
            cleaned = host.strip().lower()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @property
    def mcp_stdio_allowed(self) -> bool:
        """Whether stdio MCP servers may be registered and spawned.

        A stdio server is an arbitrary command this API launches as its own
        user, with its own filesystem and .env — the same isolation-free
        execution _guard_sandbox refuses for SANDBOX_PROVIDER=subprocess, and
        letting any workspace member name that command is remote code execution
        on the API host. An http server, by contrast, is reached over the
        network behind OAuth. So stdio is confined to development/test and
        production registers http servers. This is a property rather than a
        settable flag on purpose: there is no environment variable that makes
        arbitrary-command execution safe to expose to a workspace member.
        """
        return self.is_dev_env

    @property
    def sandbox_ready(self) -> bool:
        if not self.sandbox_enabled:
            return False
        # Only e2b needs a credential. subprocess and container are local, and
        # fake needs nothing at all.
        if self.sandbox_provider == "e2b":
            return self.sandbox_api_key is not None
        return True

    @property
    def allowed_web_origins(self) -> List[str]:
        origins = {
            origin.strip().rstrip("/")
            for origin in self.web_origin.split(",")
            if origin.strip()
        }
        for origin in list(origins):
            if "://localhost:" in origin:
                origins.add(origin.replace("://localhost:", "://127.0.0.1:"))
            elif "://127.0.0.1:" in origin:
                origins.add(origin.replace("://127.0.0.1:", "://localhost:"))
        return sorted(origins)

    @property
    def has_openai_key(self) -> bool:
        return bool(
            self.openai_api_key
            and self.openai_api_key.get_secret_value().strip()
        )

    @property
    def integrations_ready(self) -> bool:
        return bool(
            self.integrations_encryption_key
            and self.integrations_encryption_key.get_secret_value().strip()
        )

    def provider_configured(self, provider: str) -> bool:
        if not self.integrations_ready:
            return False
        if provider == "google":
            return bool(self.google_client_id and self.google_client_secret)
        if provider == "strava":
            return bool(self.strava_client_id and self.strava_client_secret)
        if provider == "garmin":
            # Credential-based (unofficial API); only token encryption is needed.
            return True
        return False

    def price_for(self, model: str) -> Optional[ModelPrice]:
        """The configured rate for a model, or None when there isn't one.

        None means "record the tokens, leave the cost null". It must never become
        a zero or a neighbouring model's rate: an invented price is worse than a
        missing one because nothing downstream can tell it apart from a measured
        one.
        """
        return self.model_prices.get(model.strip())

    @property
    def selectable_models(self) -> List[str]:
        """The models a user may pick per turn — a deployment-controlled allow-list.

        A per-turn override must never let a user hand an arbitrary model string
        to the provider: an unpriced model would record a null cost, and a typo'd
        one would 500 the turn. So the choice is bounded to models the deployment
        has already vouched for.

        `SELECTABLE_MODELS` (comma-separated) is the explicit list when set;
        otherwise it derives from the priced models, because a price is exactly
        the deployment saying "I have configured this model". `openai_model` is
        always included so the current default can never become unselectable, and
        the order is stable so the client dropdown does not reshuffle run to run.
        """
        if self.active_model_provider == "scripted":
            # The scripted double ignores the model, so the picker and the
            # allow-list agree on the one placeholder name bootstrap advertises —
            # otherwise a dev picking "scripted-double" is refused by a check that
            # was comparing against a real model name it never offered.
            return ["scripted-double"]
        if self.selectable_models_raw.strip():
            names = [
                name.strip()
                for name in self.selectable_models_raw.split(",")
                if name.strip()
            ]
        else:
            names = sorted(self.model_prices)
        ordered: List[str] = []
        for name in [*names, self.openai_model]:
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    @property
    def active_model_provider(self) -> Literal["openai", "scripted"]:
        """Which model is behind this process. Validated at startup, so it never
        names a provider that is missing what it needs to run."""
        return self.model_provider


@lru_cache
def get_settings() -> Settings:
    return Settings()
