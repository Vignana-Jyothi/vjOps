from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, require_mentor
from ..models import (
    Anomaly,
    Deployment,
    DeploymentStatus,
    Incident,
    IncidentStatus,
    MetricSample,
    Project,
    RepoAnalysis,
    RiskSnapshot,
    Role,
    Server,
    User,
    utcnow,
)
from ..observability import anomaly as anomaly_svc
from ..observability import risk as risk_svc

router = APIRouter(prefix="/api/observability", tags=["observability"])


def _owned_project_ids(db: Session, user: User) -> list[str] | None:
    """Students only see their own projects' data; None means "everything"."""
    if user.role != Role.student.value:
        return None
    return [p.id for p in db.query(Project.id).filter(Project.owner_id == user.id).all()]


@router.get("/anomalies")
def list_anomalies(
    project_id: str | None = None,
    acknowledged: bool = False,
    limit: int = Query(100, le=300),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    q = db.query(Anomaly).filter(Anomaly.acknowledged.is_(acknowledged))
    owned = _owned_project_ids(db, user)
    if owned is not None:
        q = q.filter(Anomaly.project_id.in_(owned))
    if project_id:
        q = q.filter(Anomaly.project_id == project_id)
    rows = q.order_by(Anomaly.detected_at.desc()).limit(limit).all()
    return [
        {
            "id": a.id,
            "project_id": a.project_id,
            "deployment_id": a.deployment_id,
            "kind": a.kind,
            "metric": a.metric,
            "severity": a.severity,
            "value": a.value,
            "baseline": a.baseline,
            "z_score": a.z_score,
            "message": a.message,
            "prediction": a.prediction,
            "detected_at": a.detected_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/anomalies/{anomaly_id}/acknowledge")
def acknowledge(anomaly_id: str, db: Session = Depends(get_db), _: User = Depends(current_user)):
    a = db.get(Anomaly, anomaly_id)
    if not a:
        raise HTTPException(404, "Anomaly not found")
    a.acknowledged = True
    db.commit()
    return {"id": a.id, "acknowledged": True}


@router.post("/deployments/{deployment_id}/scan")
def scan_deployment(deployment_id: str, db: Session = Depends(get_db), _: User = Depends(current_user)):
    findings = anomaly_svc.detect(db, deployment_id)
    created = anomaly_svc.persist(db, deployment_id, findings)
    return {"findings": findings, "new_anomalies": len(created)}


@router.get("/metrics/{deployment_id}")
def metrics_series(
    deployment_id: str,
    hours: int = Query(6, le=168),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    since = utcnow() - timedelta(hours=hours)
    rows = (
        db.query(MetricSample)
        .filter(MetricSample.deployment_id == deployment_id, MetricSample.ts >= since)
        .order_by(MetricSample.ts.asc())
        .limit(5000)
        .all()
    )
    return {
        "deployment_id": deployment_id,
        "points": [
            {
                "ts": r.ts.isoformat(),
                "cpu_pct": r.cpu_pct,
                "mem_mb": r.mem_mb,
                "mem_limit_mb": r.mem_limit_mb,
                "restarts": r.restarts,
                "disk_pct": r.disk_pct,
                "http_5xx": r.http_5xx,
                "http_p95_ms": r.http_p95_ms,
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Mentor triage
# --------------------------------------------------------------------------- #
@router.get("/risk")
def risk_board(db: Session = Depends(get_db), _: User = Depends(require_mentor)):
    return {
        "projects": risk_svc.leaderboard(db),
        "note": (
            "These are project-level engineering signals — deployments, incidents and resource "
            "behaviour. No individual student activity is tracked, and the purpose is to route "
            "help to teams that are stuck."
        ),
    }


@router.post("/risk/{project_id}/evaluate")
def evaluate_risk(project_id: str, db: Session = Depends(get_db), _: User = Depends(require_mentor)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    snap = risk_svc.evaluate(db, project)
    return {
        "project_id": project_id,
        "risk_score": snap.risk_score,
        "band": snap.band,
        "needs_mentor": snap.needs_mentor,
        "recommendation": snap.recommendation,
        "signals": snap.signals,
        "evaluated_at": snap.ts.isoformat(),
    }


@router.get("/risk/{project_id}/history")
def risk_history(project_id: str, limit: int = Query(30, le=200), db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = (
        db.query(RiskSnapshot)
        .filter(RiskSnapshot.project_id == project_id)
        .order_by(RiskSnapshot.ts.desc())
        .limit(limit)
        .all()
    )
    return [{"ts": r.ts.isoformat(), "risk_score": r.risk_score, "band": r.band, "needs_mentor": r.needs_mentor} for r in rows]


# --------------------------------------------------------------------------- #
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    day = utcnow() - timedelta(days=1)
    week = utcnow() - timedelta(days=7)
    owned = _owned_project_ids(db, user)

    def scoped(q, col):
        return q if owned is None else q.filter(col.in_(owned))

    deps_week = scoped(db.query(Deployment), Deployment.project_id).filter(Deployment.started_at >= week).all()
    failed_week = [d for d in deps_week if d.status == DeploymentStatus.failed.value]
    open_incidents = (
        scoped(db.query(Incident), Incident.project_id)
        .filter(Incident.status.notin_((IncidentStatus.resolved.value, IncidentStatus.dismissed.value)))
        .all()
    )
    # Server inventory is infrastructure detail students don't need.
    servers = db.query(Server).all() if owned is None else []

    diagnosed = [i for i in open_incidents if i.confidence >= 0.6]
    resolved_week = scoped(db.query(Incident), Incident.project_id).filter(Incident.resolved_at >= week).all()
    mttr = None
    if resolved_week:
        durations = [
            (i.resolved_at - i.created_at).total_seconds() / 60
            for i in resolved_week
            if i.resolved_at and i.created_at
        ]
        if durations:
            mttr = round(sum(durations) / len(durations), 1)

    return {
        "projects": scoped(db.query(Project), Project.id).filter(Project.archived.is_(False)).count(),
        "servers": {
            "total": len(servers),
            "online": len([s for s in servers if s.online]),
            "names": [{"name": s.name, "online": s.online, "last_seen": s.last_seen.isoformat() if s.last_seen else None} for s in servers],
        },
        "deployments_7d": {
            "total": len(deps_week),
            "failed": len(failed_week),
            "success_rate": round(1 - len(failed_week) / len(deps_week), 3) if deps_week else None,
            "last_24h": scoped(db.query(Deployment), Deployment.project_id).filter(Deployment.started_at >= day).count(),
        },
        "incidents": {
            "open": len(open_incidents),
            "critical": len([i for i in open_incidents if i.severity == "critical"]),
            "auto_diagnosed": len(diagnosed),
            "auto_diagnosis_rate": round(len(diagnosed) / len(open_incidents), 3) if open_incidents else None,
            "mean_time_to_resolve_min": mttr,
        },
        "anomalies_open": scoped(db.query(Anomaly), Anomaly.project_id).filter(Anomaly.acknowledged.is_(False)).count(),
        # Drives the dashboard's getting-started checklist.
        "setup": {
            "analyses": scoped(db.query(RepoAnalysis), RepoAnalysis.project_id).count(),
            "deployments": scoped(db.query(Deployment), Deployment.project_id).count(),
            "users": db.query(User).count() if owned is None else None,
        },
    }
