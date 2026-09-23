"""Project risk / mentor triage.

Ethics note, because this is the part that could go wrong: this scores
*projects*, never students. Every signal is an engineering artifact the team
produces publicly (deployments, incidents, resource anomalies). There is no
individual attribution, no message reading, and no ranking of people. The
output is always framed as "this project needs help with X", and the whole
risk history is visible to the team itself.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.orm import Session

from ..llm.client import llm
from ..llm.prompts import TRIAGE_SYSTEM
from ..models import (
    Anomaly,
    Deployment,
    DeploymentStatus,
    Incident,
    IncidentStatus,
    Project,
    RiskSnapshot,
    utcnow,
)


def collect_signals(db: Session, project: Project, *, days: int = 14) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    signals: list[dict] = []

    deps = (
        db.query(Deployment)
        .filter(Deployment.project_id == project.id, Deployment.started_at >= since)
        .order_by(Deployment.started_at.desc())
        .all()
    )
    failed = [d for d in deps if d.status == DeploymentStatus.failed.value]
    ok = [d for d in deps if d.status in (DeploymentStatus.running.value, DeploymentStatus.stopped.value)]

    if not deps:
        last = (
            db.query(Deployment)
            .filter(Deployment.project_id == project.id)
            .order_by(Deployment.started_at.desc())
            .first()
        )
        if last:
            days_since = (utcnow() - (last.started_at if last.started_at.tzinfo else last.started_at)).days
            signals.append(
                {
                    "kind": "no_recent_activity",
                    "weight": min(30, days_since),
                    "detail": f"No deployment in {days_since} days. The project may be blocked, or working locally without deploying.",
                }
            )
        else:
            signals.append({"kind": "never_deployed", "weight": 20, "detail": "This project has never been deployed."})
    else:
        fail_rate = len(failed) / len(deps)
        if fail_rate >= 0.5 and len(deps) >= 2:
            signals.append(
                {
                    "kind": "high_failure_rate",
                    "weight": int(35 * fail_rate),
                    "detail": f"{len(failed)} of {len(deps)} deployments failed in the last {days} days.",
                }
            )
        streak = 0
        for d in deps:
            if d.status == DeploymentStatus.failed.value:
                streak += 1
            else:
                break
        if streak >= 3:
            signals.append(
                {
                    "kind": "consecutive_failures",
                    "weight": min(35, 8 * streak),
                    "detail": f"{streak} deployments in a row have failed. Repeated attempts without success means the team is stuck, not iterating.",
                }
            )
        if not ok and len(deps) >= 2:
            signals.append({"kind": "never_succeeded", "weight": 25, "detail": f"No successful deployment yet across {len(deps)} attempts."})

    open_incidents = (
        db.query(Incident)
        .filter(Incident.project_id == project.id, Incident.status.notin_((IncidentStatus.resolved.value, IncidentStatus.dismissed.value)))
        .all()
    )
    if open_incidents:
        oldest = min(i.created_at for i in open_incidents)
        age_h = (utcnow() - (oldest if oldest.tzinfo else oldest)).total_seconds() / 3600
        crit = [i for i in open_incidents if i.severity == "critical"]
        signals.append(
            {
                "kind": "open_incidents",
                "weight": min(30, 6 * len(open_incidents) + 8 * len(crit) + int(age_h // 24) * 3),
                "detail": (
                    f"{len(open_incidents)} unresolved incident(s), "
                    f"{len(crit)} critical, oldest open for {age_h:.0f} hours. "
                    + "; ".join(i.title for i in open_incidents[:3])
                ),
            }
        )
        repeated: dict[str, int] = {}
        for i in open_incidents:
            if i.signature_key:
                repeated[i.signature_key] = repeated.get(i.signature_key, 0) + 1
        for key, n in repeated.items():
            if n >= 2:
                signals.append(
                    {
                        "kind": "repeating_same_failure",
                        "weight": 15,
                        "detail": f"The same failure ({key}) has recurred {n} times — the team is retrying rather than fixing the cause. This is the clearest signal that a 30-minute mentor session would unblock them.",
                    }
                )

    anomalies = (
        db.query(Anomaly)
        .filter(Anomaly.project_id == project.id, Anomaly.acknowledged.is_(False), Anomaly.detected_at >= utcnow() - timedelta(days=3))
        .all()
    )
    crit_anom = [a for a in anomalies if a.severity == "critical"]
    if crit_anom:
        signals.append(
            {
                "kind": "runtime_instability",
                "weight": min(25, 8 * len(crit_anom)),
                "detail": "; ".join(a.message for a in crit_anom[:3]),
            }
        )

    return signals


def score(signals: list[dict]) -> tuple[int, str]:
    total = min(100, sum(s["weight"] for s in signals))
    band = "green" if total < 25 else ("amber" if total < 55 else "red")
    return total, band


def evaluate(db: Session, project: Project, *, use_llm: bool = True) -> RiskSnapshot:
    signals = collect_signals(db, project)
    total, band = score(signals)

    recommendation = _heuristic_recommendation(signals, band)
    needs_mentor = band in ("amber", "red")

    if use_llm and signals:
        verdict = llm.complete_json(
            TRIAGE_SYSTEM,
            json.dumps(
                {
                    "project": {"name": project.name, "team": project.team_name},
                    "signals": [{"kind": s["kind"], "detail": s["detail"]} for s in signals],
                    "risk_score": total,
                },
                indent=2,
            ),
            fallback={},
        )
        if verdict.get("_llm_used"):
            summary = str(verdict.get("summary", "")).strip()
            intervention = str(verdict.get("recommended_intervention", "")).strip()
            if summary or intervention:
                recommendation = (summary + ("\n\nSuggested: " + intervention if intervention else "")).strip()
            needs_mentor = bool(verdict.get("blocked")) or needs_mentor

    snap = RiskSnapshot(
        project_id=project.id,
        risk_score=total,
        band=band,
        signals=signals,
        recommendation=recommendation,
        needs_mentor=needs_mentor,
    )
    db.add(snap)
    db.commit()
    return snap


def _heuristic_recommendation(signals: list[dict], band: str) -> str:
    kinds = {s["kind"] for s in signals}
    if "repeating_same_failure" in kinds:
        return "The same deployment failure keeps recurring. Book 30 minutes with a DevOps mentor — the team is retrying rather than diagnosing."
    if "consecutive_failures" in kinds:
        return "Several deployments have failed in a row. Walk through the latest incident's root cause with the team rather than letting them keep pushing."
    if "runtime_instability" in kinds:
        return "The app is deployed but unstable at runtime. Review resource limits and the memory/restart anomalies with the team."
    if "no_recent_activity" in kinds or "never_deployed" in kinds:
        return "No recent deployment activity. Check in on whether the team is blocked on something outside the code."
    if "open_incidents" in kinds:
        return "Unresolved incidents are ageing. Confirm the team has seen the diagnosis and knows the next step."
    if band == "green":
        return "No intervention needed. The project is deploying and running normally."
    return "Review the signals below with the team."


def leaderboard(db: Session, *, limit: int = 50) -> list[dict]:
    """Latest risk snapshot per project, worst first — the mentor's landing page."""
    projects = db.query(Project).filter(Project.archived.is_(False)).all()
    rows = []
    for p in projects:
        snap = (
            db.query(RiskSnapshot)
            .filter(RiskSnapshot.project_id == p.id)
            .order_by(RiskSnapshot.ts.desc())
            .first()
        )
        rows.append(
            {
                "project_id": p.id,
                "name": p.name,
                "slug": p.slug,
                "team": p.team_name,
                "risk_score": snap.risk_score if snap else 0,
                "band": snap.band if snap else "unknown",
                "needs_mentor": snap.needs_mentor if snap else False,
                "recommendation": snap.recommendation if snap else "",
                "signals": snap.signals if snap else [],
                "evaluated_at": snap.ts.isoformat() if snap else None,
            }
        )
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows[:limit]
