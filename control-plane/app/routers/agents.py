from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import audit, current_agent, mark_agent_alive, require_devops
from ..infra import nginx as nginx_svc
from ..infra import ports as port_svc
from ..models import (
    AgentCommand,
    Deployment,
    FixAction,
    FixStatus,
    Incident,
    IncidentStatus,
    MetricSample,
    NginxConfig,
    Server,
    User,
    utcnow,
)
from ..observability import anomaly
from ..remediation.verify import start_verification
from ..schemas import CommandOut, CommandResult, HeartbeatIn, ServerCreate, ServerEnrolled, ServerOut
from ..security import hash_agent_token, new_agent_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents"])


def _server_out(s: Server) -> ServerOut:
    """Build explicitly: a freshly-inserted row has unset columns missing from __dict__."""
    return ServerOut(
        id=s.id,
        name=s.name,
        hostname=s.hostname,
        cpu_cores=s.cpu_cores,
        ram_mb=s.ram_mb,
        disk_gb=s.disk_gb,
        port_range_start=s.port_range_start,
        port_range_end=s.port_range_end,
        last_seen=s.last_seen,
        agent_version=s.agent_version,
        online=s.online,
    )


# --------------------------------------------------------------------------- #
# Enrollment (human, from the dashboard)
# --------------------------------------------------------------------------- #
@router.post("/servers", response_model=ServerEnrolled, status_code=201)
def enroll_server(payload: ServerCreate, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    if db.query(Server).filter(Server.name == payload.name).first():
        raise HTTPException(409, f"A server named '{payload.name}' is already enrolled")
    token = new_agent_token()
    server = Server(
        name=payload.name,
        hostname=payload.hostname,
        cpu_cores=payload.cpu_cores,
        ram_mb=payload.ram_mb,
        disk_gb=payload.disk_gb,
        port_range_start=payload.port_range_start or settings.port_range_start,
        port_range_end=payload.port_range_end or settings.port_range_end,
        token_hash=hash_agent_token(token),
    )
    db.add(server)
    db.commit()
    audit(db, "server.enroll", actor=user.email, actor_role=user.role, target_type="server", target_id=server.id, detail={"name": server.name})

    # The plaintext token is shown exactly once.
    return ServerEnrolled(**_server_out(server).model_dump(), agent_token=token)


@router.get("/servers", response_model=list[ServerOut])
def list_servers(db: Session = Depends(get_db), _: User = Depends(require_devops)):
    return [_server_out(s) for s in db.query(Server).all()]


@router.post("/servers/{server_id}/rotate-token", response_model=ServerEnrolled)
def rotate_token(server_id: str, db: Session = Depends(get_db), user: User = Depends(require_devops)):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Server not found")
    token = new_agent_token()
    server.token_hash = hash_agent_token(token)
    db.commit()
    audit(db, "server.rotate_token", actor=user.email, actor_role=user.role, target_type="server", target_id=server.id)
    return ServerEnrolled(**_server_out(server).model_dump(), agent_token=token)


# --------------------------------------------------------------------------- #
# Agent-facing
# --------------------------------------------------------------------------- #
@router.post("/heartbeat")
def heartbeat(payload: HeartbeatIn, db: Session = Depends(get_db), server: Server = Depends(current_agent)):
    server.agent_version = payload.agent_version or server.agent_version
    if payload.cpu_cores:
        server.cpu_cores = payload.cpu_cores
    if payload.ram_mb:
        server.ram_mb = payload.ram_mb
    if payload.disk_gb:
        server.disk_gb = payload.disk_gb
    mark_agent_alive(db, server)

    reconciliation = {}
    if payload.observed_ports:
        reconciliation = port_svc.reconcile(db, server.id, payload.observed_ports)

    touched: set[str] = set()
    rows = []
    for m in payload.metrics:
        dep_id = m.deployment_id
        if not dep_id and m.container_name:
            dep = (
                db.query(Deployment)
                .filter(Deployment.container_name == m.container_name, Deployment.server_id == server.id)
                .order_by(Deployment.started_at.desc())
                .first()
            )
            dep_id = dep.id if dep else None
        rows.append(
            MetricSample(
                deployment_id=dep_id,
                server_id=server.id,
                cpu_pct=m.cpu_pct,
                mem_mb=m.mem_mb,
                mem_limit_mb=m.mem_limit_mb,
                restarts=m.restarts,
                disk_pct=m.disk_pct or payload.disk_pct,
                net_rx_kb=m.net_rx_kb,
                net_tx_kb=m.net_tx_kb,
                http_5xx=m.http_5xx,
                http_p95_ms=m.http_p95_ms,
            )
        )
        if dep_id:
            touched.add(dep_id)
    if rows:
        db.bulk_save_objects(rows)
        db.commit()

    new_anomalies = []
    for dep_id in touched:
        try:
            findings = anomaly.detect(db, dep_id)
            for a in anomaly.persist(db, dep_id, findings):
                new_anomalies.append({"deployment_id": dep_id, "kind": a.kind, "severity": a.severity, "message": a.message})
        except Exception:
            log.exception("Anomaly detection failed for deployment %s", dep_id)

    pending = db.query(AgentCommand).filter(AgentCommand.server_id == server.id, AgentCommand.status == "pending").count()
    return {
        "ok": True,
        "server": server.name,
        "pending_commands": pending,
        "port_reconciliation": reconciliation,
        "anomalies_detected": new_anomalies,
        "poll_interval_s": 15,
    }


@router.get("/commands", response_model=list[CommandOut])
def poll_commands(limit: int = Query(5, le=20), db: Session = Depends(get_db), server: Server = Depends(current_agent)):
    """The agent's only source of work. If it isn't here, it doesn't happen."""
    mark_agent_alive(db, server)
    cmds = (
        db.query(AgentCommand)
        .filter(AgentCommand.server_id == server.id, AgentCommand.status == "pending")
        .order_by(AgentCommand.created_at.asc())
        .limit(limit)
        .all()
    )
    for c in cmds:
        c.status = "claimed"
        c.claimed_at = utcnow()
    db.commit()
    return [CommandOut(id=c.id, kind=c.kind, payload=c.payload, fix_action_id=c.fix_action_id) for c in cmds]


@router.post("/commands/result")
def submit_result(payload: CommandResult, db: Session = Depends(get_db), server: Server = Depends(current_agent)):
    mark_agent_alive(db, server)
    cmd = db.get(AgentCommand, payload.command_id)
    if not cmd or cmd.server_id != server.id:
        raise HTTPException(404, "Command not found for this server")

    cmd.status = "succeeded" if payload.ok else "failed"
    cmd.result = {"output": payload.output[-8000:], "error": payload.error[-4000:], **(payload.detail or {})}
    cmd.completed_at = utcnow()

    if cmd.kind == "apply_nginx_config":
        cfg_id = (cmd.payload or {}).get("config_id")
        if cfg_id:
            cfg = db.get(NginxConfig, cfg_id)
            if cfg:
                nginx_svc.mark_applied(db, cfg, {"ok": payload.ok, "nginx_test_output": payload.output, "error": payload.error})

    if cmd.kind == "health_check":
        # Deliberately routed by payload, not cmd.fix_action_id (which is
        # always None for these) — a verification probe must never be able
        # to participate in the fix-status aggregation below.
        verify_fix_id = (cmd.payload or {}).get("verify_fix_id")
        if verify_fix_id:
            vfix = db.get(FixAction, verify_fix_id)
            if vfix:
                vfix.verification_detail = {
                    **(vfix.verification_detail or {}),
                    "health_check": {
                        "ok": payload.ok,
                        "status_code": (payload.detail or {}).get("status_code"),
                        "checked_at": utcnow().isoformat(),
                    },
                }

    if cmd.fix_action_id:
        fix = db.get(FixAction, cmd.fix_action_id)
        if fix:
            siblings = db.query(AgentCommand).filter(AgentCommand.fix_action_id == fix.id).all()
            if any(c.status == "failed" for c in siblings):
                fix.status = FixStatus.failed.value
            elif all(c.status == "succeeded" for c in siblings):
                fix.status = FixStatus.succeeded.value
            else:
                fix.status = FixStatus.executing.value
            fix.executed_at = utcnow()
            fix.result = {**(fix.result or {}), cmd.kind: cmd.result}

            incident = db.get(Incident, fix.incident_id)
            if incident and fix.status == FixStatus.succeeded.value:
                # Applied, but not confirmed — a human still has to say it worked.
                incident.status = IncidentStatus.fixing.value
            elif incident and fix.status == FixStatus.failed.value:
                incident.status = IncidentStatus.diagnosed.value

    db.commit()

    # The command succeeding only proves the shell call returned 0. Hand off
    # to the Verification Agent to check the fix's actual `verify` criterion
    # over the watch window, rather than trusting the exit code alone.
    if cmd.fix_action_id:
        fix = db.get(FixAction, cmd.fix_action_id)
        if fix and fix.status == FixStatus.succeeded.value and fix.verification_status in ("not_applicable", None):
            start_verification(db, fix)
    audit(
        db, "agent.command_result", actor=server.name, actor_role="agent",
        target_type="agent_command", target_id=cmd.id,
        detail={"kind": cmd.kind, "ok": payload.ok, "error": payload.error[:300]},
    )
    return {"ok": True, "command_id": cmd.id, "status": cmd.status}


@router.get("/commands/history")
def command_history(
    server_id: str | None = None,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(require_devops),
):
    q = db.query(AgentCommand)
    if server_id:
        q = q.filter(AgentCommand.server_id == server_id)
    cmds = q.order_by(AgentCommand.created_at.desc()).limit(limit).all()
    return [
        {
            "id": c.id,
            "server_id": c.server_id,
            "kind": c.kind,
            "status": c.status,
            "fix_action_id": c.fix_action_id,
            "created_at": c.created_at.isoformat(),
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
            "result": {k: v for k, v in (c.result or {}).items() if k != "value"},
        }
        for c in cmds
    ]
