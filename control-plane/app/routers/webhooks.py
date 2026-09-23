from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..integrations import github
from ..models import Deployment, DeploymentStatus, LogEvent, Project, utcnow
from ..rca import collectors
from ..rca.engine import analyze_deployment
from ..security import verify_github_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(default="", alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
):
    body = await request.body()
    if not verify_github_signature(body, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")

    payload = await request.json()
    repo = (payload.get("repository") or {}).get("full_name", "")

    if x_github_event == "workflow_run":
        run = payload.get("workflow_run") or {}
        if payload.get("action") == "completed":
            background.add_task(_handle_workflow_completed, repo, run)
        return {"handled": "workflow_run", "action": payload.get("action")}

    if x_github_event == "push":
        return {"handled": "push", "repo": repo, "ref": payload.get("ref")}

    if x_github_event == "ping":
        return {"handled": "ping", "ok": True}

    return {"handled": "ignored", "event": x_github_event}


def _handle_workflow_completed(repo: str, run: dict) -> None:
    """Pull the failed job logs and diagnose, without blocking GitHub's request."""
    db: Session = SessionLocal()
    try:
        project = db.query(Project).filter(Project.github_repo == repo).first()
        if not project:
            log.info("Webhook for unregistered repo %s — ignoring", repo)
            return

        run_id = str(run.get("id", ""))
        conclusion = (run.get("conclusion") or "").lower()

        dep = (
            db.query(Deployment)
            .filter(Deployment.gh_run_id == run_id)
            .order_by(Deployment.started_at.desc())
            .first()
        )
        if not dep:
            dep = Deployment(
                project_id=project.id,
                server_id=project.server_id,
                commit_sha=run.get("head_sha", ""),
                branch=run.get("head_branch", project.default_branch),
                gh_run_id=run_id,
                gh_run_url=run.get("html_url", ""),
                triggered_by=(run.get("actor") or {}).get("login", ""),
                container_name=project.slug,
                status=DeploymentStatus.building.value,
            )
            db.add(dep)
            db.flush()

        dep.status = DeploymentStatus.running.value if conclusion == "success" else DeploymentStatus.failed.value
        dep.finished_at = utcnow()
        dep.gh_run_url = run.get("html_url", dep.gh_run_url)
        db.commit()

        if conclusion == "success":
            return

        try:
            logs = github.get_run_logs(repo, run_id, failed_only=True)
        except github.GitHubError as exc:
            log.warning("Could not fetch Actions logs for %s run %s: %s", repo, run_id, exc)
            logs = {}

        rows = []
        for job_name, text in logs.items():
            events = collectors.parse_actions_log(text, job=job_name)
            events = collectors.extract_actions_failure_window(events)
            rows += [
                LogEvent(
                    deployment_id=dep.id,
                    project_id=project.id,
                    server_id=dep.server_id,
                    source="actions",
                    level=e["level"],
                    ts=e["ts"],
                    message=e["message"],
                    meta=e.get("meta", {}),
                )
                for e in events
            ]
        if rows:
            db.bulk_save_objects(rows[:4000])
            db.commit()
            log.info("Ingested %d Actions log events for %s run %s", len(rows), repo, run_id)

        incident = analyze_deployment(db, dep.id)
        if incident:
            log.info("Webhook-triggered diagnosis: incident %s (%s)", incident.id, incident.title)
    except Exception:
        log.exception("Failed to process workflow_run webhook for %s", repo)
    finally:
        db.close()
