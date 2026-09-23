from __future__ import annotations

import logging
import time

import httpx

from .config import AGENT_VERSION, config

log = logging.getLogger("viljaops.transport")


class ControlPlane:
    def __init__(self):
        self.base = config.control_plane
        self.client = httpx.Client(
            timeout=45,
            headers={
                "X-Agent-Token": config.token,
                "User-Agent": f"viljaops-agent/{AGENT_VERSION}",
            },
        )
        self._backoff = 1

    def _post(self, path: str, payload: dict) -> dict | None:
        try:
            r = self.client.post(f"{self.base}{path}", json=payload)
            if r.status_code == 401:
                log.error("Control plane rejected the agent token. Re-enroll this server.")
                return None
            r.raise_for_status()
            self._backoff = 1
            return r.json()
        except Exception as exc:
            log.warning("POST %s failed: %s (retrying, backoff %ss)", path, exc, self._backoff)
            time.sleep(min(self._backoff, 60))
            self._backoff = min(self._backoff * 2, 60)
            return None

    def _get(self, path: str) -> dict | list | None:
        try:
            r = self.client.get(f"{self.base}{path}")
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("GET %s failed: %s", path, exc)
            return None

    # ------------------------------------------------------------------ #
    def heartbeat(self, payload: dict) -> dict | None:
        return self._post("/api/agents/heartbeat", payload)

    def poll_commands(self) -> list[dict]:
        data = self._get("/api/agents/commands?limit=5")
        return data if isinstance(data, list) else []

    def submit_result(self, command_id: str, ok: bool, output: str = "", error: str = "", detail: dict | None = None):
        return self._post(
            "/api/agents/commands/result",
            {"command_id": command_id, "ok": ok, "output": output[-8000:], "error": error[-4000:], "detail": detail or {}},
        )

    def ship_logs(self, source: str, text: str, *, deployment_id: str | None = None, project_slug: str | None = None, container: str = "", stream: str = "stdout"):
        if not text.strip():
            return None
        return self._post(
            "/api/deployments/logs",
            {
                "deployment_id": deployment_id,
                "project_slug": project_slug,
                "source": source,
                "text": text[:400_000],
                "container": container,
                "stream": stream,
            },
        )
