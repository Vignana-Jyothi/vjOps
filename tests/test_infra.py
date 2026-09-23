"""Port registry and Nginx generation.

These replace manual work that currently causes outages, so the failure modes
that matter are: handing the same port to two projects, and pushing a config
that breaks every site on the box.
"""

import uuid

import pytest

from app.infra import nginx as nginx_svc
from app.infra import ports as port_svc
from app.models import Project, Server
from app.security import hash_agent_token, new_agent_token


@pytest.fixture
def server(db):
    s = Server(name=f"test-server-{uuid.uuid4().hex[:8]}", hostname="test.local", port_range_start=3000, port_range_end=3005, token_hash=hash_agent_token(new_agent_token()))
    db.add(s)
    db.commit()
    yield s
    db.query(Server).filter(Server.id == s.id).delete()
    db.commit()


@pytest.fixture
def project(db):
    p = Project(slug=f"proj-{uuid.uuid4().hex[:8]}", name="Test Project", domain=f"{uuid.uuid4().hex[:8]}.apps.vnrvjiet.in")
    db.add(p)
    db.commit()
    return p


# --------------------------------------------------------------------------- #
def test_allocates_from_range_and_skips_reserved(db, server):
    server.port_range_start, server.port_range_end = 21, 25  # spans reserved 22 and 25
    db.commit()
    ports = {port_svc.allocate(db, server.id, purpose=f"p{i}").port for i in range(3)}
    assert 22 not in ports and 25 not in ports
    assert ports <= {21, 23, 24}


def test_never_allocates_the_same_port_twice(db, server):
    allocated = [port_svc.allocate(db, server.id, purpose=f"svc{i}").port for i in range(6)]
    assert len(allocated) == len(set(allocated)), "duplicate port handed out"


def test_exhaustion_raises_a_useful_error(db, server):
    for i in range(6):
        port_svc.allocate(db, server.id, purpose=f"svc{i}")
    with pytest.raises(port_svc.NoPortsAvailable) as exc:
        port_svc.allocate(db, server.id, purpose="one-too-many")
    assert "3000-3005" in str(exc.value)


def test_redeploy_reuses_the_projects_existing_port(db, server, project):
    first = port_svc.allocate(db, server.id, project_id=project.id)
    second = port_svc.allocate(db, server.id, project_id=project.id)
    assert first.port == second.port, "a redeploy should keep its port so Nginx does not need to change"


def test_released_port_returns_to_the_pool(db, server):
    a = port_svc.allocate(db, server.id, purpose="temp")
    assert port_svc.release(db, server.id, a.port) is True
    b = port_svc.allocate(db, server.id, purpose="temp2", preferred=a.port)
    assert b.port == a.port


def test_reconcile_adopts_ports_taken_outside_the_platform(db, server):
    result = port_svc.reconcile(db, server.id, [{"port": 3004, "process": "docker-proxy", "container": "rogue"}])
    assert any(u["port"] == 3004 for u in result["untracked_adopted"])
    # And it is now protected from being handed out.
    assert 3004 not in {port_svc.allocate(db, server.id, purpose=f"x{i}").port for i in range(4)}


# --------------------------------------------------------------------------- #
def test_rendered_config_has_the_essentials(db, project):
    text = nginx_svc.render(project, domain="team.apps.vnrvjiet.in", upstream_port=3011)
    assert "server_name team.apps.vnrvjiet.in;" in text
    assert "server 127.0.0.1:3011" in text
    assert "proxy_set_header X-Forwarded-For" in text
    assert f"/var/log/nginx/{project.slug}.access.log" in text
    assert nginx_svc.lint(text) == []


def test_lint_catches_unbalanced_braces():
    broken = "server {\n  listen 80;\n  proxy_pass http://x;\n  server_name a.b;\n"
    problems = nginx_svc.lint(broken)
    assert any("brace" in p.lower() for p in problems)


def test_lint_catches_missing_proxy_pass():
    cfg = "server {\n  listen 80;\n  server_name a.b;\n}\n"
    assert any("proxy_pass" in p for p in nginx_svc.lint(cfg))


def test_two_projects_cannot_claim_one_domain(db, server, project):
    cfg = nginx_svc.create_config(db, project, server, domain="shared.apps.vnrvjiet.in", upstream_port=3001)
    nginx_svc.mark_applied(db, cfg, {"ok": True, "nginx_test_output": "syntax is ok"})

    other = Project(slug=f"other-{uuid.uuid4().hex[:8]}", name="Other Team")
    db.add(other)
    db.commit()

    with pytest.raises(nginx_svc.NginxError) as exc:
        nginx_svc.create_config(db, other, server, domain="shared.apps.vnrvjiet.in", upstream_port=3002)
    assert "already served" in str(exc.value)


def test_apply_payload_demands_validation_and_rollback(db, server, project):
    cfg = nginx_svc.create_config(db, project, server, domain="verify.apps.vnrvjiet.in", upstream_port=3003)
    payload = nginx_svc.apply_payload(cfg, project)
    assert payload["require_nginx_test"] is True
    assert payload["rollback_on_failure"] is True
    assert payload["target_path"].startswith("/etc/nginx/sites-available/")
    # The checksum is what stops a config being altered between here and the agent.
    import hashlib

    assert payload["checksum"] == hashlib.sha256(cfg.rendered.encode()).hexdigest()


def test_websocket_and_upload_options_render(db, project):
    text = nginx_svc.render(
        project,
        domain="ws.apps.vnrvjiet.in",
        upstream_port=3009,
        options={"websocket": True, "client_max_body_size": "50m"},
    )
    assert 'proxy_set_header Upgrade $http_upgrade' in text
    assert "client_max_body_size 50m;" in text
    assert nginx_svc.lint(text) == []
