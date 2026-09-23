"""Anonymization and dataset export.

This is the pipeline that decides what leaves the incubator in a training file,
so the tests are about what must NOT survive it — and equally, about the
technical structure that must.
"""

import json
import uuid
from datetime import timedelta

import pytest

from app.dataset.anonymize import Anonymizer
from app.dataset.export import build_records, export
from app.models import Deployment, DeploymentStatus, Incident, IncidentStatus, LogEvent, Project, utcnow


def test_secrets_are_destroyed_not_pseudonymized():
    a = Anonymizer()
    out = a.scrub("export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert out.count("[REDACTED_SECRET]") >= 2


def test_database_url_password_is_removed():
    a = Anonymizer()
    out = a.scrub("postgresql://appuser:sup3rs3cret@db.internal:5432/appdb")
    assert "sup3rs3cret" not in out
    assert "5432" in out, "the port is technical structure and must survive"


def test_emails_and_home_paths_are_pseudonymized_consistently():
    a = Anonymizer()
    text = "error for rakesh@vnrvjiet.in in /home/rakesh/project and again rakesh@vnrvjiet.in"
    out = a.scrub(text)
    assert "rakesh" not in out
    # The same person maps to the same alias within one record.
    assert out.count("user_1") == 3


def test_different_records_do_not_share_aliases():
    a, b = Anonymizer(), Anonymizer()
    assert a.scrub("mail: one@x.edu") == b.scrub("mail: two@y.edu"), "aliases are per-record by design"


def test_technical_structure_survives_scrubbing():
    a = Anonymizer()
    line = "ModuleNotFoundError: No module named 'psycopg2' at /app/main.py line 4, bind 0.0.0.0:3000 failed"
    out = a.scrub(line)
    assert "ModuleNotFoundError" in out
    assert "psycopg2" in out
    assert "/app/main.py" in out
    assert "0.0.0.0:3000" in out, "a dataset that loses the port teaches nothing about port conflicts"


def test_localhost_is_preserved_because_it_is_the_bug():
    a = Anonymizer()
    out = a.scrub("connecting to http://localhost:5432 failed")
    assert "localhost:5432" in out


def test_public_registries_are_not_pseudonymized():
    a = Anonymizer()
    out = a.scrub("downloading from https://pypi.org/simple and https://github.com/org/repo")
    assert "pypi.org" in out
    assert "github.com" in out


def test_internal_hostnames_are_pseudonymized():
    a = Anonymizer()
    out = a.scrub("proxying to https://team-alpha.apps.vnrvjiet.in/api")
    assert "vnrvjiet.in" not in out
    assert "example.edu" in out


# --------------------------------------------------------------------------- #
@pytest.fixture
def confirmed_incident(db):
    project = Project(
        slug=f"ds-{uuid.uuid4().hex[:8]}",
        name="Dataset Project",
        github_repo="vnrvjiet-incubator/secret-team-name",
    )
    db.add(project)
    db.flush()
    dep = Deployment(
        project_id=project.id,
        commit_sha="1234abcd",
        status=DeploymentStatus.failed.value,
        started_at=utcnow() - timedelta(minutes=5),
        finished_at=utcnow(),
    )
    db.add(dep)
    db.flush()
    db.add(
        LogEvent(
            deployment_id=dep.id,
            project_id=project.id,
            source="docker",
            level="error",
            message="ModuleNotFoundError: No module named 'psycopg2' (reported by student@vnrvjiet.in)",
            ts=utcnow() - timedelta(minutes=4),
        )
    )
    inc = Incident(
        project_id=project.id,
        deployment_id=dep.id,
        title="Python dependency missing from the image",
        status=IncidentStatus.resolved.value,
        severity="high",
        stage="runtime",
        signature_key="python_module_not_found",
        root_cause="psycopg2 is imported but not installed",
        explanation="The package was never added to requirements.txt.",
        evidence=[{"id": "E1", "source": "docker", "line": "ModuleNotFoundError"}],
        confidence=0.95,
        analysis_source="signature",
        confirmed_root_cause="psycopg2-binary was missing from requirements.txt",
        confirmed_fix="Added psycopg2-binary==2.9.9 and rebuilt",
        was_ai_correct=True,
        resolved_at=utcnow(),
    )
    db.add(inc)
    db.commit()
    return inc


def test_only_confirmed_incidents_are_exported(db, confirmed_incident):
    unconfirmed = Incident(
        project_id=confirmed_incident.project_id,
        title="Not yet confirmed",
        status=IncidentStatus.diagnosed.value,
        root_cause="a guess",
    )
    db.add(unconfirmed)
    db.commit()

    records = build_records(db)
    titles = [r["messages"][2]["content"] for r in records]
    assert not any("a guess" in t for t in titles), "training on unconfirmed AI guesses would compound its own errors"


def test_exported_record_has_training_shape_and_is_clean(db, confirmed_incident):
    records = build_records(db)
    rec = next(r for r in records if r["metadata"]["signature_key"] == "python_module_not_found")

    roles = [m["role"] for m in rec["messages"]]
    assert roles == ["system", "user", "assistant"]

    blob = json.dumps(rec)
    assert "student@vnrvjiet.in" not in blob
    assert "secret-team-name" not in blob
    assert "psycopg2" in blob, "the actual failure must survive anonymization"

    completion = json.loads(rec["messages"][2]["content"])
    assert completion["root_cause"] == "psycopg2-binary was missing from requirements.txt"
    assert rec["metadata"]["confirmed"] is True
    assert rec["metadata"]["ai_was_correct"] is True


def test_intervention_metadata_reflects_verification_not_just_diagnosis(db, confirmed_incident):
    """diagnosis -> intervention -> outcome, not diagnosis -> label: the
    export should say what was actually done and whether it held, without
    that leaking into the training target itself."""
    from app.dataset.anonymize import stable_id
    from app.models import FixAction, FixStatus

    fix = FixAction(
        incident_id=confirmed_incident.id,
        action_type="add_dependency",
        title="Add psycopg2-binary",
        rationale="missing import",
        params={"package": "psycopg2-binary"},
        risk="safe",
        requires_code_change=True,
        order_index=1,
        status=FixStatus.succeeded.value,
        verification_status="passed",
    )
    db.add(fix)
    db.commit()

    records = build_records(db)
    rec = next(r for r in records if r["id"] == stable_id(confirmed_incident.id))

    intervention = rec["metadata"]["intervention"]
    assert intervention["resolution"] == "verified"
    assert intervention["fix_actions"] == [
        {"action_type": "add_dependency", "status": "succeeded", "verification_status": "passed"}
    ]
    # Must not have leaked into what the model is trained to produce.
    completion_blob = rec["messages"][2]["content"]
    assert "verification_status" not in completion_blob
    assert "resolution" not in completion_blob


def test_intervention_metadata_with_no_fix_actions(db, confirmed_incident):
    from app.dataset.anonymize import stable_id

    records = build_records(db)
    rec = next(r for r in records if r["id"] == stable_id(confirmed_incident.id))
    assert rec["metadata"]["intervention"] == {"resolution": "no_remediation_attempted", "fix_actions": []}


def test_export_writes_split_files_and_a_data_card(db, confirmed_incident, tmp_path):
    card = export(tmp_path / "incidents.jsonl", eval_fraction=0.2)
    assert card["total_records"] >= 1

    train = tmp_path / "incidents.train.jsonl"
    cardfile = tmp_path / "incidents.card.json"
    assert train.exists() and cardfile.exists()

    for line in train.read_text().splitlines():
        json.loads(line)  # every line must be valid JSON

    meta = json.loads(cardfile.read_text())
    assert meta["confirmed_only"] is True
    assert meta["caveats"], "a dataset card without caveats invites misuse"
