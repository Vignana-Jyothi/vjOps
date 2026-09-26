from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..deps import audit, current_agent, current_user, require_project_access
from ..infra import nginx as nginx_svc
from ..infra import ports as port_svc
from ..models import (
    AgentCommand,
    Deployment,
    DeploymentStatus,
    LogEvent,
    Project,
    Role,
    Server,
    User,
    utcnow,
)
from ..rca import collectors
from ..rca.engine import analyze_deployment
from ..schemas import (
    DeploymentFinish,
    DeploymentOut,
    DeploymentStart,
    DeploymentStartOut,
    LogIngest,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/deployments", tags=["deployments"])

SUCCESS_WORDS = {"success", "succeeded", "running", "ok", "completed", "passed"}


def _resolve_project(db: Session, payload: DeploymentStart) -> Project:
    project = None
    if payload.project_slug:
        project = db.query(Project).filter(Project.slug == payload.project_slug).first()
    if not project and payload.repo:
        repo = payload.repo.strip()
        project = db.query(Project).filter(Project.github_repo == repo).first()
        if not project:
            project = db.query(Project).filter(Project.github_repo.ilike(f"%/{repo.split('/')[-1]}")).first()
    if not project:
        raise HTTPException(
            404,
            f"No ViljaOps project is registered for '{payload.repo or payload.project_slug}'. "
            "Register the project in the dashboard before deploying.",
        )
    return project


@router.post("/start", response_model=DeploymentStartOut, status_code=201)
def start_deployment(
    payload: DeploymentStart,
    db: Session = Depends(get_db),
    server: Server = Depends(current_agent),
):
    """Called by the GitHub Actions job on the self-hosted runner.

    Returns the host port the app must bind. This is what replaces manually
    checking which ports are free before every deployment.
    """
    project = _resolve_project(db, payload)

    dep = Deployment(
        project_id=project.id,
        server_id=server.id,
        commit_sha=payload.commit_sha,
        branch=payload.branch or project.default_branch,
        status=DeploymentStatus.building.value,
        phase="build",
        gh_run_id=payload.gh_run_id,
        gh_run_url=payload.gh_run_url,
        triggered_by=payload.triggered_by,
        container_name=payload.container_name or project.slug,
        image=payload.image,
        domain=project.domain,
    )
    db.add(dep)
    db.flush()

    try:
        alloc = port_svc.allocate(db, server.id, project_id=project.id, deployment_id=dep.id)
        dep.port = alloc.port
    except port_svc.NoPortsAvailable as exc:
        dep.status = DeploymentStatus.failed.value
        dep.error_summary = str(exc)
        db.commit()
        raise HTTPException(507, str(exc))

    if not project.server_id:
        project.server_id = server.id
    db.commit()

    audit(
        db, "deployment.start", actor=payload.triggered_by or "ci", actor_role="ci",
        target_type="deployment", target_id=dep.id,
        detail={"project": project.slug, "port": dep.port, "commit": payload.commit_sha[:8]},
    )
    return DeploymentStartOut(
        id=dep.id, project_id=project.id, port=dep.port, domain=dep.domain,
        container_name=dep.container_name, status=dep.status,
    )


@router.post("/{deployment_id}/finish", response_model=DeploymentOut)
def finish_deployment(
    deployment_id: str,
    payload: DeploymentFinish,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    server: Server = Depends(current_agent),
):
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "Deployment not found")
    if dep.server_id != server.id:
        raise HTTPException(403, "This deployment does not belong to your server")

    ok = payload.status.strip().lower() in SUCCESS_WORDS
    dep.status = DeploymentStatus.running.value if ok else DeploymentStatus.failed.value
    dep.phase = payload.phase or ("running" if ok else "failed")
    dep.finished_at = utcnow()
    if payload.image:
        dep.image = payload.image
    if payload.container_name:
        dep.container_name = payload.container_name
    if payload.error_summary:
        dep.error_summary = payload.error_summary[:2000]
    dep.rollback_status = payload.rollback_status or "not_attempted"
    if dep.rollback_status == "failed":
        # Not just "the new version didn't deploy" — the project has NO
        # application running at all right now. Worth being loud about.
        note = "\n\nROLLBACK FAILED: the previous known-good image also did not come up. This project has no running deployment."
        dep.error_summary = (dep.error_summary + note)[:2000]
        log.error("Rollback failed for deployment %s (project %s) — no application is running", dep.id, dep.project_id)
    db.commit()

    project = db.get(Project, dep.project_id)

    if ok and project and project.domain and dep.port:
        try:
            cfg = nginx_svc.create_config(db, project, server, domain=project.domain, upstream_port=dep.port)
            log.info("Nginx config %s generated for %s -> :%s", cfg.id, project.domain, dep.port)
            # Generating and validating the config used to be the whole story
            # here — nothing ever told the agent to actually write it and
            # reload, so a project's first successful deployment could sit
            # unreachable through its domain until a human found and called
            # POST /api/infra/nginx/{id}/apply by hand. Dispatch it now,
            # same as the manual endpoint does, so "successful deployment"
            # actually means "reachable".
            if cfg.status == "validated":
                if server.online:
                    cmd = AgentCommand(server_id=server.id, kind="apply_nginx_config", payload=nginx_svc.apply_payload(cfg, project))
                    db.add(cmd)
                    db.commit()
                    log.info("Dispatched apply_nginx_config %s for %s", cmd.id, project.slug)
                else:
                    log.warning(
                        "Nginx config %s validated for %s but the agent on %s is offline — "
                        "it will sit unapplied until POST /api/infra/nginx/%s/apply is called.",
                        cfg.id, project.slug, server.name, cfg.id,
                    )
        except nginx_svc.NginxError as exc:
            log.warning("Nginx config generation failed for %s: %s", project.slug, exc)

    if not ok:
        # RCA runs out of band so the CI job is never blocked waiting on inference.
        background.add_task(_analyze_async, deployment_id)

    # Deliberately NOT releasing the port here. The workflow's own rollback
    # step (see .github/workflows/viljaops-deploy.yml) restarts the previous
    # image on this exact port when it can, but the CI job's success at
    # doing that isn't reported back — so releasing unconditionally on any
    # failure meant the registry could mark a port "free" while a rolled-back
    # container was still actually bound to it. allocate() then handed that
    # "free" port to a completely different project's next deployment,
    # producing a real bind conflict for a team that did nothing wrong.
    # Leaving it allocated is safe by construction: this project's *own*
    # next deploy reclaims the same port automatically (allocate() reuses an
    # existing allocation for the same project+server+purpose), matching
    # whatever's actually still running there. A port that's genuinely dead
    # (the project abandoned it) is visible in GET /api/infra/ports/{server}
    # and freed deliberately via POST /api/infra/ports/{server}/{port}/release
    # rather than guessed at automatically.

    audit(
        db, "deployment.finish", actor="ci", actor_role="ci",
        target_type="deployment", target_id=dep.id, detail={"status": dep.status},
    )
    return dep


def _analyze_async(deployment_id: str) -> None:
    db = SessionLocal()
    try:
        incident = analyze_deployment(db, deployment_id)
        if incident:
            log.info("Auto-diagnosed deployment %s -> incident %s", deployment_id, incident.id)
    except Exception:
        log.exception("Root cause analysis failed for deployment %s", deployment_id)
    finally:
        db.close()


@router.post("/logs", status_code=202)
def ingest_logs(payload: LogIngest, db: Session = Depends(get_db), server: Server = Depends(current_agent)):
    """Accept a chunk of raw log text from CI or the server agent."""
    dep = db.get(Deployment, payload.deployment_id) if payload.deployment_id else None
    if not dep and payload.project_slug:
        project = db.query(Project).filter(Project.slug == payload.project_slug).first()
        if project:
            dep = (
                db.query(Deployment)
                .filter(Deployment.project_id == project.id, Deployment.server_id == server.id)
                .order_by(Deployment.started_at.desc())
                .first()
            )
    if not dep:
        # Silently ignore logs for unknown/external deployments instead of throwing a 404
        # which causes the agent to get stuck in an infinite retry loop.
        return {"detail": "Ignored"}
    if dep.server_id != server.id:
        raise HTTPException(403, "This deployment does not belong to your server")

    source = payload.source
    kwargs = {}
    if source == "actions":
        events = collectors.parse_actions_log(payload.text)
        events = collectors.extract_actions_failure_window(events)
    elif source == "docker":
        events = collectors.parse_docker_log(payload.text, container=payload.container, stream=payload.stream)
    elif source in ("nginx", "nginx_error"):
        events = collectors.parse_nginx_error_log(payload.text)
    elif source == "nginx_access":
        events = collectors.parse_nginx_access_log(payload.text)
    elif source == "system":
        events = collectors.parse_system_log(payload.text)
    else:
        events = collectors.parse("app", payload.text)

    normalized_source = {"nginx_error": "nginx", "nginx_access": "nginx"}.get(source, source)
    rows = [
        LogEvent(
            deployment_id=dep.id,
            project_id=dep.project_id,
            server_id=server.id,
            source=normalized_source,
            stream=e.get("stream", "stdout"),
            level=e["level"],
            ts=e["ts"],
            message=e["message"],
            meta=e.get("meta", {}),
        )
        for e in events[:4000]
    ]
    db.bulk_save_objects(rows)
    db.commit()
    return {"accepted": len(rows), "deployment_id": dep.id, "source": normalized_source}


@router.get("", response_model=list[DeploymentOut])
def list_deployments(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    q = db.query(Deployment)
    if project_id:
        q = q.filter(Deployment.project_id == project_id)
    if status_filter:
        q = q.filter(Deployment.status == status_filter)
    if user.role == Role.student.value:
        owned = [p.id for p in db.query(Project).filter(Project.owner_id == user.id).all()]
        q = q.filter(Deployment.project_id.in_(owned or ["-"]))
    return q.order_by(Deployment.started_at.desc()).limit(limit).all()


@router.get("/{deployment_id}/last-good")
def last_good_deployment(deployment_id: str, db: Session = Depends(get_db), server: Server = Depends(current_agent)):
    """Called by the rollback step of the CI workflow instead of guessing
    from local `docker images` output, which reflects Docker's own image
    listing order (newest build first, unrelated to which past deployment
    actually ran healthy) — not this project's deployment history. That
    history is exactly what this table already is.

    "Last good" means the most recent OTHER deployment of this same project
    that reported status=running — i.e. survived its own health check —
    not merely "the previous image that happened to exist locally."
    """
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "Deployment not found")
    if dep.server_id != server.id:
        # Matches the check submit_result already does for AgentCommand —
        # an agent's token proves which server it's calling from, not that
        # it may read or write every deployment in the system. Missing here
        # meant one server's agent could pull another server's last-known-
        # good image tag by guessing/enumerating a deployment id.
        raise HTTPException(403, "This deployment does not belong to your server")
    good = (
        db.query(Deployment)
        .filter(
            Deployment.project_id == dep.project_id,
            Deployment.id != dep.id,
            Deployment.status == DeploymentStatus.running.value,
            Deployment.image.isnot(None),
            Deployment.image != "",
        )
        .order_by(Deployment.started_at.desc())
        .first()
    )
    if not good:
        return {"found": False, "image": None, "deployment_id": None}
    return {"found": True, "image": good.image, "deployment_id": good.id, "started_at": good.started_at}


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(deployment_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "Deployment not found")
    require_project_access(user, db.get(Project, dep.project_id))
    return dep


@router.get("/{deployment_id}/timeline")
def get_timeline(deployment_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The correlated multi-source view — the thing that makes RCA explainable."""
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "Deployment not found")
    require_project_access(user, db.get(Project, dep.project_id))
    from ..rca.correlate import build_timeline

    tl = build_timeline(db, deployment_id)
    return {
        "anchor": tl.get("anchor"),
        "sources": tl.get("sources", []),
        "signals": tl.get("signals", []),
        "metrics": tl.get("metrics", {}),
        "total_raw_events": tl.get("total_raw_events", 0),
        "events": [
            {
                "id": e["id"],
                "ts": e["ts"].isoformat(),
                "source": e["source"],
                "level": e["level"],
                "count": e["count"],
                "message": e["message"][:2000],
            }
            for e in tl.get("events", [])
        ],
    }


@router.post("/{deployment_id}/analyze", status_code=201)
def analyze_now(deployment_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(404, "Deployment not found")
    require_project_access(user, db.get(Project, dep.project_id))
    incident = analyze_deployment(db, deployment_id)
    if not incident:
        raise HTTPException(422, "No log events are attached to this deployment yet, so there is nothing to analyze")
    audit(db, "deployment.analyze", actor=user.email, actor_role=user.role, target_type="deployment", target_id=deployment_id)
    return {"incident_id": incident.id, "confidence": incident.confidence, "root_cause": incident.root_cause}
