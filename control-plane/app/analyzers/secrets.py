"""Secret scanning tuned for student repositories.

Generic scanners drown reviewers in false positives from example files and test
fixtures. This one weights by *where* the match is and whether the value looks
like a real credential rather than a placeholder, because a report nobody reads
prevents nothing.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .detectors import read_text, rel, walk_files

PLACEHOLDER = re.compile(
    r"^(?:your|my|the)?[-_ ]?(?:x{3,}|changeme|placeholder|example|sample|dummy|test|fake|secret|password|token|key|value|none|null|todo|<[^>]+>|\$\{[^}]+\}|\*{3,}|\.{3,})",
    re.I,
)

RULES: list[tuple[str, str, re.Pattern, str]] = [
    ("aws_access_key", "critical", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key ID"),
    ("aws_secret_key", "critical", re.compile(r"aws_secret_access_key\s*[=:]\s*[\"']?([A-Za-z0-9/+=]{40})", re.I), "AWS secret access key"),
    ("github_pat", "critical", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"), "GitHub personal access token"),
    ("openai_key", "critical", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b"), "OpenAI API key"),
    ("anthropic_key", "critical", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "Anthropic API key"),
    ("google_api_key", "high", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
    ("slack_token", "critical", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b"), "Slack token"),
    ("stripe_key", "critical", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b"), "Stripe live secret key"),
    ("private_key", "critical", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "Private key file"),
    ("jwt_secret", "high", re.compile(r"(?:jwt[_-]?secret|secret[_-]?key)\s*[=:]\s*[\"']([^\"'\s]{8,})[\"']", re.I), "JWT/session signing secret"),
    ("db_url_with_password", "critical", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@\"']+:(?P<pw>[^\s:@\"']{3,})@[\w\.\-]+", re.I), "Database URL containing a password"),
    ("firebase_key", "high", re.compile(r"\"private_key_id\"\s*:\s*\"[a-f0-9]{40}\""), "Firebase service account"),
    ("generic_password", "medium", re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*[\"']([^\"'\s]{6,})[\"']", re.I), "Hardcoded password"),
    ("generic_api_key", "medium", re.compile(r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token)\s*[=:]\s*[\"']([A-Za-z0-9_\-]{16,})[\"']", re.I), "Hardcoded API key"),
    ("twilio", "high", re.compile(r"\bAC[a-f0-9]{32}\b"), "Twilio account SID"),
    ("sendgrid", "critical", re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"), "SendGrid API key"),
]

# Files where a "secret" is expected and harmless.
SAFE_FILES = re.compile(r"(?:\.env\.(?:example|sample|template)|env\.example|README|\.md$|\.lock$|test[s]?/|__tests__/|fixtures?/|mock)", re.I)
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".woff", ".woff2", ".ttf", ".ico", ".svg", ".mp4", ".map"}


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def looks_real(value: str) -> bool:
    if not value or len(value) < 6:
        return False
    if PLACEHOLDER.match(value.strip()):
        return False
    if value.strip("*.<>${} ") == "":
        return False
    if re.fullmatch(r"(.)\1{5,}", value):
        return False
    return shannon_entropy(value) >= 3.0 or len(value) >= 32


def scan(root: Path, *, max_findings: int = 60) -> list[dict]:
    findings: list[dict] = []
    seen: set[tuple] = set()

    for p in walk_files(root):
        if p.suffix.lower() in SKIP_EXT:
            continue
        r = rel(root, p)
        is_safe_context = bool(SAFE_FILES.search(r))
        text = read_text(p)
        if not text:
            continue

        for rule_id, severity, pattern, label in RULES:
            for m in pattern.finditer(text):
                captured = ""
                if m.groupdict().get("pw"):
                    captured = m.group("pw")
                elif m.groups():
                    captured = next((g for g in m.groups() if g), "")
                probe = captured or m.group(0)

                if rule_id in ("generic_password", "generic_api_key", "jwt_secret", "db_url_with_password") and not looks_real(probe):
                    continue

                line_no = text.count("\n", 0, m.start()) + 1
                key = (rule_id, r, line_no)
                if key in seen:
                    continue
                seen.add(key)

                sev = severity
                if is_safe_context:
                    sev = "low"

                findings.append(
                    {
                        "rule": rule_id,
                        "label": label,
                        "severity": sev,
                        "file": r,
                        "line": line_no,
                        "preview": _mask(text.splitlines()[line_no - 1][:180] if line_no <= len(text.splitlines()) else ""),
                        "in_example_file": is_safe_context,
                        "entropy": round(shannon_entropy(probe), 2),
                    }
                )
                if len(findings) >= max_findings:
                    return _rank(findings)
    return _rank(findings)


def _mask(line: str) -> str:
    """Never echo a live credential back into the UI, database or logs."""

    def repl(m: re.Match) -> str:
        v = m.group(0)
        return v[:4] + "*" * max(4, len(v) - 8) + v[-4:] if len(v) > 12 else "*" * len(v)

    masked = line
    for _, _, pattern, _ in RULES:
        masked = pattern.sub(repl, masked)
    masked = re.sub(r"([\"'])([A-Za-z0-9_\-/+=]{16,})\1", lambda m: f"{m.group(1)}{m.group(2)[:3]}{'*' * 8}{m.group(1)}", masked)
    return masked


def _rank(findings: list[dict]) -> list[dict]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(findings, key=lambda f: (order.get(f["severity"], 4), f["file"]))


def check_git_history(root: Path) -> dict:
    """Whether a committed .env is in history — deleting the file is not enough."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "log", "--all", "--pretty=format:", "--name-only", "--diff-filter=A"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        files = {l.strip() for l in out.stdout.splitlines() if l.strip()}
        leaked = sorted(f for f in files if Path(f).name in {".env", ".env.local", ".env.production", "credentials.json", "serviceAccount.json", "id_rsa"})
        return {"checked": True, "ever_committed_secrets": leaked}
    except Exception:
        return {"checked": False, "ever_committed_secrets": []}
