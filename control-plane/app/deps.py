from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuditLog, Project, Role, Server, User, utcnow
from .security import decode_token, verify_agent_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, payload.get("sub"))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def require_roles(*roles: Role):
    allowed = {r.value for r in roles}

    def _guard(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires one of: {', '.join(sorted(allowed))}",
            )
        return user

    return _guard


require_devops = require_roles(Role.admin, Role.devops)
require_mentor = require_roles(Role.admin, Role.devops, Role.mentor)


def current_agent(
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Server:
    """Authenticate a server agent, or the CI job running on that server.

    The self-hosted runner lives on the deployment server, so the same token
    identifies both. It is accepted from either header so the GitHub Actions
    workflow can use a plain `Authorization: Bearer` line.
    """
    token = x_agent_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
        if candidate.startswith("vops_"):
            token = candidate
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing agent token")
    for server in db.query(Server).all():
        if verify_agent_token(token, server.token_hash):
            # Deliberately does NOT refresh last_seen. The same token authenticates
            # CI jobs, and a running GitHub Actions job proves the *runner* is up,
            # not that the agent daemon is alive. Only the agent's own endpoints
            # (heartbeat / poll / result) mark the server online — otherwise a dead
            # agent looks healthy and approved fixes queue forever.
            return server
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown agent token")


def mark_agent_alive(db: Session, server: Server) -> None:
    """Call from endpoints only the agent daemon itself invokes."""
    server.last_seen = utcnow()
    db.commit()


def require_project_access(user: User, project: Project | None) -> None:
    """List endpoints (list_incidents, list_deployments, list_projects) all
    filter by `Project.owner_id == user.id` for students — but that filter
    only protects listing. A direct GET by id (get_incident, get_deployment,
    get_timeline, incident_trace, analyze_now, ...) loads the row first and
    was returning it to anyone with a valid token, regardless of whose
    project it belonged to, as long as they had (or could enumerate) the id.
    Call this right after loading the project for any such endpoint.

    Everyone other than a student (admin/devops/mentor) can see every
    project — this only narrows what a *student* can reach.
    """
    if user.role == Role.student.value and (not project or project.owner_id != user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this project")


def audit(
    db: Session,
    action: str,
    *,
    actor: str = "system",
    actor_role: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            action=action,
            actor=actor,
            actor_role=actor_role,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
        )
    )
    db.commit()
