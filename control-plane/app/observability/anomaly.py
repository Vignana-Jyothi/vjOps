"""Layer 3 — detect what is about to break, before it does.

Deliberately statistical rather than ML: a median/MAD robust z-score and a
least-squares trend need no training data, no GPU, and no cold-start period.
They also produce an explanation a student can read, which a learned detector
does not.

The valuable output is not "CPU is high" — it is "memory is climbing 40 MB an
hour against a 512 MB limit and will be killed in about 3 hours", which is
actionable while there is still time to act.
"""

from __future__ import annotations

import math
from datetime import timedelta
from statistics import median

from sqlalchemy.orm import Session

from ..models import Anomaly, Deployment, MetricSample, utcnow

MIN_SAMPLES = 12


def _mad_z(values: list[float]) -> tuple[float, float, float]:
    """Median absolute deviation z-score of the last value. Outlier-resistant."""
    if len(values) < 3:
        return 0.0, 0.0, 0.0
    med = median(values)
    deviations = [abs(v - med) for v in values]
    mad = median(deviations)
    scale = mad * 1.4826 if mad > 0 else (max(deviations) or 1e-9) / 3
    z = (values[-1] - med) / scale if scale else 0.0
    return z, med, scale


def _slope_per_hour(points: list[tuple[float, float]]) -> float:
    """Least-squares slope. points = [(seconds_since_start, value)]"""
    n = len(points)
    if n < 3:
        return 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    num = sum((x - mx) * (y - my) for x, y in points)
    den = sum((x - mx) ** 2 for x in points)
    if den == 0:
        return 0.0
    return (num / den) * 3600.0


def detect(db: Session, deployment_id: str, *, window_minutes: int = 180) -> list[dict]:
    dep = db.get(Deployment, deployment_id)
    if not dep:
        return []

    since = utcnow() - timedelta(minutes=window_minutes)
    samples = (
        db.query(MetricSample)
        .filter(MetricSample.deployment_id == deployment_id, MetricSample.ts >= since)
        .order_by(MetricSample.ts.asc())
        .limit(4000)
        .all()
    )
    if len(samples) < MIN_SAMPLES:
        return []

    t0 = samples[0].ts
    def secs(s):
        a, b = s.ts, t0
        a = a if a.tzinfo else a.replace(tzinfo=None)
        return (a - b).total_seconds()

    findings: list[dict] = []

    # ---------------- memory: the one that actually kills student apps -------
    mem = [s.mem_mb or 0.0 for s in samples]
    limit = max((s.mem_limit_mb or 0.0) for s in samples)
    mem_slope = _slope_per_hour([(secs(s), s.mem_mb or 0.0) for s in samples])

    if limit > 0:
        used_pct = 100 * mem[-1] / limit
        if mem_slope > 5 and used_pct > 50:
            headroom = limit - mem[-1]
            hours = headroom / mem_slope if mem_slope > 0 else math.inf
            if hours < 24:
                findings.append(
                    {
                        "kind": "memory_leak_trend",
                        "metric": "mem_mb",
                        "severity": "critical" if hours < 4 else "warning",
                        "value": round(mem[-1], 1),
                        "baseline": round(mem[0], 1),
                        "z_score": 0.0,
                        "message": (
                            f"Memory has grown from {mem[0]:.0f} MB to {mem[-1]:.0f} MB "
                            f"({mem_slope:.0f} MB/hour) against a {limit:.0f} MB limit. "
                            "Steady growth with no plateau is the signature of a leak — "
                            "commonly an unbounded cache, a list that is appended to per "
                            "request, or database connections that are never closed."
                        ),
                        "prediction": (
                            f"At this rate the container will be OOM-killed in roughly "
                            f"{hours:.1f} hours."
                        ),
                    }
                )
        elif used_pct > 92:
            findings.append(
                {
                    "kind": "memory_ceiling",
                    "metric": "mem_mb",
                    "severity": "critical",
                    "value": round(mem[-1], 1),
                    "baseline": round(limit, 1),
                    "z_score": 0.0,
                    "message": f"Using {used_pct:.0f}% of its {limit:.0f} MB memory limit.",
                    "prediction": "A single traffic spike will trigger an OOM kill (exit 137).",
                }
            )

    # ---------------- CPU ----------------
    cpu = [s.cpu_pct or 0.0 for s in samples]
    z, med, _ = _mad_z(cpu)
    if z > 4 and cpu[-1] > 70:
        findings.append(
            {
                "kind": "cpu_spike",
                "metric": "cpu_pct",
                "severity": "warning",
                "value": round(cpu[-1], 1),
                "baseline": round(med, 1),
                "z_score": round(z, 2),
                "message": f"CPU is at {cpu[-1]:.0f}% against a typical {med:.0f}% for this app.",
                "prediction": "Sustained saturation will show up as slow responses and Nginx 504s.",
            }
        )
    if len(cpu) >= 30 and min(cpu[-20:]) > 85:
        findings.append(
            {
                "kind": "cpu_saturation",
                "metric": "cpu_pct",
                "severity": "critical",
                "value": round(cpu[-1], 1),
                "baseline": round(med, 1),
                "z_score": round(z, 2),
                "message": "CPU has been pinned above 85% continuously — likely an infinite loop or a synchronous blocking call in a request handler.",
                "prediction": "Other projects on this server are being starved of CPU.",
            }
        )

    # ---------------- restarts ----------------
    restarts = [s.restarts or 0 for s in samples]
    delta = restarts[-1] - restarts[0]
    if delta >= 3:
        findings.append(
            {
                "kind": "restart_loop",
                "metric": "restarts",
                "severity": "critical",
                "value": float(restarts[-1]),
                "baseline": float(restarts[0]),
                "z_score": 0.0,
                "message": f"The container restarted {delta} times in the last {window_minutes} minutes.",
                "prediction": "A crash loop never self-resolves — the startup failure has to be fixed.",
            }
        )

    # ---------------- disk ----------------
    disk = [s.disk_pct or 0.0 for s in samples]
    disk_slope = _slope_per_hour([(secs(s), s.disk_pct or 0.0) for s in samples])
    if disk and disk[-1] > 85:
        hours = (95 - disk[-1]) / disk_slope if disk_slope > 0.05 else math.inf
        findings.append(
            {
                "kind": "disk_pressure",
                "metric": "disk_pct",
                "severity": "critical" if disk[-1] > 92 else "warning",
                "value": round(disk[-1], 1),
                "baseline": round(disk[0], 1),
                "z_score": 0.0,
                "message": f"Server disk is {disk[-1]:.0f}% full and rising {disk_slope:.2f}%/hour. Docker build cache and unrotated logs are the usual cause.",
                "prediction": ("Builds will start failing with 'no space left on device' in about "
                               f"{hours:.0f} hours." if hours != math.inf else
                               "Builds will fail once this reaches ~95%."),
            }
        )

    # ---------------- HTTP errors ----------------
    err = [s.http_5xx or 0 for s in samples]
    if sum(err) > 0:
        z_e, med_e, _ = _mad_z([float(e) for e in err])
        recent = sum(err[-6:])
        if recent >= 5 and z_e > 3:
            findings.append(
                {
                    "kind": "error_rate_spike",
                    "metric": "http_5xx",
                    "severity": "critical",
                    "value": float(recent),
                    "baseline": round(med_e, 2),
                    "z_score": round(z_e, 2),
                    "message": f"{recent} server errors in the most recent samples, against a typical {med_e:.1f}.",
                    "prediction": "Users are seeing failures right now.",
                }
            )

    # ---------------- latency ----------------
    p95 = [s.http_p95_ms or 0.0 for s in samples if (s.http_p95_ms or 0) > 0]
    if len(p95) >= MIN_SAMPLES:
        z_l, med_l, _ = _mad_z(p95)
        if z_l > 3.5 and p95[-1] > 1000:
            findings.append(
                {
                    "kind": "latency_regression",
                    "metric": "http_p95_ms",
                    "severity": "warning",
                    "value": round(p95[-1], 1),
                    "baseline": round(med_l, 1),
                    "z_score": round(z_l, 2),
                    "message": f"p95 response time is {p95[-1]:.0f} ms against a typical {med_l:.0f} ms.",
                    "prediction": "At Nginx's 60s read timeout these become 504s.",
                }
            )

    return findings


def persist(db: Session, deployment_id: str, findings: list[dict]) -> list[Anomaly]:
    """Store new anomalies, suppressing duplicates of anything still open."""
    dep = db.get(Deployment, deployment_id)
    if not dep:
        return []
    since = utcnow() - timedelta(hours=6)
    existing = {
        a.kind
        for a in db.query(Anomaly)
        .filter(Anomaly.deployment_id == deployment_id, Anomaly.detected_at >= since, Anomaly.acknowledged.is_(False))
        .all()
    }
    created = []
    for f in findings:
        if f["kind"] in existing:
            continue
        a = Anomaly(
            deployment_id=deployment_id,
            project_id=dep.project_id,
            kind=f["kind"],
            metric=f.get("metric", ""),
            severity=f.get("severity", "warning"),
            value=float(f.get("value", 0)),
            baseline=float(f.get("baseline", 0)),
            z_score=float(f.get("z_score", 0)),
            message=f.get("message", ""),
            prediction=f.get("prediction", ""),
        )
        db.add(a)
        created.append(a)
    if created:
        db.commit()
    return created
