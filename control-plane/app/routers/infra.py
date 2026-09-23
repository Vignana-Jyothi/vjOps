from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import audit, current_user, require_devops
from ..infra import nginx as nginx_svc
from ..infra import ports as port_svc
from ..models import AgentCommand, Deployment, NginxConfig, Project, Server, User
from ..schemas import NginxRequest, PortAllocateRequest

router = APIRouter(prefix="/api/infra", tags=["infrastructure"])


# --------------------------------- ports ---------------------------------- #
@router.get("/ports/{server_id}")
def port_usage(server_id: str, db: Session = Depends(get_db), _: User = Depends(current_user)):
    data = port_svc.usage(db, server_id)
    if not data:
        raise HTTPException(404, "Server not found")
    return data


@router.post("/ports/allocate")
def allocate_port(payload: PortAllocateRequest, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    try:
        alloc = port_svc.allocate(
            db,
            payload.server_id,
            project_id=payload.project_id,
            deployment_id=payload.deployment_id,
            purpose=payload.purpose,
            preferred=payload.preferred,
        )
    except port_svc.NoPortsAvailable as exc:
        raise HTTPException(507, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    audit(db, "port.allocate", actor=user.email, actor_role=user.role, target_type="port", target_id=str(alloc.port), detail={"server": payload.server_id, "project": payload.project_id})
    return {"port": alloc.port, "server_id": alloc.server_id, "project_id": alloc.project_id, "status": alloc.status}


@router.post("/ports/{server_id}/{port}/release")
def release_port(server_id: str, port: int, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    if not port_svc.release(db, server_id, port):
        raise HTTPException(404, "No active allocation for that port")
    audit(db, "port.release", actor=user.email, actor_role=user.role, target_type="port", target_id=str(port))
    return {"released": port}


# --------------------------------- nginx ---------------------------------- #
@router.post("/nginx/preview")
def preview_nginx(payload: NginxRequest, db: Session = Depends(get_db), _: User = Depends(require_devops)):
    """Render without saving — lets a DevOps engineer read the config first."""
    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    port = payload.upstream_port
    if not port:
        dep = (
            db.query(Deployment)
            .filter(Deployment.project_id == project.id, Deployment.port.isnot(None))
            .order_by(Deployment.started_at.desc())
            .first()
        )
        port = dep.port if dep else None
    if not port:
        raise HTTPException(400, "No upstream port supplied and none could be inferred from recent deployments")

    text = nginx_svc.render(project, domain=payload.domain, upstream_port=port, options=payload.options)
    return {"rendered": text, "lint": nginx_svc.lint(text), "upstream_port": port}


@router.post("/nginx", status_code=201)
def create_nginx_config(payload: NginxRequest, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    server_id = payload.server_id or project.server_id
    server = db.get(Server, server_id) if server_id else None
    if not server:
        raise HTTPException(400, "This project is not bound to a server")

    port = payload.upstream_port
    if not port:
        dep = (
            db.query(Deployment)
            .filter(Deployment.project_id == project.id, Deployment.port.isnot(None))
            .order_by(Deployment.started_at.desc())
            .first()
        )
        port = dep.port if dep else None
    if not port:
        raise HTTPException(400, "No upstream port supplied and none could be inferred")

    try:
        cfg = nginx_svc.create_config(db, project, server, domain=payload.domain, upstream_port=port, options=payload.options)
    except nginx_svc.NginxError as exc:
        raise HTTPException(400, str(exc))

    if not project.domain:
        project.domain = cfg.domain
        db.commit()

    audit(db, "nginx.create", actor=user.email, actor_role=user.role, target_type="nginx_config", target_id=cfg.id, detail={"domain": cfg.domain, "port": port})
    return {"id": cfg.id, "domain": cfg.domain, "upstream_port": cfg.upstream_port, "version": cfg.version, "status": cfg.status, "rendered": cfg.rendered}


@router.post("/nginx/{config_id}/apply")
def apply_nginx_config(config_id: str, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    """Dispatch the apply. The agent validates with `nginx -t` and rolls back on failure."""
    cfg = db.get(NginxConfig, config_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    if cfg.status == "failed":
        raise HTTPException(409, f"This config failed validation and will not be applied:\n{cfg.validation_output}")
    project = db.get(Project, cfg.project_id)
    server = db.get(Server, cfg.server_id)
    if not server or not server.online:
        raise HTTPException(503, "The agent on that server is not currently checked in")

    cmd = AgentCommand(server_id=server.id, kind="apply_nginx_config", payload=nginx_svc.apply_payload(cfg, project))
    db.add(cmd)
    db.commit()
    audit(db, "nginx.apply", actor=user.email, actor_role=user.role, target_type="nginx_config", target_id=cfg.id)
    return {"command_id": cmd.id, "config_id": cfg.id, "status": "dispatched"}


@router.get("/nginx")
def list_nginx_configs(project_id: str | None = None, db: Session = Depends(get_db), _: User = Depends(require_devops)):
    q = db.query(NginxConfig)
    if project_id:
        q = q.filter(NginxConfig.project_id == project_id)
    return [
        {
            "id": c.id,
            "project_id": c.project_id,
            "domain": c.domain,
            "upstream_port": c.upstream_port,
            "version": c.version,
            "status": c.status,
            "validation_output": c.validation_output[:500],
            "created_at": c.created_at.isoformat(),
            "applied_at": c.applied_at.isoformat() if c.applied_at else None,
        }
        for c in q.order_by(NginxConfig.created_at.desc()).limit(200).all()
    ]


@router.get("/nginx/{config_id}")
def get_nginx_config(config_id: str, db: Session = Depends(get_db), _: User = Depends(require_devops)):
    cfg = db.get(NginxConfig, config_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    return {
        "id": cfg.id,
        "project_id": cfg.project_id,
        "domain": cfg.domain,
        "upstream_port": cfg.upstream_port,
        "version": cfg.version,
        "status": cfg.status,
        "rendered": cfg.rendered,
        "validation_output": cfg.validation_output,
    }
