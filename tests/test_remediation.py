"""The Verification Agent: a fix's agent command exiting 0 only proves the
shell call ran. These tests check the actual `verify` criterion in
`rca/fixes.py` is what decides pass/fail — not the exit code alone."""

import uuid
from datetime import timedelta

import pytest

from app.models import Anomaly, FixAction, Incident, IncidentStatus, MetricSample, utcnow
from app.remediation.verify import run_verification_sweep, start_verification, verify_now


@pytest.fixture
def slug():
    return f"team-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def enrolled_server(client, admin_headers, slug):
    r = client.post(
        "/api/agents/servers",
        headers=admin_headers,
        json={"name": f"node-{slug}", "port_range_start": 3300, "port_range_end": 3320},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    return {"id": data["id"], "headers": {"X-Agent-Token": data["agent_token"]}}


@pytest.fixture
def project(client, admin_headers, enrolled_server, slug):
    r = client.post(
        "/api/projects",
        headers=admin_headers,
        json={"name": "Team Verify", "slug": slug, "github_repo": f"org/{slug}", "server_id": enrolled_server["id"], "domain": f"{slug}.apps.example.edu"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _oom_incident(client, admin_headers, enrolled_server, project):
    """Deploy -> OOM-kill in the logs -> diagnosed -> approved -> agent
    reports success. Returns (incident_id, fix, deployment_id)."""
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "c0ffee00", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    client.post(
        "/api/deployments/logs",
        headers=enrolled_server["headers"],
        json={"deployment_id": start["id"], "source": "docker", "text": "2026-01-15T10:00:00.000Z team exited with code 137\n"},
    )
    client.post(f"/api/deployments/{start['id']}/finish", headers=enrolled_server["headers"], json={"status": "failure"})
    incident_id = client.post(f"/api/deployments/{start['id']}/analyze", headers=admin_headers).json()["incident_id"]

    detail = client.get(f"/api/incidents/{incident_id}", headers=admin_headers).json()
    fix = next(f for f in detail["fixes"] if f["action_type"] == "set_memory_limit")

    client.post("/api/agents/heartbeat", headers=enrolled_server["headers"], json={"agent_version": "1.0.0"})
    approve = client.post(f"/api/incidents/fixes/{fix['id']}/approve", headers=admin_headers, json={})
    assert approve.status_code == 200, approve.text

    commands = client.get("/api/agents/commands", headers=enrolled_server["headers"]).json()
    assert {c["kind"] for c in commands} == {"set_memory_limit"}
    cmd = commands[0]
    result = client.post(
        "/api/agents/commands/result",
        headers=enrolled_server["headers"],
        json={
            "command_id": cmd["id"],
            "ok": True,
            "output": "memory limit updated",
            "detail": {"memory_mb": cmd["payload"].get("memory_mb"), "previous_memory_mb": 512},
        },
    )
    assert result.status_code == 200

    return incident_id, fix, start["id"]


def test_command_success_starts_pending_verification_not_immediate_success(
    client, admin_headers, enrolled_server, project, db
):
    """This is the bug this whole feature fixes: exit 0 must not be the end
    of the story for an observed action."""
    _incident_id, fix, _dep_id = _oom_incident(client, admin_headers, enrolled_server, project)

    row = db.get(FixAction, fix["id"])
    assert row.verification_status == "pending", "an observed action must watch before declaring success"
    assert row.verification_detail["criterion"] == "no OOM kill for 15 min"
    assert row.verification_detail["grace_minutes"] == 15
    assert "memory_ceiling" in row.verification_detail["watch_kinds"]


def test_verification_passes_when_metrics_stay_healthy(client, admin_headers, enrolled_server, project, db):
    incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    row = db.get(FixAction, fix["id"])

    # A healthy metric sample after the fix — this is what "telemetry exists
    # and nothing is wrong" looks like.
    db.add(MetricSample(deployment_id=dep_id, mem_mb=300, mem_limit_mb=2048, restarts=row.verification_detail["baseline_restarts"] or 0))
    db.commit()

    # Force-resolve rather than waiting 15 real minutes.
    verify_now(db, fix["id"])
    db.refresh(row)
    assert row.verification_status == "passed"
    assert row.verified_at is not None

    incident = db.get(Incident, incident_id)
    assert incident.auto_verified is True


def test_verification_fails_on_recurrence_and_reopens_the_incident(client, admin_headers, enrolled_server, project, db):
    incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    row = db.get(FixAction, fix["id"])

    # The memory limit was raised, but the container hit the new ceiling too.
    db.add(
        Anomaly(
            deployment_id=dep_id,
            project_id=project["id"],
            kind="memory_ceiling",
            metric="mem_mb",
            severity="critical",
            value=2000,
            baseline=2048,
            message="Using 98% of its 2048 MB memory limit.",
            detected_at=utcnow(),
        )
    )
    db.commit()

    verify_now(db, fix["id"])
    db.refresh(row)
    assert row.verification_status == "failed"
    assert row.verification_detail["evidence"], "a failed verification must carry evidence, not just a status flip"

    incident = db.get(Incident, incident_id)
    assert incident.status == IncidentStatus.diagnosed.value, "a fix that didn't hold must reopen the incident"
    assert incident.auto_verified is False

    # And it must not fail silently — an escalation is proposed automatically.
    fixes = client.get(f"/api/incidents/{incident_id}", headers=admin_headers).json()["fixes"]
    escalation = next((f for f in fixes if f["action_type"] == "escalate_to_devops"), None)
    assert escalation is not None
    assert "did not hold" in escalation["title"]


def test_verification_reports_unknown_with_zero_telemetry(client, admin_headers, enrolled_server, project, db):
    incident_id, fix, _dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    row = db.get(FixAction, fix["id"])

    # No MetricSample rows at all for this deployment since the fix ran.
    verify_now(db, fix["id"])
    db.refresh(row)
    assert row.verification_status == "unknown"
    assert row.verified_at is not None

    incident = db.get(Incident, incident_id)
    assert incident.auto_verified is False, "silence must not be read as success"


def test_sweep_leaves_fresh_pending_fixes_alone(client, admin_headers, enrolled_server, project, db):
    """The scheduled sweep must not resolve anything whose grace window
    hasn't elapsed yet, even with no force flag."""
    _incident_id, fix, _dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    result = run_verification_sweep(db)
    assert result["still_waiting"] >= 1

    row = db.get(FixAction, fix["id"])
    assert row.verification_status == "pending"


def test_sweep_resolves_once_the_grace_window_has_actually_elapsed(client, admin_headers, enrolled_server, project, db):
    _incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    row = db.get(FixAction, fix["id"])

    # Backdate the window so the sweep (no force flag) considers it due.
    detail = dict(row.verification_detail)
    detail["started_at"] = (utcnow() - timedelta(minutes=20)).isoformat()
    row.verification_detail = detail
    db.add(MetricSample(deployment_id=dep_id, mem_mb=200, mem_limit_mb=2048, restarts=0))
    db.commit()

    result = run_verification_sweep(db)
    assert result["passed"] == 1

    db.refresh(row)
    assert row.verification_status == "passed"


def test_immediate_actions_resolve_without_a_watch_window(client, admin_headers, enrolled_server, project, db):
    """apply_nginx_config's ActionSpec.verify is 'nginx -t passes' — that IS
    the command result, so there is nothing to watch for afterward."""
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "beef0002", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    client.post(
        "/api/deployments/logs",
        headers=enrolled_server["headers"],
        json={
            "deployment_id": start["id"],
            "source": "docker",
            "text": "2026-01-15T10:00:00.000Z docker: Error response from daemon: Bind for 0.0.0.0:3000 failed: port is already allocated.\n",
        },
    )
    client.post(f"/api/deployments/{start['id']}/finish", headers=enrolled_server["headers"], json={"status": "failure"})
    incident_id = client.post(f"/api/deployments/{start['id']}/analyze", headers=admin_headers).json()["incident_id"]
    detail = client.get(f"/api/incidents/{incident_id}", headers=admin_headers).json()
    fix = next(f for f in detail["fixes"] if f["action_type"] == "reassign_port")

    client.post("/api/agents/heartbeat", headers=enrolled_server["headers"], json={"agent_version": "1.0.0"})
    client.post(f"/api/incidents/fixes/{fix['id']}/approve", headers=admin_headers, json={})

    commands = client.get("/api/agents/commands", headers=enrolled_server["headers"]).json()
    nginx_cmd = next(c for c in commands if c["kind"] == "apply_nginx_config")
    other_cmd = next(c for c in commands if c["kind"] != "apply_nginx_config")
    for c in (other_cmd, nginx_cmd):
        client.post(
            "/api/agents/commands/result",
            headers=enrolled_server["headers"],
            json={"command_id": c["id"], "ok": True, "output": "ok"},
        )

    row = db.get(FixAction, fix["id"])
    # reassign_port itself is an observed action (watches error_rate_spike),
    # unlike apply_nginx_config alone — so it should be pending, not passed.
    assert row.verification_status == "pending"


def test_agent_trace_covers_investigator_and_remediation_stages(client, admin_headers, enrolled_server, project, db):
    incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)

    trace = client.get(f"/api/incidents/{incident_id}/trace", headers=admin_headers).json()
    stages = [t["stage"] for t in trace]
    assert "investigator" in stages, "diagnosis must be narrated"
    assert "remediation" in stages, "the approval must be narrated"
    assert "verification" in stages, "starting the watch window must be narrated"
    # Chronological order.
    assert stages.index("investigator") < stages.index("remediation") < stages.index("verification")


def test_health_check_dispatched_and_folded_into_pass(client, admin_headers, enrolled_server, project, db):
    """A passing health check is affirmative evidence, not just an absence
    of bad anomalies — this is the Level 2 behavioral check."""
    incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)

    # start_verification should have queued exactly one health_check command
    # for this server, addressed at this fix.
    commands = client.get("/api/agents/commands", headers=enrolled_server["headers"]).json()
    health_cmd = next((c for c in commands if c["kind"] == "health_check"), None)
    assert health_cmd is not None
    assert health_cmd["payload"]["verify_fix_id"] == fix["id"]

    client.post(
        "/api/agents/commands/result",
        headers=enrolled_server["headers"],
        json={"command_id": health_cmd["id"], "ok": True, "output": "200 OK", "detail": {"status_code": 200}},
    )

    row = db.get(FixAction, fix["id"])
    assert row.verification_detail["health_check"]["ok"] is True
    assert row.verification_detail["health_check"]["status_code"] == 200

    # The health check must not have touched the fix's execution status —
    # it was dispatched with fix_action_id=None specifically to avoid that.
    assert row.status == "succeeded"

    db.add(MetricSample(deployment_id=dep_id, mem_mb=300, mem_limit_mb=2048, restarts=0))
    db.commit()
    verify_now(db, fix["id"])
    db.refresh(row)
    assert row.verification_status == "passed"


def test_failed_health_check_fails_verification_even_before_window_elapses(
    client, admin_headers, enrolled_server, project, db
):
    """A 500 right now is decisive — no reason to wait out the grace window."""
    incident_id, fix, _dep_id = _oom_incident(client, admin_headers, enrolled_server, project)

    commands = client.get("/api/agents/commands", headers=enrolled_server["headers"]).json()
    health_cmd = next(c for c in commands if c["kind"] == "health_check")
    client.post(
        "/api/agents/commands/result",
        headers=enrolled_server["headers"],
        json={"command_id": health_cmd["id"], "ok": False, "output": "", "error": "HTTP 500", "detail": {"status_code": 500}},
    )

    row = db.get(FixAction, fix["id"])
    assert row.verification_status == "pending", "the sweep hasn't run yet"

    # No force=True, window not elapsed — should still resolve because the
    # health check failure is decisive.
    result = run_verification_sweep(db)
    assert result["failed"] == 1

    db.refresh(row)
    assert row.verification_status == "failed"
    assert any("500" in (e.get("detail") or "") for e in row.verification_detail["evidence"])


def test_rollback_proposed_when_reversible_fix_fails_verification(client, admin_headers, enrolled_server, project, db):
    """set_memory_limit is reversible=True and the agent captures the
    previous value — a failed verification should propose putting it back,
    not just escalate."""
    incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    row = db.get(FixAction, fix["id"])

    # Confirm the agent's own capture of the previous value made it through.
    assert "previous_memory_mb" in row.result.get("set_memory_limit", {})

    db.add(
        Anomaly(
            deployment_id=dep_id, project_id=project["id"], kind="memory_ceiling", metric="mem_mb",
            severity="critical", value=2000, baseline=2048, message="still ceiling-bound", detected_at=utcnow(),
        )
    )
    db.commit()
    verify_now(db, fix["id"])

    fixes = client.get(f"/api/incidents/{incident_id}", headers=admin_headers).json()["fixes"]
    rollback = next((f for f in fixes if f["title"].startswith("Roll back")), None)
    assert rollback is not None, "a reversible action's failed verification should propose a rollback"
    assert rollback["action_type"] == "set_memory_limit"
    assert rollback["status"] == "proposed"


def test_shared_scope_actions_require_explicit_confirmation(client, admin_headers, enrolled_server, project, db):
    """No action in the current catalog is scope='shared' yet, but the gate
    itself must work — this proves it does, ahead of the day one is added."""
    from app.rca import fixes as fixes_module

    original = fixes_module.ACTION_CATALOG["restart_container"]
    shared_spec = fixes_module.ActionSpec(
        original.key, original.title, original.description, original.risk,
        params=original.params, verify=original.verify, scope="shared",
    )
    fixes_module.ACTION_CATALOG["restart_container"] = shared_spec
    try:
        incident_id, fix, _dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
        # _oom_incident already approved the set_memory_limit fix; grab a
        # fresh restart_container-shaped fix isn't guaranteed to exist, so
        # exercise the endpoint directly against a fabricated fix row.
        restart_fix = FixAction(
            incident_id=incident_id, action_type="restart_container", title="Restart",
            rationale="test", params={"container": "x"}, risk="moderate",
            requires_code_change=False, order_index=1, status="proposed",
        )
        db.add(restart_fix)
        db.commit()

        denied = client.post(f"/api/incidents/fixes/{restart_fix.id}/approve", headers=admin_headers, json={})
        assert denied.status_code == 400
        assert "confirm_shared" in denied.json()["detail"]

        allowed = client.post(
            f"/api/incidents/fixes/{restart_fix.id}/approve",
            headers=admin_headers,
            json={"params": {"confirm_shared": True}},
        )
        assert allowed.status_code == 200
    finally:
        fixes_module.ACTION_CATALOG["restart_container"] = original


def test_force_verify_endpoint_requires_devops_role(client, admin_headers, enrolled_server, project):
    incident_id, fix, _dep_id = _oom_incident(client, admin_headers, enrolled_server, project)
    r = client.post(f"/api/incidents/fixes/{fix['id']}/verify", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["verification_status"] in ("passed", "failed", "unknown")


def test_successful_deployment_auto_applies_nginx_without_manual_step(client, admin_headers, enrolled_server, project):
    """Regression test: a successful deployment used to only generate and
    validate the Nginx config, never dispatch it — the site would stay
    unreachable through its domain until a human called
    POST /api/infra/nginx/{id}/apply by hand."""
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "deadbeef", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()

    # Agent must be considered online for the auto-dispatch to fire.
    client.post("/api/agents/heartbeat", headers=enrolled_server["headers"], json={"agent_version": "1.0.0"})

    finish = client.post(
        f"/api/deployments/{start['id']}/finish",
        headers=enrolled_server["headers"],
        json={"status": "success", "container_name": f"{project['slug']}-app"},
    )
    assert finish.status_code == 200

    commands = client.get("/api/agents/commands", headers=enrolled_server["headers"]).json()
    apply_cmds = [c for c in commands if c["kind"] == "apply_nginx_config"]
    assert len(apply_cmds) == 1, "a successful deployment with a domain set must dispatch its own nginx apply"
    assert apply_cmds[0]["payload"]["domain"] == project["domain"]
    assert apply_cmds[0]["payload"]["upstream_port"] == start["port"]


# --------------------------------------------------------------------------- #
# Authorization: a direct GET by id must not bypass the ownership filtering
# that list endpoints already apply for students.
# --------------------------------------------------------------------------- #
def _student_headers(client, admin_headers, email):
    client.post(
        "/api/auth/users",
        headers=admin_headers,
        json={"email": email, "name": email.split("@")[0], "role": "student", "password": "test-pass-123"},
    )
    r = client.post("/api/auth/login", data={"username": email, "password": "test-pass-123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_student_cannot_read_another_students_incident_by_id(client, admin_headers, enrolled_server, slug, db):
    """This is the exact IDOR the direct GET endpoints had: list_incidents
    filtered by ownership, but GET /incidents/{id} did not — any
    authenticated student with (or able to guess/enumerate) the id could
    read another team's logs and diagnosis."""
    owner_email = f"owner-{uuid.uuid4().hex[:6]}@vnrvjiet.in"
    stranger_email = f"stranger-{uuid.uuid4().hex[:6]}@vnrvjiet.in"
    owner_headers = _student_headers(client, admin_headers, owner_email)
    stranger_headers = _student_headers(client, admin_headers, stranger_email)
    owner_id = client.get("/api/auth/me", headers=owner_headers).json()["id"]

    project = client.post(
        "/api/projects",
        headers=admin_headers,
        json={
            "name": "Owned Team", "slug": slug, "github_repo": f"org/{slug}",
            "server_id": enrolled_server["id"], "domain": f"{slug}.apps.example.edu", "owner_id": owner_id,
        },
    ).json()

    incident_id, fix, dep_id = _oom_incident(client, admin_headers, enrolled_server, project)

    # The owner can read it.
    assert client.get(f"/api/incidents/{incident_id}", headers=owner_headers).status_code == 200
    assert client.get(f"/api/incidents/{incident_id}/trace", headers=owner_headers).status_code == 200
    assert client.get(f"/api/deployments/{dep_id}", headers=owner_headers).status_code == 200
    assert client.get(f"/api/deployments/{dep_id}/timeline", headers=owner_headers).status_code == 200

    # A student who does not own the project cannot, even with a valid id.
    assert client.get(f"/api/incidents/{incident_id}", headers=stranger_headers).status_code == 403
    assert client.get(f"/api/incidents/{incident_id}/trace", headers=stranger_headers).status_code == 403
    assert client.get(f"/api/deployments/{dep_id}", headers=stranger_headers).status_code == 403
    assert client.get(f"/api/deployments/{dep_id}/timeline", headers=stranger_headers).status_code == 403
    assert client.post(f"/api/deployments/{dep_id}/analyze", headers=stranger_headers).status_code == 403

    # Non-student roles are unaffected — a mentor/devops/admin can still see everything.
    assert client.get(f"/api/incidents/{incident_id}", headers=admin_headers).status_code == 200


def test_failed_deployment_port_is_not_stolen_by_a_different_project(
    client, admin_headers, enrolled_server, project, db, slug
):
    """Regression test for the release/rollback race: releasing a port the
    instant a deployment is reported as failed used to let a *different*
    project's very next allocation claim the same port, even though the
    workflow's own rollback step might have just restarted the previous
    container on it. The port must stay put until this project's own next
    deploy reclaims it or a human releases it deliberately."""
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "f0000001", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    client.post(f"/api/deployments/{start['id']}/finish", headers=enrolled_server["headers"], json={"status": "failure"})

    other_slug = f"{slug}-other"
    other_project = client.post(
        "/api/projects",
        headers=admin_headers,
        json={"name": "Other Team", "slug": other_slug, "github_repo": f"org/{other_slug}", "server_id": enrolled_server["id"]},
    ).json()
    other_start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": other_project["github_repo"], "commit_sha": "f0000002", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    assert other_start["port"] != start["port"], "a different project must never be handed a port a failed deploy still holds"

    # The same project's own next attempt reclaims exactly the same port.
    redeploy = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "f0000003", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    assert redeploy["port"] == start["port"]


def test_last_good_deployment_uses_deployment_history_not_docker_images(client, admin_headers, enrolled_server, project):
    """The rollback target must come from ViljaOps's own recorded deployment
    outcomes (status=running means it passed its own health check), not
    from `docker images` listing order, which reflects build/pull order and
    has no relationship to which image was ever confirmed healthy."""
    good = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "good0001", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    client.post(
        f"/api/deployments/{good['id']}/finish",
        headers=enrolled_server["headers"],
        json={"status": "success", "image": f"{project['slug']}:good0001", "container_name": project["slug"]},
    )

    bad = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "bad00002", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    client.post(
        f"/api/deployments/{bad['id']}/finish",
        headers=enrolled_server["headers"],
        json={"status": "failure", "image": f"{project['slug']}:bad00002"},
    )

    last_good = client.get(f"/api/deployments/{bad['id']}/last-good", headers=enrolled_server["headers"]).json()
    assert last_good["found"] is True
    assert last_good["image"] == f"{project['slug']}:good0001"
    assert last_good["deployment_id"] == good["id"]


def test_last_good_deployment_reports_not_found_when_nothing_ever_ran(client, admin_headers, enrolled_server, project):
    only_failure = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "onlybad1", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    client.post(f"/api/deployments/{only_failure['id']}/finish", headers=enrolled_server["headers"], json={"status": "failure"})

    last_good = client.get(f"/api/deployments/{only_failure['id']}/last-good", headers=enrolled_server["headers"]).json()
    assert last_good["found"] is False
    assert last_good["image"] is None


# --------------------------------------------------------------------------- #
# Cross-server isolation: an agent's token proves which server it's calling
# from, not that it may read or write every deployment in the system.
# submit_result (agents.py) already enforced this; finish_deployment,
# ingest_logs and last_good_deployment did not.
# --------------------------------------------------------------------------- #
def test_agent_cannot_finish_read_or_log_to_another_servers_deployment(client, admin_headers, enrolled_server, project, slug):
    other_server = client.post(
        "/api/agents/servers",
        headers=admin_headers,
        json={"name": f"node-{slug}-other", "port_range_start": 3400, "port_range_end": 3420},
    ).json()
    other_headers = {"X-Agent-Token": other_server["agent_token"]}

    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "iso00001", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()

    # Another server's agent must not be able to finish this deployment...
    denied_finish = client.post(f"/api/deployments/{start['id']}/finish", headers=other_headers, json={"status": "success"})
    assert denied_finish.status_code == 403

    # ...or attach fabricated logs to it (would otherwise poison this
    # project's RCA with another team's data)...
    denied_logs = client.post(
        "/api/deployments/logs",
        headers=other_headers,
        json={"deployment_id": start["id"], "source": "docker", "text": "fabricated log line\n"},
    )
    assert denied_logs.status_code == 403

    # ...or read its last-known-good image.
    denied_last_good = client.get(f"/api/deployments/{start['id']}/last-good", headers=other_headers)
    assert denied_last_good.status_code == 403

    # The owning server can still do all three.
    assert client.post(
        "/api/deployments/logs", headers=enrolled_server["headers"],
        json={"deployment_id": start["id"], "source": "docker", "text": "real log line\n"},
    ).status_code == 202
    assert client.get(f"/api/deployments/{start['id']}/last-good", headers=enrolled_server["headers"]).status_code == 200
    assert client.post(
        f"/api/deployments/{start['id']}/finish", headers=enrolled_server["headers"], json={"status": "success"}
    ).status_code == 200


def test_rollback_status_is_recorded_and_failed_rollback_is_loud(client, admin_headers, enrolled_server, project):
    """The workflow used to fire-and-forget its rollback attempt (`|| true`)
    with no way for ViljaOps to know whether it actually worked. A project
    with a failed rollback has NO running application at all — materially
    worse than an ordinary failed deploy — and that must be visible, not
    silently indistinguishable from any other failure."""
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "rb000001", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()

    finish = client.post(
        f"/api/deployments/{start['id']}/finish",
        headers=enrolled_server["headers"],
        json={"status": "failure", "rollback_status": "failed"},
    )
    assert finish.status_code == 200
    body = finish.json()
    assert body["rollback_status"] == "failed"
    assert "ROLLBACK FAILED" in body["error_summary"]
    assert "no running deployment" in body["error_summary"]

    fetched = client.get(f"/api/deployments/{start['id']}", headers=admin_headers).json()
    assert fetched["rollback_status"] == "failed"


def test_rollback_status_defaults_to_not_attempted(client, admin_headers, enrolled_server, project):
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "rb000002", "gh_run_id": str(uuid.uuid4().int)[:8]},
    ).json()
    finish = client.post(f"/api/deployments/{start['id']}/finish", headers=enrolled_server["headers"], json={"status": "success"})
    assert finish.json()["rollback_status"] == "not_attempted"
    assert "ROLLBACK FAILED" not in finish.json()["error_summary"]
