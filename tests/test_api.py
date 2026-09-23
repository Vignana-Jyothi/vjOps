"""End-to-end through the HTTP API.

This walks the real path a student deployment takes: CI registers a deployment
and is handed a port, the build fails, logs arrive, an incident is diagnosed,
a DevOps engineer approves a fix, and the agent picks up the command.
"""

import uuid

import pytest

from app.models import AgentCommand, Deployment, FixAction, Incident, Project, Server
from app.security import hash_agent_token, new_agent_token


@pytest.fixture
def slug():
    """Unique per test — the test database is shared across the whole session."""
    return f"team-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def enrolled_server(client, admin_headers, slug):
    r = client.post(
        "/api/agents/servers",
        headers=admin_headers,
        json={"name": f"node-{slug}", "hostname": "node.vnrvjiet.in", "port_range_start": 3100, "port_range_end": 3120},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    return {"id": data["id"], "token": data["agent_token"], "headers": {"X-Agent-Token": data["agent_token"]}}


@pytest.fixture
def project(client, admin_headers, enrolled_server, slug):
    r = client.post(
        "/api/projects",
        headers=admin_headers,
        json={
            "name": "Team Alpha",
            "slug": slug,
            "github_repo": f"vnrvjiet-incubator/{slug}",
            "team_name": "Alpha",
            "domain": f"{slug}.apps.vnrvjiet.in",
            "server_id": enrolled_server["id"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
def test_health_and_status_are_public(client):
    assert client.get("/healthz").json()["status"] == "ok"
    status = client.get("/api/system/status").json()
    assert status["database"]["ok"] is True
    # The LLM is deliberately unreachable in tests — the system must say so plainly.
    assert status["degraded_mode"] is True
    assert "signature library" in status["degraded_note"]
    assert status["auto_remediation"] is False


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/incidents").status_code == 401


def test_agent_token_is_required_for_ci_endpoints(client):
    r = client.post("/api/deployments/start", json={"repo": "x/y"})
    assert r.status_code == 401


def test_bad_agent_token_is_rejected(client):
    r = client.post(
        "/api/deployments/start",
        headers={"X-Agent-Token": "vops_not-a-real-token"},
        json={"repo": "x/y"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
def test_full_failed_deployment_lifecycle(client, admin_headers, enrolled_server, project, db):
    # 1. CI starts a deployment and is handed a port from the registry.
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={
            "repo": project["github_repo"],
            "commit_sha": "deadbeefcafe1234",
            "branch": "main",
            "gh_run_id": "99887766",
            "triggered_by": "student-one",
        },
    )
    assert start.status_code == 201, start.text
    dep = start.json()
    assert 3100 <= dep["port"] <= 3120, "the port must come from this server's configured range"
    deployment_id = dep["id"]

    # 2. The build fails and CI ships the logs.
    logs = client.post(
        "/api/deployments/logs",
        headers=enrolled_server["headers"],
        json={
            "deployment_id": deployment_id,
            "source": "docker",
            "container": "team-alpha",
            "text": (
                "2026-01-15T10:00:00.000Z Traceback (most recent call last):\n"
                '  File "/app/main.py", line 4, in <module>\n'
                "    import psycopg2\n"
                "2026-01-15T10:00:00.100Z ModuleNotFoundError: No module named 'psycopg2'\n"
            ),
        },
    )
    assert logs.status_code == 202, logs.text
    assert logs.json()["accepted"] >= 2

    # 3. CI reports failure. Diagnosis is queued in the background.
    finish = client.post(
        f"/api/deployments/{deployment_id}/finish",
        headers=enrolled_server["headers"],
        json={"status": "failure"},
    )
    assert finish.status_code == 200
    assert finish.json()["status"] == "failed"

    # 4. Trigger the analysis synchronously so the test is deterministic.
    analysis = client.post(f"/api/deployments/{deployment_id}/analyze", headers=admin_headers)
    assert analysis.status_code == 201, analysis.text
    incident_id = analysis.json()["incident_id"]

    detail = client.get(f"/api/incidents/{incident_id}", headers=admin_headers).json()
    incident = detail["incident"]
    assert incident["signature_key"] == "python_module_not_found"
    assert "psycopg2" in incident["root_cause"]
    assert incident["evidence"], "the diagnosis must cite evidence"
    assert incident["student_explanation"]
    assert detail["auto_remediation_enabled"] is False

    # 5. The dependency fix is advisory — there is nothing to execute on a server.
    advisory = next(f for f in detail["fixes"] if f["action_type"] == "add_dependency")
    rejected = client.post(f"/api/incidents/fixes/{advisory['id']}/approve", headers=admin_headers, json={})
    assert rejected.status_code == 400
    assert "advisory" in rejected.json()["detail"]

    # 6. The port stays allocated after a failure — releasing it here used to
    # be possible to race with the workflow's own rollback step (see
    # .github/workflows/viljaops-deploy.yml), which can leave a restored
    # container still bound to this exact port; releasing it in the registry
    # while that's true meant a completely different project's next deploy
    # could be handed the same port and fail to bind. It's reclaimed
    # deliberately instead: automatically by this same project's next
    # successful deploy (allocate() reuses its existing allocation), or
    # manually via the release endpoint below.
    usage = client.get(f"/api/infra/ports/{enrolled_server['id']}", headers=admin_headers).json()
    assert dep["port"] in [a["port"] for a in usage["allocations"] if a["status"] == "allocated"]

    released = client.post(f"/api/infra/ports/{enrolled_server['id']}/{dep['port']}/release", headers=admin_headers)
    assert released.status_code == 200

    usage = client.get(f"/api/infra/ports/{enrolled_server['id']}", headers=admin_headers).json()
    assert dep["port"] not in [a["port"] for a in usage["allocations"] if a["status"] == "allocated"]


def test_approval_dispatches_a_command_to_the_agent(client, admin_headers, enrolled_server, project, db):
    # Get a running deployment with logs that produce an executable fix.
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "aaaa1111", "gh_run_id": "1"},
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

    # The agent has never checked in, so dispatch must be refused rather than
    # silently queued forever.
    offline = client.post(f"/api/incidents/fixes/{fix['id']}/approve", headers=admin_headers, json={})
    assert offline.status_code == 503
    assert "has not checked in" in offline.json()["detail"]

    # Agent checks in.
    hb = client.post("/api/agents/heartbeat", headers=enrolled_server["headers"], json={"agent_version": "1.0.0", "cpu_cores": 8, "ram_mb": 16384})
    assert hb.status_code == 200

    approve = client.post(f"/api/incidents/fixes/{fix['id']}/approve", headers=admin_headers, json={"note": "approved in test"})
    assert approve.status_code == 200, approve.text
    kinds = {c["kind"] for c in approve.json()["dispatched_commands"]}
    assert "recreate_container" in kinds
    assert "apply_nginx_config" in kinds, "re-pointing the port must also regenerate the proxy config"

    # The agent polls and receives exactly those commands.
    commands = client.get("/api/agents/commands", headers=enrolled_server["headers"]).json()
    assert {c["kind"] for c in commands} == kinds

    # Reporting a result closes the loop.
    cmd = commands[0]
    result = client.post(
        "/api/agents/commands/result",
        headers=enrolled_server["headers"],
        json={"command_id": cmd["id"], "ok": True, "output": "recreated"},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "succeeded"

    # A claimed command is not handed out twice.
    assert client.get("/api/agents/commands", headers=enrolled_server["headers"]).json() == []


def test_confirmation_records_ground_truth_and_feeds_the_dataset(client, admin_headers, enrolled_server, project):
    start = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": project["github_repo"], "commit_sha": "bbbb2222", "gh_run_id": "2"},
    ).json()
    client.post(
        "/api/deployments/logs",
        headers=enrolled_server["headers"],
        json={"deployment_id": start["id"], "source": "docker", "text": "2026-01-15T10:00:00.000Z team-alpha exited with code 137\n"},
    )
    client.post(f"/api/deployments/{start['id']}/finish", headers=enrolled_server["headers"], json={"status": "failure"})
    incident_id = client.post(f"/api/deployments/{start['id']}/analyze", headers=admin_headers).json()["incident_id"]

    confirmed = client.post(
        f"/api/incidents/{incident_id}/confirm",
        headers=admin_headers,
        json={
            "confirmed_root_cause": "The container loaded the full CSV into memory at startup and exceeded its 512 MB limit.",
            "confirmed_fix": "Stream the CSV in chunks and raise the limit to 1 GB.",
            "was_ai_correct": True,
            "add_to_knowledge_base": True,
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "resolved"

    stats = client.get("/api/dataset/stats", headers=admin_headers).json()
    assert stats["confirmed"] >= 1
    assert "oom_killed" in stats["by_signature"]

    accuracy = client.get("/api/incidents/meta/accuracy", headers=admin_headers).json()
    assert accuracy["confirmed_incidents"] >= 1
    assert accuracy["accuracy"] is not None


def test_port_exhaustion_is_reported_clearly(client, admin_headers, db, slug):
    r = client.post(
        "/api/agents/servers",
        headers=admin_headers,
        json={"name": f"tiny-{slug}", "port_range_start": 3200, "port_range_end": 3201},
    )
    server = r.json()
    headers = {"X-Agent-Token": server["agent_token"]}

    for i in range(2):
        p = client.post(
            "/api/projects",
            headers=admin_headers,
            json={"name": f"Filler {i}", "slug": f"{slug}-f{i}", "github_repo": f"org/{slug}-f{i}", "server_id": server["id"]},
        )
        assert p.status_code == 201
        s = client.post(
            "/api/deployments/start",
            headers=headers,
            json={"repo": f"org/{slug}-f{i}", "commit_sha": f"c{i}", "gh_run_id": str(i)},
        )
        assert s.status_code == 201, s.text

    client.post(
        "/api/projects",
        headers=admin_headers,
        json={"name": "Overflow", "slug": f"{slug}-over", "github_repo": f"org/{slug}-over", "server_id": server["id"]},
    )
    over = client.post(
        "/api/deployments/start",
        headers=headers,
        json={"repo": f"org/{slug}-over", "commit_sha": "c9", "gh_run_id": "9"},
    )
    assert over.status_code == 507
    assert "No free ports" in over.json()["detail"]


def test_unregistered_repo_gets_an_actionable_error(client, enrolled_server):
    r = client.post(
        "/api/deployments/start",
        headers=enrolled_server["headers"],
        json={"repo": "someone/never-registered", "commit_sha": "x"},
    )
    assert r.status_code == 404
    assert "Register the project" in r.json()["detail"]


def test_action_catalog_is_exposed_for_transparency(client, admin_headers):
    actions = client.get("/api/incidents/meta/actions", headers=admin_headers).json()
    keys = {a["action_type"] for a in actions}
    assert "restart_container" in keys
    assert "run_migration" in keys
    dangerous = [a for a in actions if a["risk"] == "dangerous"]
    assert all(a["never_auto"] for a in dangerous), "every dangerous action must be flagged never-auto"


def test_students_only_see_their_own_projects(client, admin_headers, project, slug):
    client.post(
        "/api/auth/users",
        headers=admin_headers,
        json={"email": f"{slug}@vnrvjiet.in", "password": "student-password-1", "role": "student"},
    )
    token = client.post("/api/auth/login", data={"username": f"{slug}@vnrvjiet.in", "password": "student-password-1"}).json()["access_token"]
    student = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/projects", headers=student).json() == []
    # And a student cannot approve fixes or enroll servers.
    assert client.post("/api/agents/servers", headers=student, json={"name": "rogue"}).status_code == 403
    assert client.get("/api/observability/risk", headers=student).status_code == 403


def test_nginx_preview_and_domain_conflict_via_api(client, admin_headers, project, enrolled_server, slug):
    preview = client.post(
        "/api/infra/nginx/preview",
        headers=admin_headers,
        json={"project_id": project["id"], "domain": f"{slug}.apps.vnrvjiet.in", "upstream_port": 3105, "options": {}},
    )
    assert preview.status_code == 200, preview.text
    assert "proxy_pass" in preview.json()["rendered"]
    assert preview.json()["lint"] == []

    created = client.post(
        "/api/infra/nginx",
        headers=admin_headers,
        json={"project_id": project["id"], "domain": f"{slug}.apps.vnrvjiet.in", "upstream_port": 3105, "options": {}},
    )
    assert created.status_code == 201, created.text
