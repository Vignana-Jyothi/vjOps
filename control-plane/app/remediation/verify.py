"""The Verification Agent — Phase 4's missing last step.

Every ActionSpec in `rca/fixes.py` declares a `verify` criterion
("no OOM kill for 15 min", "nginx -t passes", ...), and now also *how* to
check it: `immediate_verify` (the command's own exit code is the answer) or
`watch_kinds` + `grace_minutes` (watch telemetry for a while). Before this
module existed, none of that was acted on — a fix was marked `succeeded` the
moment the agent's shell command returned exit 0, which only proves the
command ran, not that it fixed anything. A `docker update --memory` can
succeed and the container can still get OOM-killed ten minutes later.

Two kinds of evidence feed the decision:

  * Absence-of-recurrence — no watched Anomaly, no fresh deployment failure,
    no restart-count regression since the fix executed. This is what the
    first version of this module did on its own, and it is a real signal,
    but it only proves nothing *bad* happened, not that anything is
    confirmed *good*.
  * An affirmative health check — `start_verification` dispatches one
    `health_check` agent command (a plain HTTP GET against the project's
    health endpoint) right after the fix executes. If it comes back
    non-2xx, that is direct evidence the app is unhealthy regardless of
    whether an anomaly detector has caught up yet. This is a single
    snapshot taken shortly after the fix, not continuous polling — treat it
    as a supplementary signal, not a replacement for real uptime monitoring.

    apply (agent command exits 0)
        -> pending           (start_verification), one health_check dispatched
    watch metrics/anomalies/health-check result for the grace window
        -> passed            no recurrence observed, window elapsed
        -> failed            a matching anomaly, a failed health check, or a
                              fresh failed deployment appeared since the fix
                              executed -> incident reopens, an escalation fix
                              is proposed, and if the fix captured the
                              previous value and is marked reversible, a
                              rollback fix is proposed too (still requires
                              human approval, same as any other fix)
        -> unknown            window elapsed with zero telemetry from the
                              agent -> surfaced to a human rather than
                              silently assumed fine

`run_verification_sweep` is meant to be called periodically (Celery beat,
see workers/tasks.py) and is also exposed as a manual per-fix check so a
DevOps engineer (or a test) doesn't have to wait for the beat schedule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AgentCommand,
    Anomaly,
    Deployment,
    DeploymentStatus,
    FixAction,
    FixStatus,
    Incident,
    IncidentStatus,
    MetricSample,
    Project,
    Risk,
    utcnow,
)
from ..rca.fixes import spec_for
from ..tracing import record as record_trace

log = logging.getLogger(__name__)

# Fields inside a fix's own `result` blob (keyed by command kind — see
# submit_result) that hold the value from *before* the change, for actions
# where the agent handler captures one. Used to build a same-action rollback
# proposal if verification fails and the ActionSpec says it's reversible.
PREVIOUS_VALUE_FIELD = {
    "set_memory_limit": ("memory_mb", "previous_memory_mb"),
}


def _aware(dt):
    """SQLite (used in tests and small deployments) drops tzinfo on
    round-trip; Postgres keeps it. Normalize before comparing, the same way
    `Server.online` already does."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def start_verification(db: Session, fix: FixAction) -> None:
    """Called once a fix's agent commands have all succeeded.

    Immediate-verification actions resolve right here. Everything else goes
    to `pending` with a baseline snapshot (and a dispatched health check) so
    the sweep can tell "new" from "was already like this".
    """
    spec = spec_for(fix.action_type)
    if not spec.verify:
        fix.verification_status = "not_applicable"
        db.commit()
        return

    if spec.immediate_verify:
        # The command already succeeded (that's why we're here) — for these
        # actions that *is* the verify criterion.
        fix.verification_status = "passed"
        fix.verified_at = utcnow()
        fix.verification_detail = {"basis": "command result", "criterion": spec.verify}
        db.commit()
        incident = db.get(Incident, fix.incident_id)
        record_trace(
            db, "verification",
            f"'{spec.title}' verified immediately from the command result ({spec.verify}).",
            project_id=incident.project_id if incident else None,
            incident_id=fix.incident_id,
            fix_action_id=fix.id,
            outcome="pass",
        )
        return

    baseline_restarts = _latest_restarts(db, fix)
    fix.verification_status = "pending"
    fix.verified_at = None
    fix.verification_detail = {
        "criterion": spec.verify,
        "grace_minutes": spec.grace_minutes,
        "watch_kinds": list(spec.watch_kinds),
        "baseline_restarts": baseline_restarts,
        "extensions_used": 0,
        "started_at": utcnow().isoformat(),
    }
    health_cmd_id = _dispatch_health_check(db, fix)
    if health_cmd_id:
        fix.verification_detail["health_check_command_id"] = health_cmd_id
    db.commit()

    incident = db.get(Incident, fix.incident_id)
    record_trace(
        db, "verification",
        f"Watching '{spec.title}' for {spec.grace_minutes} min against: {spec.verify}."
        + (" A health check was dispatched." if health_cmd_id else ""),
        project_id=incident.project_id if incident else None,
        incident_id=fix.incident_id,
        fix_action_id=fix.id,
        outcome="info",
    )


def _dispatch_health_check(db: Session, fix: FixAction) -> str | None:
    """Fire one affirmative HTTP health check at the project's own endpoint,
    if we know a port to hit. This is Level 2 ("is the app actually
    healthy?") sitting alongside the Level 1 recurrence checks below — an
    absence of bad anomalies is not the same as confirmed-good.

    Best-effort: if there's no deployment/port on record, or the agent never
    answers, verification still falls back to the anomaly/restart signals.
    """
    incident = db.get(Incident, fix.incident_id)
    if not incident or not incident.deployment_id:
        return None
    dep = db.get(Deployment, incident.deployment_id)
    if not dep or not dep.server_id or not dep.port:
        return None
    project = db.get(Project, dep.project_id)
    health_path = (getattr(project, "health_path", "") or "/health") if project else "/health"

    cmd = AgentCommand(
        server_id=dep.server_id,
        fix_action_id=None,  # deliberately not tied to the fix's own command
        # aggregation in submit_result — a verification check must never be
        # able to flip the fix's execution status.
        kind="health_check",
        payload={"port": dep.port, "path": health_path, "verify_fix_id": fix.id},
    )
    db.add(cmd)
    db.flush()
    return cmd.id


def _latest_restarts(db: Session, fix: FixAction) -> int | None:
    incident = db.get(Incident, fix.incident_id)
    if not incident or not incident.deployment_id:
        return None
    row = (
        db.query(MetricSample)
        .filter(MetricSample.deployment_id == incident.deployment_id)
        .order_by(MetricSample.ts.desc())
        .first()
    )
    return int(row.restarts) if row else None


def run_verification_sweep(db: Session) -> dict:
    """Resolve every fix whose grace window has elapsed. Safe to call often —
    fixes not yet due are left untouched."""
    pending = db.query(FixAction).filter(FixAction.verification_status == "pending").all()
    resolved = {"passed": 0, "failed": 0, "unknown": 0, "still_waiting": 0}
    for fix in pending:
        outcome = _evaluate_one(db, fix)
        if outcome:
            resolved[outcome] = resolved.get(outcome, 0) + 1
        else:
            resolved["still_waiting"] += 1
    return resolved


def verify_now(db: Session, fix_id: str) -> FixAction | None:
    """Force-resolve one fix regardless of whether its grace window has
    technically elapsed yet — used by the manual API endpoint."""
    fix = db.get(FixAction, fix_id)
    if not fix or fix.verification_status != "pending":
        return fix
    _evaluate_one(db, fix, force=True)
    return fix


def _evaluate_one(db: Session, fix: FixAction, *, force: bool = False) -> str | None:
    detail = fix.verification_detail or {}
    started_at = detail.get("started_at")
    grace = detail.get("grace_minutes", settings.verify_grace_minutes)
    if not started_at:
        return None

    started = datetime.fromisoformat(started_at)
    due = started + timedelta(minutes=grace)

    # A failed health check is decisive evidence and doesn't need to wait out
    # the window — an app returning 500s right now is not made healthier by
    # watching it for three more minutes.
    health = detail.get("health_check")
    health_failed = bool(health) and health.get("ok") is False

    if not force and not health_failed and utcnow() < due:
        return None  # window still open, and nothing negative found yet below

    incident = db.get(Incident, fix.incident_id)
    if not incident:
        return None
    spec = spec_for(fix.action_type)

    # 1. Did a watched anomaly re-appear on this deployment since the fix ran?
    watch_kinds = detail.get("watch_kinds") or list(spec.watch_kinds)
    bad_anomalies = []
    if incident.deployment_id and watch_kinds:
        candidates = (
            db.query(Anomaly)
            .filter(Anomaly.deployment_id == incident.deployment_id, Anomaly.kind.in_(watch_kinds))
            .all()
        )
        bad_anomalies = [a for a in candidates if _aware(a.detected_at) >= started]

    # 2. Did a *different, later* deployment of this project fail since the
    #    fix ran? (Remediation acts on the existing deployment in place —
    #    there is no "back to healthy" transition on that same row — so the
    #    only trustworthy "it broke again" signal from Deployment itself is
    #    a *new* deployment attempt failing, not the stale status of the one
    #    that caused the incident in the first place.)
    fresh_failure = None
    if incident.deployment_id:
        dep = db.get(Deployment, incident.deployment_id)
        project_id = dep.project_id if dep else incident.project_id
        fresh_failure = (
            db.query(Deployment)
            .filter(
                Deployment.project_id == project_id,
                Deployment.id != incident.deployment_id,
                Deployment.status == DeploymentStatus.failed.value,
                Deployment.started_at >= started.replace(tzinfo=None),
            )
            .first()
        )

    # 3. Restart-sensitive actions: did restarts climb past the baseline?
    restart_regression = False
    if fix.action_type in ("restart_container", "recreate_container", "rollback_deployment", "set_env_var"):
        latest = _latest_restarts(db, fix)
        baseline = detail.get("baseline_restarts")
        if latest is not None and baseline is not None and latest > baseline + 1:
            restart_regression = True

    if bad_anomalies or fresh_failure or restart_regression or health_failed:
        return _fail(db, fix, incident, spec, bad_anomalies, fresh_failure, restart_regression, health)

    # No telemetry at all for this deployment since the fix ran — give it one
    # extension before calling it "unknown" rather than confidently "passed".
    has_any_sample = incident.deployment_id and any(
        _aware(s.ts) >= started
        for s in db.query(MetricSample).filter(MetricSample.deployment_id == incident.deployment_id).all()
    )
    if not has_any_sample:
        if not force:
            extensions = detail.get("extensions_used", 0)
            if extensions < settings.verify_max_extensions:
                detail["extensions_used"] = extensions + 1
                detail["started_at"] = utcnow().isoformat()  # restart the clock once
                fix.verification_detail = detail
                db.commit()
                return None
        return _unknown(db, fix, incident, spec)

    return _pass(db, fix, incident, spec, health)


def _pass(db: Session, fix: FixAction, incident: Incident, spec, health: dict | None) -> str:
    fix.verification_status = "passed"
    fix.verified_at = utcnow()
    fix.verification_detail = {**(fix.verification_detail or {}), "resolved": "no recurrence observed"}
    incident.auto_verified = True
    db.commit()
    health_note = ""
    if health and health.get("ok"):
        health_note = f" Health check also returned {health.get('status_code', 'OK')}."
    record_trace(
        db, "verification",
        f"'{spec.title}' held for the full watch window — {spec.verify}. No recurrence observed.{health_note}",
        project_id=incident.project_id, incident_id=incident.id, fix_action_id=fix.id, outcome="pass",
    )
    return "passed"


def _fail(
    db: Session, fix: FixAction, incident: Incident, spec, bad_anomalies, fresh_failure, restart_regression,
    health: dict | None,
) -> str:
    evidence = []
    for a in bad_anomalies:
        evidence.append({"anomaly_id": a.id, "kind": a.kind, "message": a.message})
    if fresh_failure:
        evidence.append({"deployment_id": fresh_failure.id, "detail": "deployment failed again after the fix"})
    if restart_regression:
        evidence.append({"detail": "restart count climbed again after the fix"})
    if health and health.get("ok") is False:
        evidence.append({"detail": f"health check returned {health.get('status_code', 'no response')}"})

    fix.verification_status = "failed"
    fix.verified_at = utcnow()
    fix.verification_detail = {**(fix.verification_detail or {}), "resolved": "recurrence detected", "evidence": evidence}
    incident.auto_verified = False
    incident.status = IncidentStatus.diagnosed.value  # reopen — the fix did not hold
    db.commit()

    record_trace(
        db, "verification",
        f"'{spec.title}' did NOT hold: {spec.verify} was violated ({len(evidence)} piece(s) of evidence). Incident reopened.",
        project_id=incident.project_id, incident_id=incident.id, fix_action_id=fix.id,
        outcome="fail", detail={"evidence": evidence},
    )

    _propose_escalation(db, incident, fix, evidence)
    _propose_rollback(db, incident, fix, spec)
    return "failed"


def _unknown(db: Session, fix: FixAction, incident: Incident, spec) -> str:
    fix.verification_status = "unknown"
    fix.verified_at = utcnow()
    fix.verification_detail = {**(fix.verification_detail or {}), "resolved": "no telemetry received in the watch window"}
    db.commit()
    record_trace(
        db, "verification",
        f"Could not verify '{spec.title}' — no metrics arrived from the agent during the watch window. Needs a human look.",
        project_id=incident.project_id, incident_id=incident.id, fix_action_id=fix.id, outcome="escalate",
    )
    return "unknown"


def _propose_escalation(db: Session, incident: Incident, failed_fix: FixAction, evidence: list[dict]) -> None:
    """A fix that fails verification is itself worth a new, honest proposal —
    not a silent retry loop."""
    already = (
        db.query(FixAction)
        .filter(FixAction.incident_id == incident.id, FixAction.action_type == "escalate_to_devops")
        .first()
    )
    if already:
        return
    escalation = FixAction(
        incident_id=incident.id,
        action_type="escalate_to_devops",
        title=f"Escalate — '{failed_fix.title}' did not hold",
        rationale=(
            f"The approved fix ({failed_fix.action_type}) was applied but the Verification Agent "
            f"observed a recurrence afterward. Evidence: "
            + "; ".join(e.get("message") or e.get("detail", "") for e in evidence)[:600]
        ),
        params={"failed_fix_id": failed_fix.id, "reason": "verification failed"},
        risk=Risk.safe.value,
        requires_code_change=False,
        order_index=99,
        status=FixStatus.proposed.value,
    )
    db.add(escalation)
    db.commit()


def _propose_rollback(db: Session, incident: Incident, failed_fix: FixAction, spec) -> None:
    """If this action is marked reversible in the catalog and its own agent
    handler recorded the pre-change value, propose putting it back — as a
    normal, human-approved fix, not an automatic action. `reversible` has
    existed on ActionSpec since the first version of this catalog but was
    only ever shown in the UI; this is the first thing that actually acts on
    it.
    """
    if not spec.reversible:
        return
    field_map = PREVIOUS_VALUE_FIELD.get(failed_fix.action_type)
    if not field_map:
        return
    param_key, result_key = field_map
    previous_value = (failed_fix.result or {}).get(failed_fix.action_type, {}).get(result_key)
    if previous_value is None:
        return  # the handler didn't capture one — nothing to roll back to

    siblings = (
        db.query(FixAction)
        .filter(FixAction.incident_id == incident.id, FixAction.action_type == failed_fix.action_type)
        .all()
    )
    if any((s.params or {}).get(param_key) == previous_value for s in siblings):
        return  # already proposed (or this literally is the pre-fix value already)

    rollback = FixAction(
        incident_id=incident.id,
        action_type=failed_fix.action_type,
        title=f"Roll back — restore {param_key.replace('_', ' ')} to its previous value",
        rationale=(
            f"'{failed_fix.title}' changed {param_key} but verification found a recurrence. "
            f"Restoring the value from before that change ({previous_value}) while a human "
            f"investigates further, per this action's `reversible=True` policy."
        ),
        params={**(failed_fix.params or {}), param_key: previous_value},
        risk=spec.risk.value,
        requires_code_change=False,
        order_index=98,
        status=FixStatus.proposed.value,
    )
    db.add(rollback)
    db.commit()
    record_trace(
        db, "verification",
        f"Proposed a rollback of '{failed_fix.title}' to its pre-fix value ({previous_value}), pending approval.",
        project_id=incident.project_id, incident_id=incident.id, fix_action_id=rollback.id, outcome="info",
    )
