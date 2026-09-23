"""Turn raw log text from five very different sources into one normalized shape.

Correlation is only possible once everything has a timestamp, a level and a
source. Each parser is deliberately forgiving: student apps print whatever they
like, and a parser that throws is worse than one that guesses.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")

LEVEL_HINTS = (
    ("critical", ("critical", "fatal", "emerg", "panic", "oomkilled", "out of memory", "segmentation fault")),
    ("error", ("error", "err]", "[err", "exception", "traceback", "failed", "failure", "denied", "refused", "cannot", "unable", "not found", "no such")),
    ("warning", ("warn", "deprecat", "retry", "slow", "restarting")),
)

# A container dying is an error even when the line contains no error word.
# Without this, "team-alpha exited with code 137" is classified 'info' and the
# correlation layer concludes the container never started — the opposite of
# what happened.
_NONZERO_EXIT = re.compile(
    r"\bexit(?:ed)?(?:\s+with)?\s+(?:code\s+)?([1-9]\d*)\b"
    r"|\b(?:died|exited)\s*\(\s*([1-9]\d*)\s*\)",
    re.I,
)
_KILLED = re.compile(r"\b(?:killed process|oom-?kill|back-?off restarting)\b", re.I)


def guess_level(line: str) -> str:
    low = line.lower()
    if _NONZERO_EXIT.search(low) or _KILLED.search(low):
        return "error"
    for level, needles in LEVEL_HINTS:
        if any(n in low for n in needles):
            return level
    return "info"


def _parse_ts(raw: str | None, default: datetime | None = None) -> datetime:
    if raw:
        cleaned = raw.strip().replace(" ", "T", 1)
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(cleaned)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return default or datetime.now(timezone.utc)


def _base(source: str, line: str, ts: datetime, **meta) -> dict:
    return {
        "source": source,
        "ts": ts,
        "level": guess_level(line),
        "message": line.rstrip()[:8000],
        "meta": meta,
    }


# --------------------------------------------------------------------------- #
# GitHub Actions
# --------------------------------------------------------------------------- #
_ACTIONS_LINE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s(?P<rest>.*)$")
_ACTIONS_CMD = re.compile(r"^##\[(?P<cmd>\w+)\](?P<rest>.*)$")
_GROUP = re.compile(r"^##\[(?:group|endgroup)\]")


def parse_actions_log(text: str, *, job: str = "", step: str = "") -> list[dict]:
    """GitHub Actions raw logs: 'ISO8601Z <text>', with ##[error] annotations."""
    events: list[dict] = []
    current_group = ""
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _ACTIONS_LINE.match(raw)
        ts = _parse_ts(m.group("ts")) if m else datetime.now(timezone.utc)
        body = m.group("rest") if m else raw

        if body.startswith("##[group]"):
            current_group = body[len("##[group]") :].strip()
            continue
        if _GROUP.match(body):
            continue

        level = None
        cmd = _ACTIONS_CMD.match(body)
        if cmd:
            kind = cmd.group("cmd").lower()
            body = cmd.group("rest")
            level = {"error": "error", "warning": "warning", "notice": "info"}.get(kind)

        ev = _base("actions", body, ts, job=job, step=step, group=current_group)
        if level:
            ev["level"] = level
        events.append(ev)
    return events


def extract_actions_failure_window(events: list[dict], *, before: int = 60, after: int = 10) -> list[dict]:
    """Actions logs run to tens of thousands of lines. Keep the useful slice.

    Anchors on the last error and takes a window around it, plus every error
    line in the whole log so nothing critical is dropped.
    """
    if not events:
        return []
    error_idx = [i for i, e in enumerate(events) if e["level"] in ("error", "critical")]
    if not error_idx:
        return events[-120:]
    anchor = error_idx[-1]
    lo, hi = max(0, anchor - before), min(len(events), anchor + after + 1)
    window = list(range(lo, hi))
    keep = sorted(set(window) | set(error_idx))
    return [events[i] for i in keep]


# --------------------------------------------------------------------------- #
# Docker
# --------------------------------------------------------------------------- #
def parse_docker_log(text: str, *, container: str = "", stream: str = "stdout") -> list[dict]:
    """`docker logs -t` output: RFC3339Nano prefix, else bare application output."""
    events: list[dict] = []
    last_ts = datetime.now(timezone.utc)
    buffer: list[str] = []

    def flush(ts: datetime):
        if buffer:
            ev = _base("docker", "\n".join(buffer), ts, container=container)
            ev["stream"] = stream
            events.append(ev)
            buffer.clear()

    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = ISO_RE.match(raw.strip())
        if m:
            flush(last_ts)
            last_ts = _parse_ts(m.group(1))
            body = raw.strip()[m.end(1) :].strip()
            buffer.append(body)
        elif raw.startswith((" ", "\t")) or raw.lstrip().startswith(("File \"", "at ", "Caused by")):
            # continuation of a stack trace — keep it attached
            buffer.append(raw)
        else:
            flush(last_ts)
            buffer.append(raw)
    flush(last_ts)

    for ev in events:
        ev["stream"] = stream
    return events


_DOCKER_EVENT = re.compile(
    r"^(?P<ts>\S+)\s+container\s+(?P<action>\w+)\s+(?P<id>\w+)\s*(?P<attrs>.*)$"
)


def parse_docker_events(text: str) -> list[dict]:
    """`docker events --format ...` — start/die/oom/health_status transitions."""
    events = []
    for raw in text.splitlines():
        m = _DOCKER_EVENT.match(raw.strip())
        if not m:
            continue
        action = m.group("action")
        ev = _base("docker", f"container {action} {m.group('attrs')}", _parse_ts(m.group("ts")), event=action)
        if action in ("die", "oom", "kill"):
            ev["level"] = "error"
        events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# Nginx
# --------------------------------------------------------------------------- #
_NGINX_ERROR = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>\w+)\] (?P<rest>.*)$"
)
_NGINX_ACCESS = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+)[^"]*" '
    r'(?P<status>\d{3}) (?P<bytes>\d+|-)(?: "(?P<ref>[^"]*)" "(?P<ua>[^"]*)")?'
    r'(?:\s+rt=(?P<rt>[\d\.]+))?'
)
_NGINX_LEVELS = {"emerg": "critical", "alert": "critical", "crit": "critical", "error": "error", "warn": "warning"}


def parse_nginx_error_log(text: str) -> list[dict]:
    events = []
    for raw in text.splitlines():
        m = _NGINX_ERROR.match(raw.strip())
        if not m:
            if raw.strip():
                events.append(_base("nginx", raw, datetime.now(timezone.utc), log="error"))
            continue
        ts = _parse_ts(m.group("ts").replace("/", "-"))
        ev = _base("nginx", m.group("rest"), ts, log="error")
        ev["level"] = _NGINX_LEVELS.get(m.group("level"), "info")
        events.append(ev)
    return events


def parse_nginx_access_log(text: str, *, only_errors: bool = True) -> list[dict]:
    """Access logs are huge. By default keep 4xx/5xx and slow requests only."""
    events = []
    for raw in text.splitlines():
        m = _NGINX_ACCESS.match(raw.strip())
        if not m:
            continue
        status = int(m.group("status"))
        rt = float(m.group("rt")) if m.group("rt") else 0.0
        slow = rt >= 3.0
        if only_errors and status < 400 and not slow:
            continue
        ts = _parse_ts_clf(m.group("ts"))
        msg = f'{m.group("method")} {m.group("path")} -> {status}' + (f" rt={rt}s" if rt else "")
        ev = _base("nginx", msg, ts, log="access", status=status, path=m.group("path"), rt=rt, ip=m.group("ip"))
        ev["level"] = "error" if status >= 500 else ("warning" if status >= 400 or slow else "info")
        events.append(ev)
    return events


def _parse_ts_clf(raw: str) -> datetime:
    try:
        return datetime.strptime(raw.split()[0], "%d/%b/%Y:%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def access_log_stats(text: str) -> dict:
    """Cheap rollup used by the observability layer."""
    total = err5 = err4 = 0
    rts: list[float] = []
    paths: dict[str, int] = {}
    for raw in text.splitlines():
        m = _NGINX_ACCESS.match(raw.strip())
        if not m:
            continue
        total += 1
        status = int(m.group("status"))
        if status >= 500:
            err5 += 1
            paths[m.group("path")] = paths.get(m.group("path"), 0) + 1
        elif status >= 400:
            err4 += 1
        if m.group("rt"):
            rts.append(float(m.group("rt")))
    rts.sort()
    p95 = rts[int(len(rts) * 0.95)] * 1000 if rts else 0.0
    return {
        "requests": total,
        "http_5xx": err5,
        "http_4xx": err4,
        "p95_ms": round(p95, 1),
        "error_rate": round((err5 + err4) / total, 4) if total else 0.0,
        "top_failing_paths": sorted(paths.items(), key=lambda kv: kv[1], reverse=True)[:5],
    }


# --------------------------------------------------------------------------- #
# System / journald
# --------------------------------------------------------------------------- #
_SYSLOG = re.compile(r"^(?P<ts>\w{3}\s+\d+\s\d{2}:\d{2}:\d{2})\s(?P<host>\S+)\s(?P<proc>[^:]+):\s(?P<rest>.*)$")


def parse_system_log(text: str) -> list[dict]:
    events = []
    year = datetime.now(timezone.utc).year
    for raw in text.splitlines():
        m = _SYSLOG.match(raw.strip())
        if not m:
            if raw.strip():
                events.append(_base("system", raw, datetime.now(timezone.utc)))
            continue
        try:
            ts = datetime.strptime(f"{year} {m.group('ts')}", "%Y %b %d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.now(timezone.utc)
        events.append(_base("system", m.group("rest"), ts, process=m.group("proc")))
    return events


PARSERS = {
    "actions": parse_actions_log,
    "docker": parse_docker_log,
    "nginx_error": parse_nginx_error_log,
    "nginx_access": parse_nginx_access_log,
    "system": parse_system_log,
}


def parse(source: str, text: str, **kw) -> list[dict]:
    fn = PARSERS.get(source)
    if fn:
        return fn(text, **kw)
    return [_base(source or "app", line, datetime.now(timezone.utc)) for line in text.splitlines() if line.strip()]
