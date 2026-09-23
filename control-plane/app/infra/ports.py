"""Port registry.

Replaces "SSH in, run ss -tlnp, squint, pick a number". Allocation is a single
database transaction against a unique constraint, so two deployments racing on
the same server cannot be handed the same port — which is exactly how the
manual process fails.

The registry is also reconciled against what the agent actually observes on the
host, so a port grabbed outside the platform still gets recorded rather than
silently handed out twice.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import PortAllocation, Server, utcnow

log = logging.getLogger(__name__)

# Never hand these out even if they fall inside a configured range.
RESERVED = {22, 25, 80, 443, 3306, 5432, 5672, 6379, 8080, 9090, 9100, 11434, 27017}


class NoPortsAvailable(RuntimeError):
    pass


def allocate(
    db: Session,
    server_id: str,
    *,
    project_id: str | None = None,
    deployment_id: str | None = None,
    purpose: str = "app",
    preferred: int | None = None,
) -> PortAllocation:
    server = db.get(Server, server_id)
    if not server:
        raise ValueError(f"Unknown server {server_id}")

    # Reuse this project's existing allocation for the same purpose — a redeploy
    # should keep its port so the Nginx config doesn't need to change.
    if project_id:
        existing = (
            db.query(PortAllocation)
            .filter(
                PortAllocation.server_id == server_id,
                PortAllocation.project_id == project_id,
                PortAllocation.purpose == purpose,
                PortAllocation.status == "allocated",
            )
            .first()
        )
        if existing:
            existing.deployment_id = deployment_id or existing.deployment_id
            db.commit()
            return existing

    taken = {
        p.port
        for p in db.query(PortAllocation)
        .filter(PortAllocation.server_id == server_id, PortAllocation.status.in_(("allocated", "reserved")))
        .all()
    }

    lo, hi = server.port_range_start, server.port_range_end
    candidates: list[int] = []
    if preferred and lo <= preferred <= hi and preferred not in taken and preferred not in RESERVED:
        candidates.append(preferred)
    candidates += [p for p in range(lo, hi + 1) if p not in taken and p not in RESERVED]

    if not candidates:
        raise NoPortsAvailable(
            f"No free ports on {server.name} in range {lo}-{hi} "
            f"({len(taken)} allocated). Release stale allocations or widen the range."
        )

    for port in candidates[:50]:
        # A previously released port keeps its row (the unique constraint is on
        # server+port), so reviving it is how a freed port comes back into use.
        existing = (
            db.query(PortAllocation)
            .filter(PortAllocation.server_id == server_id, PortAllocation.port == port)
            .first()
        )
        if existing is not None:
            if existing.status != "released":
                continue  # taken since we built the candidate list
            existing.status = "allocated"
            existing.project_id = project_id
            existing.deployment_id = deployment_id
            existing.purpose = purpose
            existing.note = ""
            existing.allocated_at = utcnow()
            existing.released_at = None
            db.commit()
            log.info("Re-allocated port %s on %s for project %s", port, server.name, project_id)
            return existing

        alloc = PortAllocation(
            server_id=server_id,
            port=port,
            project_id=project_id,
            deployment_id=deployment_id,
            purpose=purpose,
            status="allocated",
        )
        db.add(alloc)
        try:
            db.commit()
            log.info("Allocated port %s on %s for project %s", port, server.name, project_id)
            return alloc
        except IntegrityError:
            # Another deployment won the race for this port. Try the next one.
            db.rollback()
            continue
    raise NoPortsAvailable(f"Could not acquire a port on {server.name} after 50 attempts")


def release(db: Session, server_id: str, port: int) -> bool:
    alloc = (
        db.query(PortAllocation)
        .filter(PortAllocation.server_id == server_id, PortAllocation.port == port, PortAllocation.status == "allocated")
        .first()
    )
    if not alloc:
        return False
    alloc.status = "released"
    alloc.released_at = utcnow()
    db.commit()
    return True


def release_for_project(db: Session, project_id: str) -> int:
    rows = db.query(PortAllocation).filter(PortAllocation.project_id == project_id, PortAllocation.status == "allocated").all()
    for r in rows:
        r.status = "released"
        r.released_at = utcnow()
    db.commit()
    return len(rows)


def reserve(db: Session, server_id: str, port: int, note: str) -> PortAllocation:
    """Record a port used by something outside the platform (nginx, a DB, ...)."""
    existing = db.query(PortAllocation).filter(PortAllocation.server_id == server_id, PortAllocation.port == port).first()
    if existing:
        existing.status = "reserved"
        existing.note = note
        existing.released_at = None
        db.commit()
        return existing
    alloc = PortAllocation(server_id=server_id, port=port, purpose="external", status="reserved", note=note)
    db.add(alloc)
    db.commit()
    return alloc


def reconcile(db: Session, server_id: str, observed_ports: list[dict]) -> dict:
    """Compare the registry against what the agent actually sees listening.

    observed_ports: [{"port": 3011, "process": "docker-proxy", "container": "team-x"}]
    """
    server = db.get(Server, server_id)
    if not server:
        return {"error": "unknown server"}

    registry = {
        p.port: p
        for p in db.query(PortAllocation).filter(PortAllocation.server_id == server_id, PortAllocation.status.in_(("allocated", "reserved"))).all()
    }
    observed = {int(o["port"]): o for o in observed_ports if str(o.get("port", "")).isdigit()}

    untracked, ghosts = [], []

    for port, info in observed.items():
        if port in registry or port in RESERVED:
            continue
        if server.port_range_start <= port <= server.port_range_end:
            reserve(db, server_id, port, f"detected outside platform: {info.get('process','?')} {info.get('container','')}".strip())
            untracked.append({"port": port, **info})

    for port, alloc in registry.items():
        if alloc.status == "allocated" and port not in observed:
            ghosts.append({"port": port, "project_id": alloc.project_id, "allocated_at": alloc.allocated_at.isoformat()})

    return {
        "server": server.name,
        "observed": len(observed),
        "registered": len(registry),
        "untracked_adopted": untracked,
        "possible_stale_allocations": ghosts,
        "free_in_range": max(
            0,
            (server.port_range_end - server.port_range_start + 1)
            - len({p for p in set(registry) | set(observed) if server.port_range_start <= p <= server.port_range_end})
        ),
    }


def usage(db: Session, server_id: str) -> dict:
    server = db.get(Server, server_id)
    if not server:
        return {}
    rows = db.query(PortAllocation).filter(PortAllocation.server_id == server_id).all()
    active = [r for r in rows if r.status == "allocated"]
    reserved = [r for r in rows if r.status == "reserved"]
    total = server.port_range_end - server.port_range_start + 1
    return {
        "server_id": server_id,
        "server": server.name,
        "range": [server.port_range_start, server.port_range_end],
        "capacity": total,
        "allocated": len(active),
        "reserved": len(reserved),
        "free": total - len(active) - len(reserved),
        "utilization_pct": round(100 * (len(active) + len(reserved)) / total, 1) if total else 0,
        "allocations": [
            {
                "port": r.port,
                "project_id": r.project_id,
                "deployment_id": r.deployment_id,
                "purpose": r.purpose,
                "status": r.status,
                "note": r.note,
                "allocated_at": r.allocated_at.isoformat(),
            }
            for r in sorted(rows, key=lambda x: x.port)
            if r.status in ("allocated", "reserved")
        ],
    }
