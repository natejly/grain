from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def project_root() -> Path:
    """Repo root, so relative paths mean the same thing from any working dir."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "Makefile").is_file() and (candidate / "apps").is_dir():
            return candidate
    return Path.cwd()


class Settings(BaseSettings):
    app_env: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://localhost:3000"
    database_url: str = "sqlite:///./data/workspace.db"
    objects_dir: Path = Path("./data/objects")
    tool_host_allowlist: str = "api.github.com"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_tool_response_bytes: int = 256 * 1024
    model_provider: Literal["auto", "deterministic", "openai"] = "auto"
    openai_api_key: Optional[SecretStr] = None
    openai_model: str = "gpt-5.5"
    openai_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] = "low"
    openai_timeout_seconds: float = 60.0
    openai_max_output_tokens: int = 1200
    openai_embedding_model: str = "text-embedding-3-small"
    openai_codegen_max_output_tokens: int = 16000
    memory_enabled: bool = True
    memory_max_items_per_run: int = 5
    memory_recall_limit: int = 6
    memory_transcript_messages: int = 10
    run_lease_seconds: int = 90
    google_client_id: str = ""
    google_client_secret: Optional[SecretStr] = None
    strava_client_id: str = ""
    strava_client_secret: Optional[SecretStr] = None
    oauth_redirect_base: str = "http://127.0.0.1:8000"
    integrations_encryption_key: Optional[SecretStr] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    def active_model_provider(self) -> Literal["deterministic", "openai"]:
        if self.model_provider == "openai":
            return "openai"
        if self.model_provider == "auto" and self.has_openai_key:
            return "openai"
        return "deterministic"


@lru_cache
def get_settings() -> Settings:
    return Settings()
