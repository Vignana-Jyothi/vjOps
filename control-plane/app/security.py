from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:
        return False


def create_access_token(sub: str, role: str, extra: dict | None = None) -> str:
    payload = {
        "sub": sub,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    payload.update(extra or {})
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


# --------------------------------------------------------------------------- #
# Agent enrollment tokens
# --------------------------------------------------------------------------- #
def new_agent_token() -> str:
    return "vops_" + secrets.token_urlsafe(32)


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_agent_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_agent_token(token), token_hash or "")


# --------------------------------------------------------------------------- #
# GitHub webhook signature
# --------------------------------------------------------------------------- #
def verify_github_signature(body: bytes, header: str | None) -> bool:
    secret = settings.github_webhook_secret
    if not secret:
        # `check_production_safety` already refuses to start the app in
        # production without this set — this branch only exists so that a
        # webhook call arriving during local development (no secret
        # configured, ENVIRONMENT unset/development) isn't rejected. In
        # production this is unreachable by construction, but fail closed
        # here too rather than relying solely on the startup check.
        return settings.environment != "production"
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
