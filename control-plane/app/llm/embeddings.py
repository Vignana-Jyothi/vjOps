from __future__ import annotations

import hashlib
import logging
import math
import struct

import httpx

from ..config import settings

log = logging.getLogger(__name__)


def embed(text: str) -> list[float] | None:
    """Embed with the self-hosted embedding model.

    Falls back to a deterministic hashed bag-of-words vector so retrieval still
    works (worse, but works) when the embedding model isn't pulled yet.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{settings.embed_base_url.rstrip('/')}/api/embeddings",
                json={"model": settings.embed_model, "prompt": text[:8000]},
            )
            if r.status_code == 404:
                # vLLM / OpenAI-compatible embedding route
                r = c.post(
                    f"{settings.embed_base_url.rstrip('/')}/v1/embeddings",
                    json={"model": settings.embed_model, "input": text[:8000]},
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                )
                r.raise_for_status()
                return r.json()["data"][0]["embedding"]
            r.raise_for_status()
            return r.json()["embedding"]
    except Exception as exc:
        log.warning("Embedding backend unavailable (%s); using hashed fallback", exc)
        return _hashed_embedding(text, settings.embed_dim)


def _hashed_embedding(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for token in _tokenize(text):
        h = hashlib.blake2b(token.encode(), digest_size=8).digest()
        idx = struct.unpack("<Q", h)[0] % dim
        sign = 1.0 if (h[0] & 1) else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _tokenize(text: str) -> list[str]:
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum() or ch in "_.-":
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
