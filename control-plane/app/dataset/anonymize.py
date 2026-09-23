"""Anonymization for the incident dataset.

Two separate goals, and they need different treatment:

  * Privacy — no student name, email, repo path, IP or hostname leaves the
    incubator in a training file.
  * Utility — the *shape* of the error must survive. Replacing every identifier
    with "X" produces a dataset that teaches nothing. So identifiers are mapped
    consistently within a record (team-a always becomes project_1 in that
    record) and the technical structure of the message is preserved exactly.

Secrets are redacted irreversibly and never mapped, because a consistent
pseudonym for a live API key is still a leak of its structure.
"""

from __future__ import annotations

import hashlib
import re

EMAIL = re.compile(r"\b[\w\.\-\+]+@[\w\.\-]+\.\w{2,}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
URL_HOST = re.compile(r"https?://([\w\.\-]+)(?::\d+)?")
HOME_PATH = re.compile(r"/(?:home|Users)/([\w\.\-]+)")
GIT_REPO = re.compile(r"\b([\w\.\-]+)/([\w\.\-]+?)(?:\.git)?\b(?=\s|$|['\"])")
LONG_HEX = re.compile(r"\b[0-9a-f]{32,}\b", re.I)

SECRET_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"(?<=://)[^\s:/@]+:[^\s:@]+(?=@)"),  # creds in a URL
    re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----"),
]

# Words that must never be pseudonymized — they carry the technical meaning.
KEEP = {
    "localhost", "127", "0", "docker", "nginx", "postgres", "postgresql", "mysql",
    "redis", "mongodb", "node", "python", "app", "main", "src", "api", "web",
    "backend", "frontend", "server", "client", "test", "tests", "build", "dist",
    "usr", "var", "etc", "opt", "lib", "bin", "tmp", "root", "github", "actions",
    "requirements", "package", "index", "config", "settings", "models", "utils",
}


class Anonymizer:
    """Consistent within one record, inconsistent across records by design."""

    def __init__(self, salt: str = ""):
        self.salt = salt
        self._map: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def _alias(self, kind: str, value: str) -> str:
        key = f"{kind}:{value.lower()}"
        if key not in self._map:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            self._map[key] = f"{kind}_{self._counters[kind]}"
        return self._map[key]

    def scrub(self, text: str) -> str:
        if not text:
            return ""
        out = text

        # 1. Secrets first — destroyed, never mapped.
        for pat in SECRET_PATTERNS:
            out = pat.sub("[REDACTED_SECRET]", out)

        # 2. Direct identifiers.
        # Key on the local part, so rakesh@vnrvjiet.in and /home/rakesh resolve
        # to the same alias — otherwise one person appears as two in a record
        # and the dataset loses the link between an error and who hit it.
        out = EMAIL.sub(lambda m: f"[{self._alias('user', m.group(0).split('@')[0])}]@example.edu", out)
        out = IPV4.sub(lambda m: m.group(0) if m.group(0).startswith(("127.", "0.0.0.0")) else "[IP]", out)
        out = HOME_PATH.sub(lambda m: f"/home/{self._alias('user', m.group(1))}", out)
        out = URL_HOST.sub(self._host_repl, out)
        out = LONG_HEX.sub("[HASH]", out)
        return out

    def _host_repl(self, m: re.Match) -> str:
        host = m.group(1)
        if host in ("localhost", "127.0.0.1") or host.startswith("127."):
            return m.group(0)
        if host.endswith(("github.com", "githubusercontent.com", "docker.io", "pypi.org", "npmjs.org", "npmjs.com")):
            return m.group(0)
        return m.group(0).replace(host, f"{self._alias('host', host)}.example.edu")

    def scrub_repo(self, repo: str) -> str:
        if not repo or "/" not in repo:
            return self._alias("project", repo or "unknown")
        org, name = repo.split("/", 1)
        return f"{self._alias('org', org)}/{self._alias('project', name)}"

    def scrub_slug(self, slug: str) -> str:
        return self._alias("project", slug or "unknown")


def stable_id(*parts: str) -> str:
    return hashlib.blake2b("|".join(parts).encode(), digest_size=8).hexdigest()
