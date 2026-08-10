from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    openai_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] = "low"
    openai_timeout_seconds: float = 60.0
    openai_max_output_tokens: int = 1200
    openai_embedding_model: str = "text-embedding-3-small"
    openai_codegen_max_output_tokens: int = 16000

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
    email_from: str = "no-reply@fieldnote.local"
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

    @field_validator("objects_dir")
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

    @property
    def active_model_provider(self) -> Literal["openai", "scripted"]:
        """Which model is behind this process. Validated at startup, so it never
        names a provider that is missing what it needs to run."""
        return self.model_provider


@lru_cache
def get_settings() -> Settings:
    return Settings()
