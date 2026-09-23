from __future__ import annotations

import os
from dataclasses import dataclass, field

AGENT_VERSION = "1.0.0"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    control_plane: str = os.environ.get("VILJAOPS_URL", "http://localhost:8000").rstrip("/")
    token: str = os.environ.get("VILJAOPS_AGENT_TOKEN", "")
    server_name: str = os.environ.get("VILJAOPS_SERVER_NAME", os.uname().nodename)

    heartbeat_interval: int = _int("VILJAOPS_HEARTBEAT_S", 30)
    command_poll_interval: int = _int("VILJAOPS_POLL_S", 15)
    log_ship_interval: int = _int("VILJAOPS_LOG_SHIP_S", 60)

    nginx_sites_available: str = os.environ.get("NGINX_SITES_AVAILABLE", "/etc/nginx/sites-available")
    nginx_sites_enabled: str = os.environ.get("NGINX_SITES_ENABLED", "/etc/nginx/sites-enabled")
    nginx_log_dir: str = os.environ.get("NGINX_LOG_DIR", "/var/log/nginx")
    nginx_bin: str = os.environ.get("NGINX_BIN", "nginx")

    docker_bin: str = os.environ.get("DOCKER_BIN", "docker")
    env_dir: str = os.environ.get("VILJAOPS_ENV_DIR", "/etc/viljaops/env")
    state_dir: str = os.environ.get("VILJAOPS_STATE_DIR", "/var/lib/viljaops-agent")

    # Containers the agent manages. Anything not matching is observed but never
    # touched, so the agent can run on a box with unrelated workloads.
    managed_label: str = os.environ.get("VILJAOPS_LABEL", "viljaops.managed")
    manage_all: bool = os.environ.get("VILJAOPS_MANAGE_ALL", "true").lower() == "true"

    dry_run: bool = os.environ.get("VILJAOPS_DRY_RUN", "false").lower() == "true"
    log_tail_lines: int = _int("VILJAOPS_LOG_TAIL", 400)

    exclude_containers: list[str] = field(
        default_factory=lambda: [
            c.strip()
            for c in os.environ.get(
                "VILJAOPS_EXCLUDE",
                "viljaops-control-plane,viljaops-worker,viljaops-postgres,viljaops-redis,viljaops-ollama",
            ).split(",")
            if c.strip()
        ]
    )

    def validate(self) -> list[str]:
        problems = []
        if not self.token:
            problems.append("VILJAOPS_AGENT_TOKEN is not set — enroll this server in the dashboard first")
        if not self.control_plane.startswith(("http://", "https://")):
            problems.append("VILJAOPS_URL must be a full http(s) URL")
        return problems


config = Config()
