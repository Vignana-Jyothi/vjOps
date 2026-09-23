"""Layer 1 on synthetic repositories that reproduce real student mistakes."""

import json
import subprocess
from pathlib import Path

import pytest

from app.analyzers import secrets as secret_scanner
from app.analyzers.detectors import detect
from app.analyzers.plan import recommend_resources
from app.analyzers.repo import analyze


def write(root: Path, rel: str, content: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


@pytest.fixture
def good_repo(tmp_path):
    r = tmp_path / "good"
    write(r, "requirements.txt", "fastapi==0.115.6\nuvicorn==0.34.0\npsycopg[binary]==3.2.3\n")
    write(
        r,
        "app/main.py",
        """import os
from fastapi import FastAPI

app = FastAPI()
DATABASE_URL = os.environ["DATABASE_URL"]
SECRET_KEY = os.getenv("SECRET_KEY")

@app.get("/health")
def health():
    return {"status": "ok"}
""",
    )
    write(
        r,
        "Dockerfile",
        """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app
USER app
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
    )
    write(r, ".dockerignore", "node_modules\n.env\n.git\n")
    write(r, ".gitignore", ".env\n__pycache__\n")
    write(r, ".env.example", "DATABASE_URL=postgresql://user:pass@postgres:5432/db\nSECRET_KEY=changeme\n")
    write(r, "README.md", "# Good project\n")
    write(r, "tests/test_health.py", "def test_health():\n    assert True\n")
    write(r, ".github/workflows/deploy.yml", "name: Deploy\non: push\n")
    write(r, "poetry.lock", "# lock\n")
    return r


@pytest.fixture
def bad_repo(tmp_path):
    r = tmp_path / "bad"
    write(r, "requirements.txt", "flask\n")
    write(
        r,
        "app.py",
        """import os
import psycopg2

conn = psycopg2.connect("postgresql://admin:hunter2@localhost:5432/mydb")
API_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"

from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "hello"

app.run(host="0.0.0.0", port=5000)
""",
    )
    write(r, ".env", "DATABASE_URL=postgresql://admin:hunter2@localhost:5432/mydb\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
    write(r, "docker-compose.yml", 'services:\n  web:\n    build: .\n    ports:\n      - "5000:5000"\n')
    write(r, "frontend/src/api.js", "export const API = 'http://localhost:5000/api';\n")
    return r


# --------------------------------------------------------------------------- #
def test_detects_python_fastapi_stack(good_repo):
    d = detect(good_repo)
    assert d["primary_language"] == "python"
    assert "fastapi" in d["frameworks"]
    assert "postgresql" in d["databases"]
    assert "DATABASE_URL" in d["env_vars"]
    assert d["has_health_endpoint"] is True
    assert d["dockerfile"]["has_healthcheck"] is True
    assert d["dockerfile"]["installs_before_copy"] is True
    assert d["dockerfile"]["runs_as_root"] is False


def test_good_repo_scores_well_and_has_no_blockers(good_repo):
    result = analyze(good_repo, use_llm=False)
    assert result["blocking_count"] == 0
    assert result["score"] >= 75, f"scored {result['score']}: {[f['title'] for f in result['findings']]}"
    assert result["verdict"] in ("ready", "ready_with_changes")


def test_bad_repo_is_blocked_and_names_the_reasons(bad_repo):
    result = analyze(bad_repo, use_llm=False)
    ids = {f["id"] for f in result["findings"]}

    assert result["blocking_count"] > 0
    assert result["score"] <= 45
    assert result["verdict"] == "not_ready"
    assert "env_committed" in ids
    assert "secrets_in_repo" in ids
    assert "localhost_hardcoded" in ids
    assert "hardcoded_host_ports" in ids
    assert "no_healthcheck" in ids
    assert "no_dockerfile_for_build" in ids  # compose builds from a Dockerfile that is not there


def test_findings_always_carry_an_actionable_fix(bad_repo):
    result = analyze(bad_repo, use_llm=False)
    for f in result["findings"]:
        assert f["fix"] and len(f["fix"]) > 20, f"finding {f['id']} has no useful fix text"
        assert f["detail"], f"finding {f['id']} has no explanation"


def test_generated_dockerfile_is_usable(bad_repo):
    result = analyze(bad_repo, use_llm=False)
    dockerfile = result["artifacts"].get("Dockerfile")
    assert dockerfile, "a repo with no container definition should get a generated Dockerfile"
    assert "FROM python:" in dockerfile
    # The whole point of the generated file is correct layer ordering.
    assert dockerfile.index("COPY requirements.txt") < dockerfile.index("COPY . .")
    assert "HEALTHCHECK" in dockerfile
    assert "USER app" in dockerfile


def test_secret_scanner_finds_real_keys_and_masks_them(bad_repo):
    findings = secret_scanner.scan(bad_repo)
    rules = {f["rule"] for f in findings}
    assert "aws_access_key" in rules
    assert "openai_key" in rules
    for f in findings:
        assert "AKIAIOSFODNN7EXAMPLE" not in f["preview"], "a live-looking key was echoed back unmasked"


def test_secret_scanner_ignores_example_files(tmp_path):
    r = tmp_path / "examples"
    write(r, ".env.example", "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234\nPASSWORD=changeme\n")
    findings = secret_scanner.scan(r)
    assert all(f["severity"] == "low" for f in findings), "example files should not raise high-severity findings"


def test_placeholder_values_are_not_flagged(tmp_path):
    r = tmp_path / "placeholders"
    write(r, "config.py", 'PASSWORD = "your-password-here"\nAPI_KEY = "xxxxxxxxxxxx"\n')
    findings = [f for f in secret_scanner.scan(r) if f["severity"] in ("critical", "high", "medium")]
    assert findings == []


def test_resource_recommendation_scales_with_stack(good_repo, tmp_path):
    light = recommend_resources(detect(good_repo), expected_users=20, use_llm=False)

    ml = tmp_path / "ml"
    write(ml, "requirements.txt", "torch==2.5.1\ntransformers==4.47.0\nfastapi\n")
    write(ml, "main.py", "import torch\n")
    heavy = recommend_resources(detect(ml), expected_users=500, use_llm=False)

    assert heavy["ram_mb"] > light["ram_mb"]
    assert heavy["needs_gpu"] is True
    assert any("model" in w.lower() for w in heavy["warnings"])


def test_deployment_plan_blocks_when_blockers_exist(bad_repo):
    result = analyze(bad_repo, use_llm=False)
    assert result["plan"]["deployable_now"] is False
    assert result["plan"]["blocker_count"] > 0
    # Blockers must be the first thing the plan asks for.
    assert result["plan"]["steps"][0]["owner"] == "team"


def test_git_history_check_finds_removed_env_file(tmp_path):
    r = tmp_path / "history"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    write(r, ".env", "SECRET=abc123\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "oops"], cwd=r, check=True)
    (r / ".env").unlink()
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "remove secret"], cwd=r, check=True)

    result = secret_scanner.check_git_history(r)
    assert result["checked"] is True
    assert ".env" in result["ever_committed_secrets"]
