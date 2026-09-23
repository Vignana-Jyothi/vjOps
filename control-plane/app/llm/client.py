"""Self-hosted LLM access.

Everything goes through an OpenAI-compatible /v1 endpoint, which means the same
code drives Ollama (dev / small GPU) and vLLM (production GPU node). No traffic
ever leaves the campus network.

Design rule for this system: the LLM is an *enricher*, never a hard dependency.
Every caller must degrade gracefully when inference is unavailable, because a
DevOps tool that stops working when the GPU box reboots is worse than useless.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        self.timeout = timeout or settings.llm_timeout_s

    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{self.base_url}/models", headers=self._headers())
                r.raise_for_status()
                data = r.json()
            models = [m.get("id") for m in data.get("data", [])]
            return {
                "available": True,
                "backend": self.base_url,
                "configured_model": self.model,
                "models": models,
                "model_loaded": any(self.model.split(":")[0] in (m or "") for m in models),
            }
        except Exception as exc:
            return {
                "available": False,
                "backend": self.base_url,
                "configured_model": self.model,
                "error": str(exc),
            }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as exc:
            # Some servers reject response_format; retry once without it.
            if json_mode and exc.response.status_code in (400, 422):
                return self.complete(
                    system,
                    user + "\n\nRespond with a single valid JSON object and nothing else.",
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=False,
                )
            raise LLMUnavailable(f"LLM HTTP {exc.response.status_code}: {exc.response.text[:300]}")
        except Exception as exc:
            raise LLMUnavailable(f"LLM unreachable at {self.base_url}: {exc}") from exc

        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMUnavailable(f"Malformed LLM response: {data}") from exc

    # ------------------------------------------------------------------ #
    def complete_json(
        self,
        system: str,
        user: str,
        *,
        fallback: dict | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        """JSON completion that never raises — returns `fallback` on any failure.

        Small self-hosted models are chatty and often wrap JSON in prose or
        fences, so we extract rather than trust.
        """
        try:
            raw = self.complete(
                system,
                user,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
            )
        except LLMUnavailable as exc:
            log.warning("LLM unavailable, using fallback: %s", exc)
            return dict(fallback or {}, _llm_error=str(exc), _llm_used=False)

        parsed = extract_json(raw)
        if parsed is None:
            log.warning("Could not parse JSON from model output: %.200s", raw)
            return dict(fallback or {}, _llm_error="unparseable", _llm_used=False)
        parsed["_llm_used"] = True
        parsed["_llm_model"] = self.model
        return parsed


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction from a chatty completion."""
    if not text:
        return None
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)

    for cand in candidates:
        cand = cand.strip()
        try:
            val = json.loads(cand)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass
        # Balanced-brace scan for the first complete object.
        start = cand.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            val = json.loads(cand[start : i + 1])
                            if isinstance(val, dict):
                                return val
                        except json.JSONDecodeError:
                            break
            start = cand.find("{", start + 1)
    return None


llm = LLMClient()
