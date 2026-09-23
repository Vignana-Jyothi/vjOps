"""Multi-source correlation.

The whole premise of the DevOps copilot is that no single log tells the story.
A 502 in Nginx, a SIGKILL in the Docker events stream and a MemoryError in the
app log are one incident, and only look like one when they are on the same
timeline.

This module builds that timeline, compresses it to something a 14B model can
actually read, and extracts cross-source signals a single-log tool would miss.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import Deployment, LogEvent, MetricSample

# Values that differ per-run but not per-cause. Masking them lets us collapse
# a thousand near-identical lines into one.
_NOISE = [
    (re.compile(r"\b[0-9a-f]{12,64}\b"), "<hash>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"\b\d{4,}\b"), "<num>"),
]


def fingerprint(message: str) -> str:
    norm = message.strip()[:400]
    for pat, repl in _NOISE:
        norm = pat.sub(repl, norm)
    return hashlib.blake2b(norm.encode(), digest_size=8).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_timeline(
    db: Session,
    deployment_id: str,
    *,
    window_minutes: int = 45,
    max_events: int = 220,
) -> dict:
    """Collect, dedupe and rank events for one deployment."""
    dep = db.get(Deployment, deployment_id)
    if not dep:
        return {"events": [], "text": "", "signals": [], "sources": []}

    anchor = _aware(dep.finished_at or dep.started_at)
    lo = anchor - timedelta(minutes=window_minutes)
    hi = anchor + timedelta(minutes=10)

    base = db.query(LogEvent).filter(LogEvent.deployment_id == deployment_id)

    rows = (
        base.filter(LogEvent.ts >= lo, LogEvent.ts <= hi)
        .order_by(LogEvent.ts.asc())
        .limit(6000)
        .all()
    )

    # Container clocks drift, students set wrong timezones, and logs shipped
    # late can carry timestamps far outside the deployment window. Events are
    # already scoped to this deployment, so falling back to all of them is safe
    # — and far better than reporting "nothing to analyze" on a real failure.
    windowed = bool(rows)
    if not rows:
        rows = base.order_by(LogEvent.ts.asc()).limit(6000).all()
        if rows:
            anchor = _aware(rows[-1].ts)

    # Collapse repeats by fingerprint, keeping first + count.
    collapsed: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        fp = fingerprint(row.message)
        key = f"{row.source}:{fp}"
        if key in collapsed:
            c = collapsed[key]
            c["count"] += 1
            c["last_ts"] = _aware(row.ts)
            continue
        collapsed[key] = {
            "id": f"E{len(order) + 1}",
            "db_id": row.id,
            "source": row.source,
            "level": row.level,
            "ts": _aware(row.ts),
            "last_ts": _aware(row.ts),
            "message": row.message,
            "meta": row.meta or {},
            "count": 1,
        }
        order.append(key)

    events = [collapsed[k] for k in order]

    # Rank: errors first, then anything near the failure moment.
    level_w = {"critical": 4, "error": 3, "warning": 2, "info": 1, "debug": 0}

    def score(e: dict) -> float:
        proximity = 1.0 / (1.0 + abs((e["ts"] - anchor).total_seconds()) / 60.0)
        return level_w.get(e["level"], 1) * 10 + proximity * 5 + min(e["count"], 10) * 0.2

    if len(events) > max_events:
        keep = sorted(events, key=score, reverse=True)[:max_events]
        keep_ids = {e["id"] for e in keep}
        events = [e for e in events if e["id"] in keep_ids]

    # Renumber so ids the model cites are contiguous and stable.
    for i, e in enumerate(events, 1):
        e["id"] = f"E{i}"

    metrics = summarize_metrics(db, deployment_id, lo, hi)
    signals = cross_source_signals(events, metrics)

    return {
        "events": events,
        "text": render_timeline(events),
        "metrics": metrics,
        "signals": signals,
        "sources": sorted({e["source"] for e in events}),
        "anchor": anchor.isoformat(),
        "total_raw_events": len(rows),
        "window_applied": windowed,
    }


def render_timeline(events: list[dict]) -> str:
    lines = []
    for e in events:
        rep = f" (x{e['count']})" if e["count"] > 1 else ""
        ts = e["ts"].strftime("%H:%M:%S")
        msg = e["message"].replace("\n", "\n        ")
        lines.append(f"[{e['id']}] {ts} {e['source']:<7} {e['level']:<8}{rep} | {msg}")
    return "\n".join(lines)


def summarize_metrics(db: Session, deployment_id: str, lo: datetime, hi: datetime) -> dict:
    samples = (
        db.query(MetricSample)
        .filter(MetricSample.deployment_id == deployment_id)
        .filter(MetricSample.ts >= lo, MetricSample.ts <= hi)
        .order_by(MetricSample.ts.asc())
        .limit(2000)
        .all()
    )
    if not samples:
        return {"available": False}

    def stat(attr: str) -> dict:
        vals = [getattr(s, attr) or 0.0 for s in samples]
        return {
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "avg": round(sum(vals) / len(vals), 2),
            "last": round(vals[-1], 2),
        }

    mem = stat("mem_mb")
    limit = max((s.mem_limit_mb or 0) for s in samples)
    return {
        "available": True,
        "samples": len(samples),
        "cpu_pct": stat("cpu_pct"),
        "mem_mb": mem,
        "mem_limit_mb": round(limit, 1),
        "mem_headroom_pct": round(100 * (1 - mem["max"] / limit), 1) if limit else None,
        "disk_pct": stat("disk_pct"),
        "restarts": max(s.restarts or 0 for s in samples),
        "http_5xx_total": sum(s.http_5xx or 0 for s in samples),
        "http_p95_ms": stat("http_p95_ms"),
    }


def _is_failure(event: dict) -> bool:
    """Judge by content as well as by the stored level.

    Levels are assigned by whichever collector ingested the line, and an agent
    or CI job can ship a line pre-labelled 'info'. Re-deriving from the message
    keeps the correlation layer from concluding a container never started when
    the logs plainly show it dying.
    """
    if event.get("level") in ("error", "critical"):
        return True
    from .collectors import guess_level

    return guess_level(event.get("message") or "") in ("error", "critical")


def cross_source_signals(events: list[dict], metrics: dict) -> list[dict]:
    """Findings that only exist because sources were combined.

    These get handed to the model as pre-computed hints, and are shown in the UI
    as the 'why we correlated these' explanation.
    """
    signals: list[dict] = []
    by_source: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_source[e["source"]].append(e)

    nginx_502 = [e for e in by_source["nginx"] if "502" in e["message"] or "Connection refused" in e["message"]]
    docker_death = [
        e
        for e in by_source["docker"]
        if re.search(r"exited with code|died|OOMKilled|Restarting", e["message"], re.I)
    ]
    if nginx_502 and docker_death:
        gap = abs((nginx_502[0]["ts"] - docker_death[0]["ts"]).total_seconds())
        if gap < 300:
            signals.append(
                {
                    "kind": "proxy_follows_container_death",
                    "confidence": 0.9,
                    "detail": (
                        f"Nginx started returning 502 within {int(gap)}s of the container "
                        "dying. The proxy is a symptom; the container is the cause."
                    ),
                    "evidence_ids": [nginx_502[0]["id"], docker_death[0]["id"]],
                }
            )

    if metrics.get("available"):
        head = metrics.get("mem_headroom_pct")
        if head is not None and head < 8:
            signals.append(
                {
                    "kind": "memory_ceiling",
                    "confidence": 0.85,
                    "detail": (
                        f"Peak memory reached {metrics['mem_mb']['max']} MB against a "
                        f"{metrics['mem_limit_mb']} MB limit ({head}% headroom). Memory "
                        "pressure is a plausible contributor regardless of the visible error."
                    ),
                    "evidence_ids": [],
                }
            )
        if metrics.get("restarts", 0) >= 3:
            signals.append(
                {
                    "kind": "restart_loop",
                    "confidence": 0.92,
                    "detail": f"The container restarted {metrics['restarts']} times in this window — a deterministic startup failure, not a transient one.",
                    "evidence_ids": [],
                }
            )
        if metrics.get("disk_pct", {}).get("max", 0) > 90:
            signals.append(
                {
                    "kind": "disk_pressure",
                    "confidence": 0.88,
                    "detail": f"Server disk reached {metrics['disk_pct']['max']}% during this deployment — builds and writes fail unpredictably above 90%.",
                    "evidence_ids": [],
                }
            )

    build_errors = [e for e in by_source["actions"] if _is_failure(e)]
    runtime_errors = [e for e in by_source["docker"] if _is_failure(e)]
    if build_errors and not runtime_errors:
        signals.append(
            {
                "kind": "failed_before_runtime",
                "confidence": 0.8,
                "detail": "The pipeline failed before the container ever started, so runtime logs are empty by definition. Look at the build stage.",
                "evidence_ids": [build_errors[-1]["id"]],
            }
        )
    if runtime_errors and not build_errors:
        signals.append(
            {
                "kind": "build_ok_runtime_failed",
                "confidence": 0.8,
                "detail": "The image built successfully and failed at runtime — so this is configuration or environment, not compilation.",
                "evidence_ids": [runtime_errors[0]["id"]],
            }
        )

    counts = Counter(e["source"] for e in events)
    if counts:
        signals.append(
            {
                "kind": "coverage",
                "confidence": 1.0,
                "detail": "Correlated " + ", ".join(f"{v} {k}" for k, v in counts.most_common()) + " events.",
                "evidence_ids": [],
            }
        )
    return signals
