"""Layer 1 — deployment readiness.

Answers the question the DevOps team asks fifty times a semester: *can we
actually deploy this, and what will break if we try?*

Scoring is rule-based and reproducible so two students with the same problems
get the same score, and a team can watch their score climb as they fix things.
The model only adds commentary on top; it never moves the number.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from ..config import settings
from ..llm.client import llm
from ..llm.prompts import READINESS_SYSTEM
from . import secrets as secret_scanner
from .detectors import detect
from .plan import build_plan, generate_artifacts

WEIGHTS = {
    "containerization": 25,
    "configuration": 20,
    "security": 25,
    "operability": 15,
    "build_quality": 15,
}

BLOCKER = "blocker"
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

PENALTY = {BLOCKER: 1.0, CRITICAL: 0.6, WARNING: 0.3, INFO: 0.0}


def clone_repo(repo: str, dest: Path, *, ref: str = "", token: str = "") -> dict:
    """Shallow clone into the cache dir. Never checks out onto a shared path."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = repo
    tok = token or settings.github_token
    if tok:
        if "://" not in repo:
            url = f"https://x-access-token:{tok}@github.com/{repo}.git"
        elif "://" in repo and "@" not in repo:
            # Inject token into a full URL (e.g. https://github.com/... -> https://x-access-token:token@github.com/...)
            parts = repo.split("://", 1)
            url = f"{parts[0]}://x-access-token:{tok}@{parts[1]}"
    elif "://" not in repo:
        url = f"https://github.com/{repo}.git"

    cmd = ["git", "clone", "--depth", "50", "--no-tags"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dest)]

    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.clone_timeout_s)
    if proc.returncode != 0:
        # Never let a token appear in an error surfaced to the UI.
        err = proc.stderr.replace(settings.github_token or "\0", "***") if settings.github_token else proc.stderr
        return {"ok": False, "error": err[-800:], "duration_s": round(time.time() - started, 1)}

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()
    size_mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1_048_576
    return {"ok": True, "commit_sha": sha, "size_mb": round(size_mb, 1), "duration_s": round(time.time() - started, 1)}


# --------------------------------------------------------------------------- #
def analyze(root: Path, *, use_llm: bool = True) -> dict:
    started = time.time()
    detected = detect(root)
    secret_findings = secret_scanner.scan(root)
    history = secret_scanner.check_git_history(root)

    findings: list[dict] = []
    add = findings.append

    df = detected.get("dockerfile") or {}
    compose = detected.get("compose") or {}

    # ---------------- containerization ----------------
    if not detected["dockerfiles"] and not detected["compose_files"]:
        add(_f("no_container_definition", "containerization", BLOCKER,
               "No Dockerfile or compose file",
               "There is nothing describing how to build and run this project, so it cannot be deployed to the shared server.",
               "Add a Dockerfile. The generated starter below matches your detected stack."))
    else:
        if compose.get("build_services") and not detected["dockerfiles"]:
            add(_f("no_dockerfile_for_build", "containerization", BLOCKER,
                   "Compose builds from a Dockerfile that does not exist",
                   "Service(s) " + ", ".join(compose["build_services"]) +
                   " declare a build context, but there is no Dockerfile in the repository. "
                   "The build will fail immediately with 'failed to read dockerfile'.",
                   "Add a Dockerfile at the build context path. The generated starter below matches your detected stack."))
        if df:
            if df.get("uses_latest_tag"):
                add(_f("unpinned_base_image", "containerization", WARNING,
                       "Base image is not pinned to a version",
                       f"FROM {df.get('base_image')} will silently change when upstream publishes a new image, so a build that works today can break tomorrow with no code change.",
                       "Pin a specific tag, e.g. python:3.12-slim or node:20-alpine."))
            if not df.get("has_expose"):
                add(_f("no_expose", "containerization", WARNING,
                       "Dockerfile does not EXPOSE a port",
                       "Nothing declares which port the app listens on, so the port has to be guessed when wiring up Nginx.",
                       "Add EXPOSE <port> matching the port your server binds."))
            if not df.get("has_entrypoint"):
                add(_f("no_entrypoint", "containerization", BLOCKER,
                       "Dockerfile has no CMD or ENTRYPOINT",
                       "The container will start and immediately exit because nothing tells it what to run.",
                       "Add a CMD that starts your server in the foreground."))
            if not df.get("installs_before_copy"):
                add(_f("bad_layer_order", "containerization", WARNING,
                       "Dependencies are installed after copying all source",
                       "Every code change invalidates the dependency layer, so each deploy reinstalls everything. Builds take minutes instead of seconds and are far more likely to time out.",
                       "COPY the manifest, RUN the install, then COPY the rest of the source."))
            if df.get("runs_as_root"):
                add(_f("container_root", "containerization", WARNING,
                       "Container runs as root",
                       "On a shared server, a compromised root container is a much bigger problem than a compromised unprivileged one.",
                       "Create a non-root user and add a USER instruction before CMD (e.g. 'RUN useradd -m appuser' followed by 'USER appuser')."))
            if df.get("apt_without_cleanup"):
                add(_f("apt_no_cleanup", "containerization", INFO,
                       "apt lists not cleaned up",
                       "The image carries tens of MB of package index it will never use.",
                       "Append && rm -rf /var/lib/apt/lists/* to the apt-get install layer."))
        if not detected["has_dockerignore"]:
            add(_f("no_dockerignore", "containerization", WARNING,
                   "No .dockerignore",
                   "node_modules, .git, local virtualenvs and .env get sent into the build context — slow builds, bloated images, and a real risk of baking secrets into a layer.",
                   "Add a .dockerignore (a generated one is included below)."))

    published = (compose.get("published_ports") or [])
    fixed_ports = [p for p in published if p.get("host_port")]
    if fixed_ports:
        add(_f("hardcoded_host_ports", "containerization", CRITICAL,
               "Host ports are hard-coded in the compose file",
               "Ports " + ", ".join(str(p["host_port"]) for p in fixed_ports) +
               " are claimed directly. On a server shared by many teams this collides sooner or later, and the deploy fails with 'port is already allocated'.",
               "Publish through a variable (\"${HOST_PORT}:3000\") and let the platform's port registry assign it."))

    # ---------------- configuration ----------------
    env_keys = list((detected.get("env_vars") or {}).keys())
    if env_keys and not detected["has_env_example"]:
        add(_f("no_env_example", "configuration", CRITICAL,
               f"{len(env_keys)} environment variables used, none documented",
               "The code reads " + ", ".join(env_keys[:6]) + ("…" if len(env_keys) > 6 else "") +
               " but there is no .env.example, so whoever deploys this has to reverse-engineer the configuration from the source.",
               "Commit a .env.example listing every key with a dummy value."))

    if detected["committed_env_files"]:
        add(_f("env_committed", "configuration", BLOCKER,
               "A real .env file is committed to the repository",
               "Committed: " + ", ".join(detected["committed_env_files"]) +
               ". Anything in it must be treated as public and rotated.",
               "Remove it from git, add .env to .gitignore, and rotate every value it contained."))

    if detected["localhost_references"]:
        files = sorted({r["file"] for r in detected["localhost_references"]})[:4]
        add(_f("localhost_hardcoded", "configuration", CRITICAL,
               "Application code points at localhost",
               "Found in " + ", ".join(files) +
               ". Inside a container 'localhost' is the container itself, and in a browser bundle it is the visitor's own machine — this is the single most common reason a project that 'works on my laptop' fails after deployment.",
               "Read the API and database hosts from environment variables, with localhost only as the local-development default."))

    if detected["databases"] and not any(k for k in env_keys if "DATABASE" in k or "DB_" in k or "MONGO" in k or "POSTGRES" in k or "MYSQL" in k):
        add(_f("db_no_config", "configuration", CRITICAL,
               f"Database detected ({', '.join(detected['databases'])}) with no configurable connection",
               "The connection details appear to be hard-coded, so the app cannot be pointed at the server's database without editing code.",
               "Move the connection string into an environment variable such as DATABASE_URL."))

    if "sqlite" in detected["databases"] and not (compose.get("has_named_volumes")):
        add(_f("sqlite_no_volume", "configuration", CRITICAL,
               "SQLite database with no persistent volume",
               "The database file lives inside the container filesystem, so every redeploy silently destroys all data.",
               "Mount a named volume for the database file, or move to PostgreSQL."))

    # ---------------- security ----------------
    real_secrets = [s for s in secret_findings if not s["in_example_file"] and s["severity"] in ("critical", "high")]
    if real_secrets:
        add(_f("secrets_in_repo", "security", BLOCKER,
               f"{len(real_secrets)} credential(s) found in the repository",
               "; ".join(f"{s['label']} in {s['file']}:{s['line']}" for s in real_secrets[:5]) +
               ". These are in git history permanently and must be considered compromised.",
               "Revoke and reissue each credential, then move the values to platform-managed environment variables.",
               evidence=real_secrets[:8]))

    medium_secrets = [s for s in secret_findings if not s["in_example_file"] and s["severity"] == "medium"]
    if medium_secrets:
        add(_f("possible_secrets", "security", WARNING,
               f"{len(medium_secrets)} possible hardcoded credential(s)",
               "These look like passwords or API keys but could be placeholders — worth a human glance.",
               "Confirm each one and move real values into environment variables.",
               evidence=medium_secrets[:8]))

    if history.get("ever_committed_secrets"):
        add(_f("secret_in_history", "security", CRITICAL,
               "Sensitive files exist in git history",
               "Previously committed: " + ", ".join(history["ever_committed_secrets"]) +
               ". Deleting a file does not remove it from history.",
               "Rotate the credentials. Rewriting history is optional; rotation is not."))

    if not detected["has_gitignore"]:
        add(_f("no_gitignore", "security", WARNING, "No .gitignore",
               "Without one, secrets and build output get committed by accident.",
               "Add a .gitignore for your stack, including .env."))

    # ---------------- operability ----------------
    if not detected["has_health_endpoint"]:
        add(_f("no_healthcheck", "operability", CRITICAL,
               "No health endpoint",
               "Nothing can distinguish 'running' from 'running but broken'. Deployments cannot be verified, and a hung app keeps receiving traffic.",
               "Add GET /health returning 200 with a small JSON body, and a Docker HEALTHCHECK that calls it."))
    elif df and not df.get("has_healthcheck"):
        add(_f("no_docker_healthcheck", "operability", WARNING,
               "Health endpoint exists but Docker does not use it",
               "Docker reports the container as healthy as long as the process is alive, even when the app is not serving.",
               "Add a HEALTHCHECK instruction that curls your health endpoint."))

    if compose and not compose.get("uses_restart_policy"):
        add(_f("no_restart_policy", "operability", WARNING,
               "No restart policy",
               "The app will stay down after a server reboot or a transient crash.",
               "Set restart: unless-stopped on your services."))

    if compose and not compose.get("has_resource_limits"):
        add(_f("no_resource_limits", "operability", WARNING,
               "No memory limit on containers",
               "On a shared server, one runaway container can exhaust the host's RAM and take other teams' projects down with it.",
               "Set a memory limit per service based on the recommendation below."))

    if not detected["has_readme"]:
        add(_f("no_readme", "operability", INFO, "No README",
               "Reviewers and future teammates have no starting point.",
               "Describe what the project does, how to run it, and what configuration it needs."))

    # ---------------- build quality ----------------
    if not detected["workflows"]:
        add(_f("no_ci", "build_quality", CRITICAL,
               "No GitHub Actions workflow",
               "There is no automated build, so deployment is entirely manual.",
               "Add the incubator's reusable deploy workflow (generated below)."))

    if not detected["has_lockfile"] and detected["primary_language"] in ("javascript", "typescript", "python"):
        add(_f("no_lockfile", "build_quality", WARNING,
               "No dependency lockfile",
               "Builds are not reproducible — the server can resolve different versions than your laptop did, which is a classic 'works locally, fails in CI' cause.",
               "Commit package-lock.json / poetry.lock / requirements pinned with ==."))

    if not detected["has_tests"]:
        add(_f("no_tests", "build_quality", WARNING, "No tests found",
               "Nothing gates a broken deploy.",
               "Even three smoke tests on your main endpoints prevent most bad deploys."))

    if detected["ml_workload"]:
        add(_f("ml_workload", "build_quality", INFO,
               "Machine-learning dependencies detected",
               "Model weights and CUDA images make containers large and memory-hungry; this project may need a dedicated resource envelope.",
               "Load models once at startup, not per request, and declare the memory you need."))

    # ---------------- score ----------------
    breakdown = _score(findings)
    total = sum(breakdown[c]["earned"] for c in WEIGHTS)
    blockers = [f for f in findings if f["severity"] == BLOCKER]
    score = 0 if blockers and total > 40 else int(round(total))
    if blockers:
        score = min(score, 45)  # cannot be "ready" with a blocker outstanding

    plan = build_plan(detected, findings)
    artifacts = generate_artifacts(root, detected, findings)

    result = {
        "score": score,
        "grade": _grade(score),
        "verdict": _verdict(score, blockers),
        "breakdown": breakdown,
        "detected": detected,
        "findings": findings,
        "blocking_count": len(blockers),
        "secret_findings": secret_findings,
        "git_history": history,
        "plan": plan,
        "artifacts": artifacts,
        "duration_ms": int((time.time() - started) * 1000),
    }

    if use_llm:
        result["llm_review"] = _llm_review(result)
    return result


def _f(fid, category, severity, title, detail, fix, evidence=None) -> dict:
    return {
        "id": fid,
        "category": category,
        "severity": severity,
        "title": title,
        "detail": detail,
        "fix": fix,
        "evidence": evidence or [],
    }


def _score(findings: list[dict]) -> dict:
    out = {}
    for cat, weight in WEIGHTS.items():
        cat_findings = [f for f in findings if f["category"] == cat]
        lost = sum(PENALTY[f["severity"]] for f in cat_findings)
        # Each "penalty unit" costs a third of the category, floored at zero.
        earned = max(0.0, weight * (1 - min(1.0, lost / 3.0)))
        out[cat] = {
            "weight": weight,
            "earned": round(earned, 1),
            "issues": len(cat_findings),
            "blockers": len([f for f in cat_findings if f["severity"] == BLOCKER]),
        }
    return out


def _grade(score: int) -> str:
    for cutoff, g in ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")):
        if score >= cutoff:
            return g
    return "F"


def _verdict(score: int, blockers: list) -> str:
    if blockers:
        return "not_ready"
    if score >= 80:
        return "ready"
    return "ready_with_changes"


def _llm_review(result: dict) -> str:
    import json

    detected = result["detected"]
    payload = {
        "detected": {
            "primary_language": detected["primary_language"],
            "frameworks": detected["frameworks"],
            "databases": detected["databases"],
            "ml_workload": detected["ml_workload"],
            "env_var_count": len(detected["env_vars"]),
            "ports": detected["ports"],
            "dockerfile": detected.get("dockerfile"),
            "compose": detected.get("compose"),
            "approx_loc": detected["approx_loc"],
        },
        "score": result["score"],
        "finding_titles": [f"{f['severity']}: {f['title']}" for f in result["findings"]],
    }
    verdict = llm.complete_json(
        READINESS_SYSTEM,
        json.dumps(payload, indent=2)[:12000],
        fallback={},
    )
    if not verdict.get("_llm_used"):
        return ""
    parts = []
    if verdict.get("summary"):
        parts.append(str(verdict["summary"]))
    if verdict.get("top_priority"):
        parts.append(f"**Fix first:** {verdict['top_priority']}")
    for risk in (verdict.get("architectural_risks") or [])[:4]:
        parts.append(f"- {risk}")
    if verdict.get("student_message"):
        parts.append(f"\n_For the team:_ {verdict['student_message']}")
    return "\n\n".join(parts)[:4000]
