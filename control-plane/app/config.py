from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "ViljaOps"
    # "development" or "production". Nothing about behavior changes on this
    # by itself — it only controls the startup safety check below and the
    # CORS/webhook defaults, which are the things that are fine to leave
    # loose for a laptop demo and genuinely dangerous to leave loose once
    # this is reachable from the internet.
    environment: str = "development"
    database_url: str = "postgresql+psycopg://viljaops:viljaops@localhost:5432/viljaops"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 60 * 12

    # Comma-separated list of origins the dashboard is served from, e.g.
    # "https://ops.vnrvjiet.in". Empty means "allow any origin" — fine for
    # local development against a throwaway database, refused at startup in
    # production (see check_production_safety below).
    cors_allowed_origins: str = ""

    # Self-hosted inference (Ollama / vLLM — both OpenAI-compatible)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen2.5-coder:14b"
    llm_api_key: str = "ollama"
    llm_timeout_s: int = 180
    llm_max_tokens: int = 2048

    embed_base_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    github_token: str = ""
    github_webhook_secret: str = ""
    github_org: str = ""
    github_api: str = "https://api.github.com"

    repo_cache_dir: str = "/var/lib/viljaops/repos"
    clone_timeout_s: int = 180
    max_repo_mb: int = 500

    # Safety switch. When false, no fix action is ever executed without an
    # explicit approval record written by a human user.
    auto_remediation: bool = False

    # Port allocation policy for student deployments
    port_range_start: int = 3000
    port_range_end: int = 3999

    # Verification Agent: how long to watch metrics/anomalies after a fix
    # executes before deciding its `verify` criterion held. Short grace for
    # most actions; memory fixes get longer because OOM kills the ActionSpec
    # itself estimates at ~15 min.
    verify_grace_minutes: int = 3
    verify_grace_minutes_memory: int = 15
    # If the window elapses with zero telemetry (agent not reporting), give
    # it one extension before giving up and marking the fix "unknown".
    verify_max_extensions: int = 1

    bootstrap_admin_email: str = "admin@vnrvjiet.in"
    bootstrap_admin_password: str = "changeme123"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Every value here is a documented default from this file, docker-compose.yml,
# or .env.example — every one of them is public, since this repository is.
# Fine to leave in place for a laptop demo against a throwaway database;
# dangerous to leave in place for anything with a real domain pointed at it.
_KNOWN_DEFAULT_SECRETS = {
    "jwt_secret": "change-me-in-production",
    "bootstrap_admin_password": "changeme123",
}


def check_production_safety(s: Settings = settings) -> list[str]:
    """Returns the list of problems, empty if none. `environment=development`
    (the default) skips this entirely — it exists so that flipping one
    setting to go live doesn't also silently carry over every convenience
    default from local development."""
    if s.environment != "production":
        return []
    problems = []
    for field, default in _KNOWN_DEFAULT_SECRETS.items():
        if getattr(s, field) == default:
            problems.append(f"{field} is still its documented default value")
    if "://viljaops:viljaops@" in s.database_url:
        problems.append("database_url is still using the documented default Postgres password")
    if not s.github_webhook_secret:
        problems.append(
            "github_webhook_secret is unset — the webhook endpoint would accept unsigned, "
            "forged GitHub events from anyone who finds the URL"
        )
    if not s.cors_allowed_origins:
        problems.append(
            "cors_allowed_origins is unset — the dashboard API would accept credentialed "
            "requests from any origin. Set it to your actual frontend origin(s)."
        )
    return problems
