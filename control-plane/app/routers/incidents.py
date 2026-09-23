from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import audit, current_user, require_devops, require_project_access
from ..infra import nginx as nginx_svc
from ..infra import ports as port_svc
from ..models import (
    AgentCommand,
    Deployment,
    FixAction,
    FixStatus,
    Incident,
    IncidentStatus,
    Project,
    Risk,
    Role,
    Server,
    User,
    utcnow,
)
from ..rca.engine import learn_from_resolution
from ..rca.fixes import ACTION_CATALOG, spec_for
from ..remediation.verify import verify_now
from ..schemas import AgentTraceOut, ApproveFix, ConfirmIncident, FixActionOut, IncidentOut
from ..tracing import record as record_trace
from ..tracing import for_incident as trace_for_incident

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.get("", response_model=list[IncidentOut])
def list_incidents(
    project_id: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = None,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    q = db.query(Incident)
    if project_id:
        q = q.filter(Incident.project_id == project_id)
    if status_filter:
        q = q.filter(Incident.status == status_filter)
    if severity:
        q = q.filter(Incident.severity == severity)
    if user.role == Role.student.value:
        owned = [p.id for p in db.query(Project).filter(Project.owner_id == user.id).all()]
        q = q.filter(Incident.project_id.in_(owned or ["-"]))
    return q.order_by(Incident.created_at.desc()).limit(limit).all()


@router.get("/{incident_id}")
def get_incident(incident_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    project = db.get(Project, incident.project_id)
    require_project_access(user, project)
    fixes = (
        db.query(FixAction)
        .filter(FixAction.incident_id == incident_id)
        .order_by(FixAction.order_index.asc())
        .all()
    )
    dep = db.get(Deployment, incident.deployment_id) if incident.deployment_id else None

    return {
        "incident": IncidentOut.model_validate(incident).model_dump(),
        "project": {"id": project.id, "name": project.name, "slug": project.slug} if project else None,
        "deployment": {
            "id": dep.id,
            "commit_sha": dep.commit_sha[:8],
            "branch": dep.branch,
            "status": dep.status,
            "port": dep.port,
            "gh_run_url": dep.gh_run_url,
        }
        if dep
        else None,
        "fixes": [
            {
                **FixActionOut.model_validate(f).model_dump(),
                "action_spec": {
                    "description": spec_for(f.action_type).description,
                    "executable": spec_for(f.action_type).executable,
                    "reversible": spec_for(f.action_type).reversible,
                    "never_auto": spec_for(f.action_type).never_auto,
                    "verify": spec_for(f.action_type).verify,
                    "immediate_verify": spec_for(f.action_type).immediate_verify,
                    "scope": spec_for(f.action_type).scope,
                },
            }
            for f in fixes
        ],
        "auto_remediation_enabled": settings.auto_remediation,
    }


# --------------------------------------------------------------------------- #
# The approval gate
# --------------------------------------------------------------------------- #
@router.post("/fixes/{fix_id}/approve")
def approve_fix(
    fix_id: str,
    payload: ApproveFix,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    """Approve a proposed fix. This is the only path to execution.

    Nothing the AI proposes touches a server until a named human calls this
    endpoint, and the approver's identity is recorded on the action and in the
    audit log.
    """
    fix = db.get(FixAction, fix_id)
    if not fix:
        raise HTTPException(404, "Fix action not found")
    if fix.status != FixStatus.proposed.value:
        raise HTTPException(409, f"This fix is already '{fix.status}' and cannot be approved again")

    spec = spec_for(fix.action_type)
    if not spec.executable:
        raise HTTPException(
            400,
            f"'{fix.action_type}' is advisory — it describes a change the team must make in their "
            "repository. There is nothing for the platform to execute.",
        )
    if spec.risk == Risk.dangerous and user.role != Role.admin.value and user.role != Role.devops.value:
        raise HTTPException(403, "Only DevOps engineers or admins may approve a dangerous action")
    if spec.scope == "shared" and not payload.params.get("confirm_shared"):
        raise HTTPException(
            400,
            f"'{fix.action_type}' can affect infrastructure shared across projects, not just this one. "
            "Re-submit with params.confirm_shared=true to acknowledge the wider blast radius.",
        )

    incident = db.get(Incident, fix.incident_id)
    project = db.get(Project, incident.project_id)
    server_id = project.server_id if project else None
    if not server_id:
        dep = db.get(Deployment, incident.deployment_id) if incident.deployment_id else None
        server_id = dep.server_id if dep else None
    if not server_id:
        raise HTTPException(409, "This project is not bound to a server, so no agent can execute the fix")

    server = db.get(Server, server_id)
    if not server or not server.online:
        raise HTTPException(
            503,
            f"The agent on '{getattr(server, 'name', server_id)}' has not checked in recently, so the fix "
            "cannot be dispatched. It will need to be applied manually or retried when the agent is back.",
        )

    params = {**(fix.params or {}), **(payload.params or {})}
    fix.params = params
    fix.status = FixStatus.approved.value
    fix.approved_by = user.id
    fix.approved_at = utcnow()

    commands = _build_commands(db, incident, fix, server, params)
    for cmd in commands:
        db.add(cmd)

    incident.status = IncidentStatus.fixing.value
    db.commit()

    audit(
        db, "fix.approve", actor=user.email, actor_role=user.role,
        target_type="fix_action", target_id=fix.id,
        detail={
            "action_type": fix.action_type,
            "risk": fix.risk,
            "incident_id": incident.id,
            "project": getattr(project, "slug", ""),
            "note": payload.note,
            "commands": [c.kind for c in commands],
        },
    )
    record_trace(
        db, "remediation",
        f"{user.email} approved '{fix.title}' ({fix.action_type}). Dispatched {len(commands)} command(s) to {server.name}.",
        project_id=project.id if project else None,
        incident_id=incident.id,
        fix_action_id=fix.id,
        outcome="info",
        detail={"approved_by": user.email, "commands": [c.kind for c in commands]},
    )
    return {
        "fix_id": fix.id,
        "status": fix.status,
        "dispatched_commands": [{"id": c.id, "kind": c.kind} for c in commands],
        "approved_by": user.email,
    }


@router.post("/fixes/{fix_id}/reject")
def reject_fix(fix_id: str, payload: ApproveFix, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    fix = db.get(FixAction, fix_id)
    if not fix:
        raise HTTPException(404, "Fix action not found")
    fix.status = FixStatus.rejected.value
    fix.approved_by = user.id
    fix.approved_at = utcnow()
    fix.result = {"rejected_reason": payload.note}
    db.commit()
    audit(
        db, "fix.reject", actor=user.email, actor_role=user.role,
        target_type="fix_action", target_id=fix.id,
        detail={"action_type": fix.action_type, "note": payload.note},
    )
    return {"fix_id": fix.id, "status": fix.status}


def _build_commands(db: Session, incident: Incident, fix: FixAction, server: Server, params: dict) -> list[AgentCommand]:
    """Translate an approved fix into concrete agent commands.

    The agent is deliberately dumb: it receives exact instructions, not intent.
    All decision-making lives here, where it can be audited.
    """
    dep = db.get(Deployment, incident.deployment_id) if incident.deployment_id else None
    project = db.get(Project, incident.project_id)
    container = params.get("container") or (dep.container_name if dep else project.slug)
    cmds: list[AgentCommand] = []

    def cmd(kind: str, payload: dict) -> AgentCommand:
        return AgentCommand(server_id=server.id, fix_action_id=fix.id, kind=kind, payload=payload)

    at = fix.action_type

    if at == "reassign_port":
        old_port = dep.port if dep else None
        if old_port:
            port_svc.release(db, server.id, old_port)
        alloc = port_svc.allocate(db, server.id, project_id=project.id, deployment_id=dep.id if dep else None)
        if dep:
            dep.port = alloc.port
        cmds.append(cmd("recreate_container", {"container": container, "host_port": alloc.port, "previous_port": old_port}))
        if project.domain:
            cfg = nginx_svc.create_config(db, project, server, domain=project.domain, upstream_port=alloc.port)
            cmds.append(cmd("apply_nginx_config", nginx_svc.apply_payload(cfg, project)))
        fix.result = {"new_port": alloc.port, "previous_port": old_port}

    elif at == "apply_nginx_config":
        config_id = params.get("config_id")
        cfg = None
        if config_id:
            from ..models import NginxConfig

            cfg = db.get(NginxConfig, config_id)
        if not cfg:
            if not project.domain or not (dep and dep.port):
                raise HTTPException(409, "Cannot regenerate the Nginx config: the project has no domain or no allocated port")
            options = {k: v for k, v in params.items() if k not in {"config_id"}}
            cfg = nginx_svc.create_config(db, project, server, domain=project.domain, upstream_port=dep.port, options=options)
        cmds.append(cmd("apply_nginx_config", nginx_svc.apply_payload(cfg, project)))

    elif at == "set_env_var":
        key = params.get("key")
        if not key:
            raise HTTPException(400, "An environment variable name is required")
        cmds.append(
            cmd(
                "set_env_var",
                {
                    "container": container,
                    "project_slug": project.slug,
                    "key": key,
                    "value": params.get("value", ""),
                    "recreate": True,
                },
            )
        )
        # Never persist the value in the fix record.
        fix.params = {**params, "value": "***stored***"}

    elif at == "set_memory_limit":
        cmds.append(cmd("set_memory_limit", {"container": container, "memory_mb": int(params.get("memory_mb") or 1024)}))

    elif at == "rollback_deployment":
        previous = (
            db.query(Deployment)
            .filter(
                Deployment.project_id == project.id,
                Deployment.status == "running",
                Deployment.id != (dep.id if dep else ""),
            )
            .order_by(Deployment.started_at.desc())
            .first()
        )
        if not previous or not previous.image:
            raise HTTPException(409, "No previous healthy image is recorded for this project, so there is nothing to roll back to")
        cmds.append(cmd("rollback_deployment", {"container": container, "previous_image": previous.image, "host_port": previous.port}))

    elif at == "run_migration":
        cmds.append(cmd("run_migration", {"container": container, "command": params.get("command") or "alembic upgrade head"}))

    elif at in {"restart_container", "recreate_container", "prune_logs", "stop_conflicting_container"}:
        cmds.append(cmd(at, {"container": container, **{k: v for k, v in params.items() if k != "container"}}))

    elif at in {"reload_nginx", "prune_docker"}:
        cmds.append(cmd(at, dict(params)))

    elif at == "rebuild_image":
        cmds.append(cmd("rebuild_image", {"project_slug": project.slug, "image": params.get("image") or f"{project.slug}:latest", **params}))

    else:
        raise HTTPException(400, f"'{at}' has no execution path — it is advisory only")

    return cmds


# --------------------------------------------------------------------------- #
# Human confirmation — this is what builds the training set
# --------------------------------------------------------------------------- #
@router.post("/{incident_id}/confirm", response_model=IncidentOut)
def confirm_incident(
    incident_id: str,
    payload: ConfirmIncident,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    """Record what the cause actually was.

    Every confirmation does two things: it closes the incident, and it becomes a
    labelled example. Over a semester this turns real incidents in this specific
    environment into an evaluation and fine-tuning set that no general model has.
    """
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    incident.confirmed_root_cause = payload.confirmed_root_cause
    incident.confirmed_fix = payload.confirmed_fix
    incident.was_ai_correct = payload.was_ai_correct
    incident.confirmed_by = user.id
    incident.status = IncidentStatus.resolved.value
    incident.resolved_at = utcnow()
    db.commit()

    if payload.add_to_knowledge_base:
        learn_from_resolution(db, incident)

    audit(
        db, "incident.confirm", actor=user.email, actor_role=user.role,
        target_type="incident", target_id=incident.id,
        detail={"was_ai_correct": payload.was_ai_correct, "signature": incident.signature_key},
    )
    return incident


@router.post("/{incident_id}/dismiss", response_model=IncidentOut)
def dismiss_incident(incident_id: str, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    incident.status = IncidentStatus.dismissed.value
    incident.resolved_at = utcnow()
    db.commit()
    audit(db, "incident.dismiss", actor=user.email, actor_role=user.role, target_type="incident", target_id=incident.id)
    return incident


@router.get("/{incident_id}/trace")
def incident_trace(incident_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The full agent lifecycle for this incident, in order: Repository Agent
    (if a readiness analysis preceded it), Incident Investigator, Remediation
    Agent (one line per approval), Verification Agent (one line per fix)."""
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    require_project_access(user, db.get(Project, incident.project_id))
    rows = trace_for_incident(db, incident_id)
    return [AgentTraceOut.model_validate(r).model_dump() for r in rows]


@router.post("/fixes/{fix_id}/verify")
def force_verify(fix_id: str, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    """Resolve a pending verification now instead of waiting for the next
    sweep — useful right after applying a fix, or in a demo."""
    fix = db.get(FixAction, fix_id)
    if not fix:
        raise HTTPException(404, "Fix action not found")
    if fix.verification_status != "pending":
        return {
            "fix_id": fix.id,
            "verification_status": fix.verification_status,
            "note": "Nothing to force — this fix is not currently pending verification.",
        }
    verify_now(db, fix_id)
    db.refresh(fix)
    audit(
        db, "fix.force_verify", actor=user.email, actor_role=user.role,
        target_type="fix_action", target_id=fix.id,
        detail={"result": fix.verification_status},
    )
    return {
        "fix_id": fix.id,
        "verification_status": fix.verification_status,
        "verification_detail": fix.verification_detail,
    }


@router.get("/meta/actions")
def list_action_catalog(_: User = Depends(current_user)):
    """The complete whitelist — what this system is capable of doing, and nothing more."""
    return [
        {
            "action_type": s.key,
            "title": s.title,
            "description": s.description,
            "risk": s.risk.value,
            "executable": s.executable,
            "reversible": s.reversible,
            "never_auto": s.never_auto,
            "params": list(s.params),
            "verification": s.verify,
            "immediate_verify": s.immediate_verify,
            "scope": s.scope,
        }
        for s in ACTION_CATALOG.values()
    ]


@router.get("/meta/accuracy")
def ai_accuracy(db: Session = Depends(get_db), _: User = Depends(require_devops)):
    """Honest scoreboard: how often was the AI's first diagnosis right?"""
    confirmed = db.query(Incident).filter(Incident.was_ai_correct.isnot(None)).all()
    total = len(confirmed)
    correct = len([i for i in confirmed if i.was_ai_correct])
    by_source: dict[str, dict] = {}
    for i in confirmed:
        b = by_source.setdefault(i.analysis_source or "unknown", {"total": 0, "correct": 0})
        b["total"] += 1
        b["correct"] += 1 if i.was_ai_correct else 0
    by_sig: dict[str, dict] = {}
    for i in confirmed:
        key = i.signature_key or "novel"
        b = by_sig.setdefault(key, {"total": 0, "correct": 0})
        b["total"] += 1
        b["correct"] += 1 if i.was_ai_correct else 0
    return {
        "confirmed_incidents": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else None,
        "by_analysis_source": {
            k: {**v, "accuracy": round(v["correct"] / v["total"], 3)} for k, v in by_source.items()
        },
        "by_signature": {
            k: {**v, "accuracy": round(v["correct"] / v["total"], 3)}
            for k, v in sorted(by_sig.items(), key=lambda kv: kv[1]["total"], reverse=True)[:20]
        },
        "note": "Accuracy is measured only on incidents a human explicitly confirmed.",
    }
