"""Root cause engine, correlation, and the degraded path.

The critical property under test: with the LLM backend unreachable (conftest
points it at a dead port), the system still produces a correct, evidence-backed
diagnosis. A DevOps tool that fails when the GPU box reboots is not usable.
"""

from datetime import timedelta

import uuid

import pytest

from app.models import Deployment, DeploymentStatus, FixAction, Incident, LogEvent, MetricSample, Project, utcnow
from app.rca import collectors
from app.rca.correlate import build_timeline, fingerprint
from app.rca.engine import analyze_deployment
from app.rca.fixes import ACTION_CATALOG, can_auto_execute, spec_for


@pytest.fixture
def failed_deployment(db):
    project = Project(slug=f"rca-{uuid.uuid4().hex[:8]}", name="RCA Test Project")
    db.add(project)
    db.flush()
    dep = Deployment(
        project_id=project.id,
        commit_sha="abc123def456",
        status=DeploymentStatus.failed.value,
        container_name=project.slug,
        started_at=utcnow() - timedelta(minutes=5),
        finished_at=utcnow(),
    )
    db.add(dep)
    db.commit()
    return dep


def add_logs(db, dep, rows):
    base = utcnow() - timedelta(minutes=4)
    for i, (source, level, message) in enumerate(rows):
        db.add(
            LogEvent(
                deployment_id=dep.id,
                project_id=dep.project_id,
                source=source,
                level=level,
                message=message,
                ts=base + timedelta(seconds=i * 10),
            )
        )
    db.commit()


# --------------------------------------------------------------------------- #
def test_diagnoses_port_conflict_without_the_llm(db, failed_deployment):
    add_logs(
        db,
        failed_deployment,
        [
            ("actions", "info", "Successfully built image team-alpha:abc123"),
            ("docker", "error", "docker: Error response from daemon: Bind for 0.0.0.0:3000 failed: port is already allocated."),
            ("actions", "error", "##[error]Process completed with exit code 125."),
        ],
    )
    incident = analyze_deployment(db, failed_deployment.id)

    assert incident is not None
    assert incident.signature_key == "port_already_allocated"
    assert "3000" in incident.root_cause
    assert incident.confidence >= 0.9
    assert incident.analysis_source == "signature"  # LLM is unreachable in tests
    assert incident.evidence, "a diagnosis with no cited evidence is not trustworthy"
    assert incident.student_explanation

    fixes = db.query(FixAction).filter(FixAction.incident_id == incident.id).all()
    assert any(f.action_type == "reassign_port" for f in fixes)
    assert all(f.status == "proposed" for f in fixes), "nothing may be pre-approved"


def test_correlates_nginx_502_with_container_death(db, failed_deployment):
    add_logs(
        db,
        failed_deployment,
        [
            ("docker", "error", "team-alpha exited with code 137"),
            ("nginx", "error", 'connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://127.0.0.1:3011/"'),
            ("nginx", "error", 'connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://127.0.0.1:3011/"'),
        ],
    )
    timeline = build_timeline(db, failed_deployment.id)
    kinds = {s["kind"] for s in timeline["signals"]}
    assert "proxy_follows_container_death" in kinds, "the proxy/container link is the whole point of correlation"

    incident = analyze_deployment(db, failed_deployment.id)
    # Exit 137 is the cause; the 502 is the symptom. The engine must pick the cause.
    assert incident.signature_key == "oom_killed"


def test_repeated_lines_are_collapsed_not_duplicated(db, failed_deployment):
    add_logs(db, failed_deployment, [("docker", "error", f"Connection refused to 10.0.0.{i}:5432") for i in range(40)])
    timeline = build_timeline(db, failed_deployment.id)
    # 40 lines that differ only by IP are one problem, not forty.
    assert len(timeline["events"]) < 10
    assert max(e["count"] for e in timeline["events"]) > 20


def test_fingerprint_masks_volatile_values():
    a = fingerprint("Connection refused to 10.0.0.5:5432 at 2026-01-01T10:00:00Z req=abc123def456789012")
    b = fingerprint("Connection refused to 10.0.0.9:5432 at 2026-01-02T11:30:00Z req=fff999aaa888777666")
    assert a == b, "the same failure with different IDs should fingerprint identically"


def test_memory_pressure_surfaces_from_metrics(db, failed_deployment):
    add_logs(db, failed_deployment, [("docker", "error", "Some unrecognized failure")])
    base = utcnow() - timedelta(minutes=4)
    for i in range(20):
        db.add(
            MetricSample(
                deployment_id=failed_deployment.id,
                ts=base + timedelta(seconds=i * 10),
                mem_mb=480 + i,
                mem_limit_mb=512,
                cpu_pct=30,
            )
        )
    db.commit()
    timeline = build_timeline(db, failed_deployment.id)
    assert "memory_ceiling" in {s["kind"] for s in timeline["signals"]}


def test_unknown_failure_escalates_rather_than_guessing(db, failed_deployment):
    add_logs(db, failed_deployment, [("docker", "error", "Widget frobnicator returned an unexpected quux")])
    incident = analyze_deployment(db, failed_deployment.id)

    assert incident.confidence < 0.4, "an unmatched failure must not claim confidence"
    assert incident.status == "open"
    fixes = db.query(FixAction).filter(FixAction.incident_id == incident.id).all()
    assert [f.action_type for f in fixes] == ["escalate_to_devops"]


def test_deployment_with_no_logs_produces_no_incident(db, failed_deployment):
    assert analyze_deployment(db, failed_deployment.id) is None


# --------------------------------------------------------------------------- #
def test_dangerous_actions_never_auto_execute():
    for key, spec in ACTION_CATALOG.items():
        if spec.risk.value == "dangerous":
            assert can_auto_execute(key, auto_enabled=True) is False, f"{key} must never run unattended"


def test_auto_execution_is_off_unless_explicitly_enabled():
    assert can_auto_execute("prune_docker", auto_enabled=False) is False
    assert can_auto_execute("prune_docker", auto_enabled=True) is True


def test_advisory_actions_are_not_executable():
    assert spec_for("code_change").executable is False
    assert spec_for("add_dependency").executable is False
    # An unknown action degrades to escalation rather than being invented.
    assert spec_for("rm_minus_rf_everything").key == "escalate_to_devops"


# --------------------------------------------------------------------------- #
def test_actions_log_parser_handles_real_format():
    raw = (
        "2026-01-15T10:23:45.1234567Z ##[group]Run docker build\n"
        "2026-01-15T10:23:46.0000000Z Step 5/9 : RUN pip install -r requirements.txt\n"
        "2026-01-15T10:24:10.0000000Z ##[error]ERROR: Could not find a version that satisfies the requirement fastapii\n"
        "2026-01-15T10:24:11.0000000Z ##[endgroup]\n"
    )
    events = collectors.parse_actions_log(raw)
    assert len(events) == 2  # group/endgroup markers are structural, not content
    assert events[-1]["level"] == "error"
    assert "fastapii" in events[-1]["message"]


def test_actions_window_keeps_every_error_even_in_a_huge_log():
    events = [{"level": "info", "message": f"line {i}", "ts": None, "source": "actions"} for i in range(5000)]
    events[10]["level"] = "error"
    events[10]["message"] = "early error"
    events[4990]["level"] = "error"
    events[4990]["message"] = "late error"
    window = collectors.extract_actions_failure_window(events)
    messages = {e["message"] for e in window}
    assert "early error" in messages and "late error" in messages
    assert len(window) < 200, "the window must stay small enough for a 14B model's context"


def test_nginx_access_log_stats():
    lines = "\n".join(
        [
            '10.0.0.1 - - [15/Jan/2026:10:00:00 +0000] "GET /api/x HTTP/1.1" 200 512 "-" "curl" rt=0.05',
            '10.0.0.2 - - [15/Jan/2026:10:00:01 +0000] "GET /api/y HTTP/1.1" 500 128 "-" "curl" rt=2.10',
            '10.0.0.3 - - [15/Jan/2026:10:00:02 +0000] "GET /api/y HTTP/1.1" 500 128 "-" "curl" rt=4.00',
        ]
    )
    stats = collectors.access_log_stats(lines)
    assert stats["requests"] == 3
    assert stats["http_5xx"] == 2
    assert stats["top_failing_paths"][0][0] == "/api/y"


def test_docker_log_parser_keeps_stack_traces_together():
    raw = (
        "2026-01-15T10:00:00.000Z Traceback (most recent call last):\n"
        '  File "/app/main.py", line 3, in <module>\n'
        "    import psycopg2\n"
        "2026-01-15T10:00:00.001Z ModuleNotFoundError: No module named 'psycopg2'\n"
    )
    events = collectors.parse_docker_log(raw, container="team-a")
    assert len(events) == 2
    assert "File \"/app/main.py\"" in events[0]["message"], "stack trace lines must stay attached to their traceback"
    assert events[1]["level"] == "error"


def test_nonzero_exit_is_classified_as_an_error():
    """A dying container contains no error word — it must still read as one."""
    assert collectors.guess_level("team-alpha exited with code 137") == "error"
    assert collectors.guess_level("Container web died (1)") == "error"
    assert collectors.guess_level("process exited with code 0") == "info"
    assert collectors.guess_level("Listening on port 8000") == "info"


def test_container_death_is_not_reported_as_never_started(db, failed_deployment):
    """Regression: exit-137 logs once read as 'info', so correlation claimed the
    pipeline failed before runtime — the opposite of what happened."""
    add_logs(
        db,
        failed_deployment,
        [
            ("actions", "error", "##[error]Process completed with exit code 137."),
            ("docker", "info", "team-alpha exited with code 137"),
        ],
    )
    timeline = build_timeline(db, failed_deployment.id)
    kinds = {s["kind"] for s in timeline["signals"]}
    assert "failed_before_runtime" not in kinds
