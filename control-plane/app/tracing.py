"""Narration for the four-agent lifecycle.

The design doc frames Layer 1/2/3 as four cooperating agents:

    Repository Agent -> Incident Investigator -> Remediation Agent -> Verification Agent

That pipeline already exists as real code (analyzers/, rca/engine.py,
routers/incidents.py, remediation/verify.py) — it just had no shared,
readable trail a person could open on one incident and see the whole
lifecycle. `record()` is that trail: every stage writes one line here as it
runs, and `GET /api/incidents/{id}/trace` (and the repo-analysis equivalent)
reads it back in order.

This is deliberately not a framework. There is no agent base class, no tool
registry, no planner. Each "agent" is just the existing function for that
stage, plus one call to `record()` before it returns.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AgentTrace

STAGES = ("repository", "investigator", "remediation", "verification")


def record(
    db: Session,
    stage: str,
    summary: str,
    *,
    project_id: str | None = None,
    incident_id: str | None = None,
    fix_action_id: str | None = None,
    outcome: str = "info",
    detail: dict | None = None,
    commit: bool = True,
) -> AgentTrace:
    assert stage in STAGES, f"unknown pipeline stage '{stage}'"
    row = AgentTrace(
        project_id=project_id,
        incident_id=incident_id,
        fix_action_id=fix_action_id,
        stage=stage,
        outcome=outcome,
        summary=summary[:2000],
        detail=detail or {},
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def for_incident(db: Session, incident_id: str) -> list[AgentTrace]:
    return (
        db.query(AgentTrace)
        .filter(AgentTrace.incident_id == incident_id)
        .order_by(AgentTrace.created_at.asc())
        .all()
    )


def for_project(db: Session, project_id: str, *, limit: int = 100) -> list[AgentTrace]:
    return (
        db.query(AgentTrace)
        .filter(AgentTrace.project_id == project_id)
        .order_by(AgentTrace.created_at.desc())
        .limit(limit)
        .all()
    )
