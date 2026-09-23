"""Deployment plan + generated starter artifacts + resource sizing.

The point of generating real files (not advice) is that a student can copy a
working Dockerfile into their repo and be deployable in ten minutes, instead of
reading a checklist and guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..llm.client import llm
from ..llm.prompts import INFRA_SYSTEM

# --------------------------------------------------------------------------- #
# Resource sizing
# --------------------------------------------------------------------------- #
BASE_RAM = {
    "fastapi": 256, "flask": 256, "django": 512, "streamlit": 512,
    "express": 256, "nestjs": 384, "next": 512, "react": 128, "vue": 128,
    "angular": 128, "svelte": 128, "spring-boot": 1024, "rails": 512,
    "laravel": 384, "gin": 128,
}
SERVICE_RAM = {"postgresql": 512, "mysql": 512, "mongodb": 512, "redis": 128, "elasticsearch": 2048}


def recommend_resources(detected: dict, *, expected_users: int = 50, use_llm: bool = True) -> dict:
    frameworks = detected.get("frameworks") or {}
    dbs = detected.get("databases") or []
    ml = detected.get("ml_workload")

    ram = 128
    for fw in frameworks:
        ram = max(ram, BASE_RAM.get(fw, 256))
    if frameworks.get("next") or "react" in frameworks or "vue" in frameworks:
        ram += 128  # static serving container

    services = [d for d in dbs if d in SERVICE_RAM]
    for svc in services:
        ram += SERVICE_RAM[svc]

    if ml:
        ram += 2048

    # Concurrency headroom: roughly log-scaled with expected users.
    if expected_users > 500:
        ram = int(ram * 2.0)
    elif expected_users > 100:
        ram = int(ram * 1.5)

    ram = max(512, int(round(ram / 256) * 256))
    cpu = 1.0
    if ml:
        cpu = 2.0
    if expected_users > 200:
        cpu = max(cpu, 2.0)
    if "elasticsearch" in services or "spring-boot" in frameworks:
        cpu = max(cpu, 2.0)

    container_count = 1 + len(services) + (1 if detected.get("frontend") and detected.get("backend") else 0)
    disk = 10 + (10 if ml else 0) + 5 * len(services)

    warnings = []
    if ml:
        warnings.append(
            "ML dependencies detected. Load models once at process start — loading per request "
            "will exhaust memory under any real concurrency."
        )
    if "sqlite" in dbs:
        warnings.append(
            "SQLite does not tolerate concurrent writers. Move to PostgreSQL before this project "
            "has more than a handful of simultaneous users."
        )
    if detected.get("localhost_references"):
        warnings.append(
            "Hard-coded localhost references will break as soon as this runs anywhere but a laptop."
        )
    if not detected.get("has_health_endpoint"):
        warnings.append("No health endpoint, so autoscaling and restart-on-failure cannot work correctly.")

    rec = {
        "cpu_cores": cpu,
        "ram_mb": ram,
        "disk_gb": disk,
        "needs_gpu": bool(ml),
        "services": services,
        "container_count": container_count,
        "expected_users": expected_users,
        "warnings": warnings,
        "source": "heuristic",
    }

    if use_llm:
        verdict = llm.complete_json(
            INFRA_SYSTEM,
            json.dumps(
                {
                    "frameworks": frameworks,
                    "databases": dbs,
                    "ml_workload": ml,
                    "approx_loc": detected.get("approx_loc"),
                    "expected_users": expected_users,
                    "heuristic_baseline": {k: rec[k] for k in ("cpu_cores", "ram_mb", "disk_gb")},
                },
                indent=2,
            ),
            fallback={},
        )
        if verdict.get("_llm_used"):
            # The model may raise the envelope but never shrink it below the floor.
            try:
                rec["ram_mb"] = max(rec["ram_mb"], int(verdict.get("ram_mb") or 0))
                rec["cpu_cores"] = max(rec["cpu_cores"], float(verdict.get("cpu_cores") or 0))
                rec["disk_gb"] = max(rec["disk_gb"], int(verdict.get("disk_gb") or 0))
            except (TypeError, ValueError):
                pass
            extra = [w for w in (verdict.get("warnings") or []) if isinstance(w, str)]
            rec["warnings"] = rec["warnings"] + extra[:4]
            rec["scaling_notes"] = verdict.get("scaling_notes", "")
            rec["reasoning"] = verdict.get("reasoning", "")
            rec["source"] = "heuristic+llm"
    return rec


# --------------------------------------------------------------------------- #
# Deployment plan
# --------------------------------------------------------------------------- #
def build_plan(detected: dict, findings: list[dict]) -> dict:
    blockers = [f for f in findings if f["severity"] == "blocker"]
    steps: list[dict] = []

    def step(title, detail, *, automated=True, owner="platform"):
        steps.append({"n": len(steps) + 1, "title": title, "detail": detail, "automated": automated, "owner": owner})

    if blockers:
        for b in blockers:
            step(f"Resolve blocker: {b['title']}", b["fix"], automated=False, owner="team")

    step("Transfer the repository into the organisation", "Grants the self-hosted runner access to build this project.", owner="team")
    step("Register required environment variables", "Values are stored write-only in the platform and injected at container start: " + (", ".join(list((detected.get("env_vars") or {}).keys())[:10]) or "none detected"), owner="team")

    if not detected.get("dockerfiles"):
        step("Add the generated Dockerfile", "A starter matching the detected stack is attached to this report.", owner="team")

    step("Allocate a host port", "The port registry assigns a free port from this server's range — no manual checking.")
    services = [d for d in (detected.get("databases") or []) if d in SERVICE_RAM]
    if services:
        step("Provision backing services", "Start managed containers for: " + ", ".join(services) + ", and inject their connection strings.")
    step("Build the image on the self-hosted runner", "docker build with layer caching; the runner shares the deployment server's architecture, so no cross-platform surprises.")
    step("Start the container with a memory limit", "Limits are enforced so one project cannot take down the server for everyone.")
    step("Generate and validate the Nginx site", "Rendered from a template, checked with nginx -t, and only then reloaded. A failed check restores the previous config.")
    step("Verify the health endpoint", "Deployment is only marked successful once the app answers. Otherwise it rolls back to the previous image.")
    step("Enable continuous monitoring", "Logs, metrics and anomaly detection start automatically; failures raise a diagnosed incident.")

    return {
        "deployable_now": not blockers,
        "blocker_count": len(blockers),
        "steps": steps,
        "resources": recommend_resources(detected, use_llm=False),
        "estimated_minutes": 8 + 3 * len(services) + (15 if blockers else 0),
    }


# --------------------------------------------------------------------------- #
# Generated artifacts
# --------------------------------------------------------------------------- #
def generate_artifacts(root: Path, detected: dict, findings: list[dict]) -> dict:
    out: dict[str, str] = {}
    ids = {f["id"] for f in findings}
    lang = detected.get("primary_language")
    frameworks = detected.get("frameworks") or {}
    port = _guess_port(detected)

    if ids & {"no_container_definition", "no_dockerfile_for_build", "no_entrypoint", "bad_layer_order"}:
        out["Dockerfile"] = _dockerfile(lang, frameworks, port)
    if not detected.get("has_dockerignore"):
        out[".dockerignore"] = _dockerignore(lang)
    if "no_env_example" in ids:
        out[".env.example"] = _env_example(detected)
    if "no_ci" in ids:
        out[".github/workflows/deploy.yml"] = _workflow()
    if "no_healthcheck" in ids:
        out["health_endpoint_snippet"] = _health_snippet(frameworks, lang)
    return out


def _guess_port(detected: dict) -> int:
    ports = [int(p) for p in (detected.get("ports") or {}).keys() if str(p).isdigit()]
    common = [p for p in ports if 1000 <= p <= 9999]
    if common:
        return common[0]
    fw = detected.get("frameworks") or {}
    if "fastapi" in fw or "flask" in fw:
        return 8000
    if "django" in fw:
        return 8000
    if "express" in fw or "nestjs" in fw or "next" in fw:
        return 3000
    if "spring-boot" in fw:
        return 8080
    if "streamlit" in fw:
        return 8501
    return 8000


def _dockerfile(lang: str, frameworks: dict, port: int) -> str:
    if lang in ("javascript", "typescript"):
        if "next" in frameworks:
            return f"""# Generated by ViljaOps — review before committing
FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Frontend env vars are baked in at BUILD time, not runtime.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

FROM node:20-alpine AS run
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD wget -qO- http://127.0.0.1:{port}/api/health || exit 1
CMD ["node", "server.js"]
"""
        if frameworks.get("react") or frameworks.get("vue") or frameworks.get("svelte") or frameworks.get("angular"):
            return f"""# Generated by ViljaOps — review before committing
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# IMPORTANT: the API URL is compiled into the bundle here. If you leave this
# unset, the built site will call localhost — i.e. the visitor's own computer.
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine AS run
COPY --from=build /app/dist /usr/share/nginx/html
COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {{
    listen {port};
    root /usr/share/nginx/html;
    location /healthz {{ return 200 'ok'; add_header Content-Type text/plain; }}
    location / {{ try_files $uri $uri/ /index.html; }}
}}
EOF
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:{port}/healthz || exit 1
"""
        return f"""# Generated by ViljaOps — review before committing
FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
RUN addgroup -S app && adduser -S app -G app && chown -R app:app /app
USER app
ENV NODE_ENV=production PORT={port}
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \\
  CMD wget -qO- http://127.0.0.1:{port}/health || exit 1
CMD ["node", "index.js"]
"""

    if lang == "python":
        if "django" in frameworks:
            cmd = f'CMD ["gunicorn", "--bind", "0.0.0.0:{port}", "--workers", "3", "config.wsgi:application"]'
            extra = "RUN python manage.py collectstatic --noinput || true\n"
        elif "streamlit" in frameworks:
            cmd = f'CMD ["streamlit", "run", "app.py", "--server.port={port}", "--server.address=0.0.0.0"]'
            extra = ""
        elif "flask" in frameworks:
            cmd = f'CMD ["gunicorn", "--bind", "0.0.0.0:{port}", "--workers", "3", "app:app"]'
            extra = ""
        else:
            cmd = f'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "{port}"]'
            extra = ""
        return f"""# Generated by ViljaOps — review before committing
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
      build-essential curl \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies BEFORE copying source, so this layer stays cached
# when only your code changes. This is the difference between a 15-second
# rebuild and a 4-minute one.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
{extra}
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD curl -fsS http://127.0.0.1:{port}/health || exit 1

{cmd}
"""

    if lang == "java":
        return f"""# Generated by ViljaOps — review before committing
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY pom.xml .
RUN mvn -B dependency:go-offline
COPY src ./src
RUN mvn -B clean package -DskipTests

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /src/target/*.jar app.jar
USER app
EXPOSE {port}
ENV JAVA_OPTS="-XX:MaxRAMPercentage=75"
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD wget -qO- http://127.0.0.1:{port}/actuator/health || exit 1
ENTRYPOINT ["sh","-c","java $JAVA_OPTS -jar app.jar"]
"""

    if lang == "go":
        return f"""# Generated by ViljaOps — review before committing
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/app ./...

FROM alpine:3.20
RUN adduser -D -u 1000 app && apk add --no-cache curl
COPY --from=build /out/app /usr/local/bin/app
USER app
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:{port}/health || exit 1
CMD ["app"]
"""

    return f"""# Generated by ViljaOps — starter template, adapt to your stack
FROM debian:bookworm-slim
WORKDIR /app
COPY . .
EXPOSE {port}
CMD ["echo", "Replace this CMD with the command that starts your server in the foreground"]
"""


def _dockerignore(lang: str) -> str:
    common = """.git
.gitignore
.github
node_modules
dist
build
.next
coverage
*.log
.env
.env.*
!.env.example
.venv
venv
__pycache__
*.pyc
.pytest_cache
.mypy_cache
.DS_Store
.idea
.vscode
README.md
docs
"""
    return common


def _env_example(detected: dict) -> str:
    lines = ["# Generated by ViljaOps — copy to .env locally, never commit real values", ""]
    hints = {
        "DATABASE_URL": "postgresql://user:password@postgres:5432/appdb",
        "REDIS_URL": "redis://redis:6379/0",
        "PORT": str(_guess_port(detected)),
        "SECRET_KEY": "generate-with-openssl-rand-hex-32",
        "JWT_SECRET": "generate-with-openssl-rand-hex-32",
        "CORS_ORIGINS": "https://your-team.apps.vnrvjiet.in",
    }
    for key, files in (detected.get("env_vars") or {}).items():
        hint = hints.get(key, "")
        for needle, val in (("URL", "https://example.com"), ("KEY", "replace-me"), ("TOKEN", "replace-me"), ("PASSWORD", "replace-me"), ("HOST", "localhost"), ("PORT", "8000")):
            if not hint and needle in key:
                hint = val
        lines.append(f"# used in: {', '.join(files[:3])}")
        lines.append(f"{key}={hint}")
        lines.append("")
    return "\n".join(lines)


def _workflow() -> str:
    return """# Generated by ViljaOps. Commit as .github/workflows/deploy.yml
name: Deploy

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: true

jobs:
  deploy:
    runs-on: [self-hosted, linux, x64, viljaops]
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v4

      - name: Tell ViljaOps a deployment started
        id: start
        run: |
          RESP=$(curl -sS -X POST "$VILJAOPS_URL/api/deployments/start" \\
            -H "Authorization: Bearer $VILJAOPS_TOKEN" \\
            -H "Content-Type: application/json" \\
            -d "{\\"repo\\":\\"$GITHUB_REPOSITORY\\",\\"commit_sha\\":\\"$GITHUB_SHA\\",\\"branch\\":\\"${GITHUB_REF_NAME}\\",\\"gh_run_id\\":\\"$GITHUB_RUN_ID\\",\\"triggered_by\\":\\"$GITHUB_ACTOR\\"}")
          echo "deployment_id=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')" >> $GITHUB_OUTPUT
          echo "port=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["port"])')" >> $GITHUB_OUTPUT
        env:
          VILJAOPS_URL: ${{ vars.VILJAOPS_URL }}
          VILJAOPS_TOKEN: ${{ secrets.VILJAOPS_TOKEN }}

      - name: Build
        run: docker build -t ${{ github.event.repository.name }}:${{ github.sha }} .

      - name: Run
        run: |
          docker rm -f ${{ github.event.repository.name }} 2>/dev/null || true
          docker run -d --name ${{ github.event.repository.name }} \\
            --restart unless-stopped \\
            --memory 512m \\
            -p 127.0.0.1:${{ steps.start.outputs.port }}:8000 \\
            ${{ github.event.repository.name }}:${{ github.sha }}

      - name: Report outcome (always)
        if: always()
        run: |
          curl -sS -X POST "$VILJAOPS_URL/api/deployments/${{ steps.start.outputs.deployment_id }}/finish" \\
            -H "Authorization: Bearer $VILJAOPS_TOKEN" \\
            -H "Content-Type: application/json" \\
            -d "{\\"status\\":\\"${{ job.status }}\\"}"
        env:
          VILJAOPS_URL: ${{ vars.VILJAOPS_URL }}
          VILJAOPS_TOKEN: ${{ secrets.VILJAOPS_TOKEN }}
"""


def _health_snippet(frameworks: dict, lang: str) -> str:
    if "fastapi" in frameworks:
        return '''from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
def health():
    # Keep this cheap: no database calls, no external requests.
    # It is polled every 30 seconds.
    return {"status": "ok"}
'''
    if "flask" in frameworks:
        return '''@app.get("/health")
def health():
    return {"status": "ok"}, 200
'''
    if "django" in frameworks:
        return '''# urls.py
from django.http import JsonResponse
from django.urls import path

urlpatterns += [path("health", lambda r: JsonResponse({"status": "ok"}))]
'''
    if "express" in frameworks or "nestjs" in frameworks:
        return """app.get('/health', (req, res) => res.status(200).json({ status: 'ok' }));
"""
    return """Add an endpoint at GET /health that returns HTTP 200 with a small JSON body.
Keep it cheap — it is polled every 30 seconds and must not touch the database.
"""
