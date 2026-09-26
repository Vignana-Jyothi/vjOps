from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from ..config import settings
from ..db import SessionLocal
from ..models import Deployment, DeploymentStatus, LogEvent, MetricSample, Project, RepoAnalysis, utcnow
from ..observability import anomaly as anomaly_svc
from ..observability import risk as risk_svc
from ..rca.engine import analyze_deployment
from ..remediation.verify import run_verification_sweep
from ..tracing import record as record_trace
from .celery_app import celery

log = logging.getLogger(__name__)


@celery.task(name="viljaops.analyze_deployment", bind=True, max_retries=2)
def analyze_deployment_task(self, deployment_id: str):
    db = SessionLocal()
    try:
        incident = analyze_deployment(db, deployment_id)
        return {"incident_id": incident.id if incident else None}
    except Exception as exc:
        log.exception("RCA task failed for %s", deployment_id)
        raise self.retry(exc=exc, countdown=30)
    finally:
        db.close()


@celery.task(name="viljaops.analyze_repo")
def analyze_repo_task(project_id: str, ref: str = "", use_llm: bool = True):
    from ..analyzers import repo as repo_analyzer
    from ..analyzers.plan import recommend_resources

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if not project or not project.github_repo:
            return {"error": "project not found or has no repository"}

        dest = Path(settings.repo_cache_dir) / project.slug
        clone = repo_analyzer.clone_repo(project.github_repo, dest, ref=ref or project.default_branch, token=settings.github_token)
        if not clone["ok"]:
            return {"error": clone["error"]}

        result = repo_analyzer.analyze(dest, use_llm=use_llm)
        result["plan"]["resources"] = recommend_resources(result["detected"], use_llm=use_llm)

        analysis = RepoAnalysis(
            project_id=project.id,
            commit_sha=clone["commit_sha"],
            score=result["score"],
            grade=result["grade"],
            blocking_count=result["blocking_count"],
            breakdown=result["breakdown"],
            detected=result["detected"],
            findings=result["findings"],
            plan=result["plan"],
            artifacts=result["artifacts"],
            llm_review=result.get("llm_review", ""),
            duration_ms=result["duration_ms"],
        )
        db.add(analysis)
        db.commit()
        record_trace(
            db,
            "repository",
            (
                f"Analyzed {project.github_repo}@{clone['commit_sha'][:8]}: "
                f"readiness {result['score']}/100 ({result['grade']}), "
                f"{result['blocking_count']} blocking finding(s), "
                f"stack: {', '.join(k for k, v in (result['detected'] or {}).items() if v) or 'undetected'}."
            ),
            project_id=project.id,
            outcome="fail" if result["blocking_count"] else "pass",
            detail={"analysis_id": analysis.id, "score": result["score"], "grade": result["grade"]},
        )
        return {"analysis_id": analysis.id, "score": analysis.score}
    finally:
        db.close()


@celery.task(name="viljaops.sweep_anomalies")
def sweep_anomalies():
    """Scan every running deployment for emerging problems."""
    db = SessionLocal()
    created = 0
    try:
        running = db.query(Deployment).filter(Deployment.status == DeploymentStatus.running.value).all()
        for dep in running:
            try:
                findings = anomaly_svc.detect(db, dep.id)
                created += len(anomaly_svc.persist(db, dep.id, findings))
            except Exception:
                log.exception("Anomaly sweep failed for deployment %s", dep.id)
        return {"scanned": len(running), "new_anomalies": created}
    finally:
        db.close()


@celery.task(name="viljaops.verify_fixes")
def verify_fixes_task():
    """The Verification Agent's heartbeat: resolve every fix whose watch
    window has elapsed since the last sweep."""
    db = SessionLocal()
    try:
        result = run_verification_sweep(db)
        if result.get("failed") or result.get("unknown"):
            log.warning("Verification sweep: %s", result)
        return result
    finally:
        db.close()


@celery.task(name="viljaops.refresh_risk")
def refresh_risk():
    db = SessionLocal()
    try:
        projects = db.query(Project).filter(Project.archived.is_(False)).all()
        flagged = 0
        for p in projects:
            try:
                snap = risk_svc.evaluate(db, p, use_llm=False)
                flagged += 1 if snap.needs_mentor else 0
            except Exception:
                log.exception("Risk evaluation failed for project %s", p.id)
        return {"projects": len(projects), "needing_mentor": flagged}
    finally:
        db.close()


@celery.task(name="viljaops.prune_logs")
def prune_logs(days: int = 30, metric_days: int = 14):
    """Log volume grows fast. Keep incidents forever, raw lines for a month."""
    db = SessionLocal()
    try:
        log_cutoff = utcnow() - timedelta(days=days)
        metric_cutoff = utcnow() - timedelta(days=metric_days)
        logs = db.query(LogEvent).filter(LogEvent.ts < log_cutoff).delete(synchronize_session=False)
        metrics = db.query(MetricSample).filter(MetricSample.ts < metric_cutoff).delete(synchronize_session=False)
        db.commit()
        return {"log_events_deleted": logs, "metric_samples_deleted": metrics}
    finally:
        db.close()
