"""The signature library is the safety net when inference is unavailable.

These tests use real log text of the kind the incubator's servers actually
produce, because a regex library that only matches its own examples is worthless.
"""

from datetime import datetime, timezone

from app.rca.signatures import BY_KEY, match_signatures


def ev(source, message, level="error", i=1):
    return {
        "id": f"E{i}",
        "source": source,
        "message": message,
        "level": level,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def top_key(events):
    hits = match_signatures(events)
    return hits[0]["signature_key"] if hits else None


def test_port_conflict_detected_and_port_extracted():
    events = [
        ev("docker", 'docker: Error response from daemon: driver failed programming external connectivity on endpoint team-alpha: Bind for 0.0.0.0:3000 failed: port is already allocated.'),
    ]
    hits = match_signatures(events)
    assert hits[0]["signature_key"] == "port_already_allocated"
    assert hits[0]["extracted"]["port"] == "3000"
    assert "3000" in hits[0]["root_cause"]
    assert any(f["action_type"] == "reassign_port" for f in hits[0]["fixes"])


def test_missing_python_module_extracts_package_name():
    events = [ev("docker", "ModuleNotFoundError: No module named 'psycopg2'")]
    hits = match_signatures(events)
    assert hits[0]["signature_key"] == "python_module_not_found"
    assert hits[0]["extracted"]["package"] == "psycopg2"
    assert "psycopg2" in hits[0]["student_explanation"]


def test_node_missing_module():
    assert top_key([ev("docker", "Error: Cannot find module '@prisma/client'")]) == "node_module_not_found"


def test_oom_kill_from_exit_code():
    assert top_key([ev("docker", "team-beta exited with code 137")]) == "oom_killed"


def test_localhost_database_connection():
    events = [
        ev("docker", 'sqlalchemy.exc.OperationalError: connection to server at "localhost" (127.0.0.1), port 5432 failed: Connection refused')
    ]
    assert top_key(events) == "db_connection_refused_localhost"


def test_nginx_502_upstream_refused():
    events = [
        ev("nginx", 'connect() failed (111: Connection refused) while connecting to upstream, client: 10.0.0.5, upstream: "http://127.0.0.1:3011/", host: "team-a.apps.vnrvjiet.in"')
    ]
    hits = match_signatures(events)
    assert hits[0]["signature_key"] == "nginx_502_upstream_refused"
    assert hits[0]["extracted"].get("port") == "3011"


def test_missing_env_var_extracts_key():
    hits = match_signatures([ev("docker", "KeyError: 'DATABASE_URL'")])
    assert hits[0]["signature_key"] == "missing_env_var"
    assert hits[0]["extracted"]["key"] == "DATABASE_URL"


def test_migration_not_run():
    events = [ev("docker", 'psycopg.errors.UndefinedTable: relation "users" does not exist')]
    hits = match_signatures(events)
    assert hits[0]["signature_key"] == "db_relation_missing"
    assert any(f["action_type"] == "run_migration" and f["risk"] == "dangerous" for f in hits[0]["fixes"])


def test_disk_full():
    assert top_key([ev("actions", "failed to register layer: write /usr/lib/x.so: no space left on device")]) == "disk_full"


def test_no_runner_available():
    assert top_key([ev("actions", "No runner matching the specified labels was found: self-hosted, linux, viljaops")]) == "actions_no_runner"


def test_docker_socket_permission():
    events = [ev("actions", "Got permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock")]
    assert top_key(events) == "actions_permission_denied_docker"


def test_architecture_mismatch():
    assert top_key([ev("docker", "standard_init_linux.go:228: exec user process caused: exec format error")]) is not None


def test_dockerfile_copy_missing_path():
    events = [ev("actions", 'COPY failed: file not found in build context or excluded by .dockerignore: stat requirements.txt: file does not exist')]
    hits = match_signatures(events)
    assert hits[0]["signature_key"] == "dockerfile_copy_missing"


def test_secret_detection_is_highest_confidence():
    hits = match_signatures([ev("actions", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")])
    assert hits[0]["signature_key"] == "secret_committed"
    assert hits[0]["confidence"] >= 0.95


def test_confidence_rises_with_corroborating_sources():
    single = match_signatures([ev("docker", "Bind for 0.0.0.0:3000 failed: port is already allocated")])
    multi = match_signatures(
        [
            ev("docker", "Bind for 0.0.0.0:3000 failed: port is already allocated", i=1),
            ev("actions", "Error starting userland proxy: listen tcp 0.0.0.0:3000: bind: address already in use", i=2),
        ]
    )
    assert multi[0]["confidence"] > single[0]["confidence"]


def test_unrelated_noise_matches_nothing():
    events = [
        ev("docker", "INFO: Application startup complete.", level="info"),
        ev("nginx", "GET /api/items -> 200", level="info"),
    ]
    assert match_signatures(events) == []


def test_every_signature_fix_references_a_real_action():
    from app.rca.fixes import ACTION_CATALOG

    for sig in BY_KEY.values():
        for fix in sig.fixes:
            assert fix.action_type in ACTION_CATALOG, f"{sig.key} references unknown action '{fix.action_type}'"


def test_no_unformatted_placeholders_leak_into_output():
    """A signature whose groups don't match must not render literal {port}."""
    hits = match_signatures([ev("docker", "Restarting (1) 3 seconds ago")])
    for h in hits:
        for field in ("root_cause", "explanation", "student_explanation"):
            assert "{" not in h[field], f"{h['signature_key']}.{field} leaked a placeholder"
