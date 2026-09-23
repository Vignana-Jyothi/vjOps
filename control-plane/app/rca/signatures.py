"""Deterministic failure-signature library.

This is the layer that must work when the GPU is offline, when the model is
having a bad day, and when a first-year student needs an answer in ten seconds.
Roughly 80% of real incidents in a student incubator are one of these thirty
patterns; the LLM exists for the other 20% and to write the explanation.

Every signature carries the evidence pattern, the cause, and a fix that maps to
an entry in the action whitelist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FixTemplate:
    action_type: str
    title: str
    rationale: str
    params: dict = field(default_factory=dict)
    risk: str | None = None
    requires_code_change: bool = False
    patch: str = ""


@dataclass(frozen=True)
class Signature:
    key: str
    title: str
    stage: str  # build | deploy | runtime | proxy | infra
    severity: str  # low | medium | high | critical
    patterns: tuple[str, ...]
    root_cause: str
    explanation: str
    student_explanation: str
    fixes: tuple[FixTemplate, ...] = ()
    sources: tuple[str, ...] = ()  # empty = any source
    confidence: float = 0.85
    excludes: tuple[str, ...] = ()


S = Signature
F = FixTemplate

SIGNATURES: tuple[Signature, ...] = (
    # ------------------------------------------------------------------ #
    # PORTS — the single most common failure on a shared server
    # ------------------------------------------------------------------ #
    S(
        key="port_already_allocated",
        title="Host port already in use",
        stage="deploy",
        severity="high",
        patterns=(
            r"bind for .*?:(?P<port>\d+) failed: port is already allocated",
            r"driver failed programming external connectivity.*?:(?P<port>\d+)",
            r"Error starting userland proxy.*?:(?P<port>\d+): bind: address already in use",
            r"listen tcp .*?:(?P<port>\d+): bind: address already in use",
        ),
        root_cause="Host port {port} is already bound by another container or process on this server.",
        explanation=(
            "Docker could not publish the container's port because {port} on the host is "
            "occupied. On a shared incubator server this normally means another team's "
            "deployment, or a previous container of this same project that was never removed, "
            "already owns that port."
        ),
        student_explanation=(
            "Your app tried to use door number {port} on the server, but another app is already "
            "standing in that doorway. We'll move your app to a free door — nothing is wrong "
            "with your code."
        ),
        fixes=(
            F(
                "reassign_port",
                "Allocate a free port and re-point Nginx",
                "The port registry picks a port nothing else owns, recreates the container "
                "on it and regenerates the Nginx upstream, so the public URL is unchanged.",
            ),
            F(
                "code_change",
                "Stop hard-coding the host port",
                "Publishing a fixed host port guarantees a collision eventually. Read the "
                "port from an environment variable so the platform can assign it.",
                params={"file": "docker-compose.yml / Dockerfile"},
                requires_code_change=True,
                patch='ports:\n  - "${HOST_PORT}:3000"   # was "3000:3000"',
            ),
        ),
        sources=("docker", "actions"),
        confidence=0.97,
    ),
    S(
        key="app_port_in_use_inside_container",
        title="Application port already in use inside the container",
        stage="runtime",
        severity="high",
        patterns=(
            r"EADDRINUSE.*?:(?P<port>\d+)",
            r"OSError:\s*\[Errno 98\] Address already in use",
            r"Address already in use.*?:(?P<port>\d+)",
        ),
        root_cause="The application tried to bind port {port} inside the container but it was already taken.",
        explanation=(
            "Two processes inside the same container are binding the same port — commonly a "
            "dev server started by the entrypoint alongside the production server, or a "
            "reloader that forked twice."
        ),
        student_explanation=(
            "Your app started twice inside the same container and both copies tried to use "
            "port {port}. Usually the start command is launching the server twice."
        ),
        fixes=(
            F(
                "code_change",
                "Start exactly one server process in the entrypoint",
                "Check CMD/ENTRYPOINT — a reloader (--reload, nodemon) plus a production "
                "server will both try to bind.",
                params={"file": "Dockerfile"},
                requires_code_change=True,
            ),
            F("restart_container", "Restart the container", "Clears a duplicated process from a previous crash."),
        ),
    ),
    # ------------------------------------------------------------------ #
    # DEPENDENCIES
    # ------------------------------------------------------------------ #
    S(
        key="python_module_not_found",
        title="Python dependency missing from the image",
        stage="runtime",
        severity="high",
        patterns=(
            r"ModuleNotFoundError: No module named '(?P<package>[\w\.\-]+)'",
            r"ImportError: cannot import name .*? from '(?P<package>[\w\.\-]+)'",
        ),
        root_cause="Python package '{package}' is imported by the code but is not installed in the image.",
        explanation=(
            "The container exits immediately at import time. The package works on the "
            "student's laptop because it was pip-installed there by hand, but it was never "
            "added to requirements.txt, so the Docker build never installed it."
        ),
        student_explanation=(
            "Your code uses '{package}', but the server doesn't have it. It works on your "
            "laptop because you installed it there. Add it to requirements.txt so the server "
            "installs it too."
        ),
        fixes=(
            F(
                "add_dependency",
                "Add {package} to requirements.txt",
                "Pin the version you develop with so builds are reproducible: "
                "run `pip freeze | grep -i {package}` locally and paste that line.",
                params={"package": "{package}", "manifest": "requirements.txt"},
                requires_code_change=True,
            ),
            F("rebuild_image", "Rebuild the image after the dependency is added", "Layer cache must be invalidated."),
        ),
        confidence=0.95,
    ),
    S(
        key="node_module_not_found",
        title="Node dependency missing from the image",
        stage="runtime",
        severity="high",
        patterns=(
            r"Error: Cannot find module '(?P<package>[@\w\/\.\-]+)'",
            r"Module not found: Error: Can't resolve '(?P<package>[@\w\/\.\-]+)'",
        ),
        root_cause="Node module '{package}' is required at runtime but is not present in the image.",
        explanation=(
            "Either the package is missing from package.json, or it is in devDependencies "
            "while the image installs with --omit=dev, or node_modules was copied in from "
            "the host and does not match the container's platform."
        ),
        student_explanation=(
            "The server can't find the package '{package}'. Either it isn't listed in "
            "package.json, or it's listed under devDependencies but your app needs it to run."
        ),
        fixes=(
            F(
                "add_dependency",
                "Add {package} to dependencies",
                "npm install {package} --save (not --save-dev) and commit package-lock.json.",
                params={"package": "{package}", "manifest": "package.json"},
                requires_code_change=True,
            ),
            F(
                "code_change",
                "Never COPY node_modules into the image",
                "Add node_modules to .dockerignore and let `npm ci` install inside the build.",
                params={"file": ".dockerignore"},
                requires_code_change=True,
                patch="node_modules\nnpm-debug.log\n.env",
            ),
        ),
        confidence=0.95,
    ),
    S(
        key="pip_resolution_failed",
        title="pip could not resolve dependencies",
        stage="build",
        severity="high",
        patterns=(
            r"ERROR: Could not find a version that satisfies the requirement (?P<package>[\w\.\-\[\]]+)",
            r"ERROR: ResolutionImpossible",
            r"No matching distribution found for (?P<package>[\w\.\-\[\]]+)",
        ),
        root_cause="pip cannot install '{package}' — no matching distribution for the image's Python version.",
        explanation=(
            "Usually a package pinned to a version that has no wheel for the base image's "
            "Python (e.g. a 3.10-only package on python:3.12-slim), or a typo'd/private name."
        ),
        student_explanation=(
            "The server couldn't download '{package}'. Check the spelling, and whether that "
            "version works with the Python version in your Dockerfile."
        ),
        fixes=(
            F(
                "code_change",
                "Match the base image Python version to your local one",
                "Run `python --version` locally and use the same tag in FROM.",
                params={"file": "Dockerfile"},
                requires_code_change=True,
            ),
        ),
        sources=("actions", "docker"),
    ),
    # ------------------------------------------------------------------ #
    # ENVIRONMENT / CONFIG
    # ------------------------------------------------------------------ #
    S(
        key="missing_env_var",
        title="Required environment variable is not set",
        stage="runtime",
        severity="high",
        patterns=(
            r"KeyError: '(?P<key>[A-Z][A-Z0-9_]{2,})'",
            r"environment variable (?P<key>[A-Z][A-Z0-9_]{2,}) (?:is )?(?:not set|missing|required)",
            r"(?P<key>[A-Z][A-Z0-9_]{2,}) is not defined",
            r"ValidationError.*?field required.*?(?P<key>[A-Z][A-Z0-9_]{2,})",
            r"process\.env\.(?P<key>[A-Z][A-Z0-9_]{2,}) is undefined",
        ),
        root_cause="Environment variable {key} is required by the application but was not provided to the container.",
        explanation=(
            "The app reads {key} at startup and there is no value for it in the container "
            "environment. Typically the value lives in a local .env file that is (correctly) "
            "git-ignored, and was never registered with the platform."
        ),
        student_explanation=(
            "Your app needs a setting called {key}. It's in your .env file on your laptop, but "
            "that file isn't uploaded to GitHub (which is good — it may hold passwords). "
            "Register the value with the deployment platform instead."
        ),
        fixes=(
            F(
                "set_env_var",
                "Register {key} for this deployment",
                "Stored write-only and injected at container start. Never written to git.",
                params={"key": "{key}", "value": ""},
            ),
            F(
                "code_change",
                "Document every variable in .env.example",
                "Commit a .env.example with the key names and dummy values so reviewers know "
                "what the app needs. Never commit real values.",
                params={"file": ".env.example"},
                requires_code_change=True,
            ),
        ),
        confidence=0.88,
    ),
    S(
        key="env_file_not_found",
        title="Application expects a .env file that does not exist in the image",
        stage="runtime",
        severity="high",
        patterns=(
            r"(?:FileNotFoundError|ENOENT).*?\.env",
            r"could not (?:find|read|open).*?\.env",
        ),
        root_cause="The application tries to load a .env file that is not present in the container.",
        explanation=(
            ".env is git-ignored, so it never reaches the image. In containers, configuration "
            "should come from real environment variables, not from a file on disk."
        ),
        student_explanation=(
            "Your app looks for a .env file, but that file isn't in the container (it's not on "
            "GitHub). Read settings from environment variables instead."
        ),
        fixes=(
            F(
                "code_change",
                "Read config from the environment, not from a file",
                "load_dotenv() should be a local-development convenience only; in production "
                "os.environ / process.env is already populated by the platform.",
                params={"file": "config module"},
                requires_code_change=True,
            ),
        ),
    ),
    # ------------------------------------------------------------------ #
    # DATABASE / NETWORKING BETWEEN CONTAINERS
    # ------------------------------------------------------------------ #
    S(
        key="db_connection_refused_localhost",
        title="App is connecting to a database at localhost from inside a container",
        stage="runtime",
        severity="critical",
        patterns=(
            r"could not connect to server.*?(?:127\.0\.0\.1|localhost)",
            r"connection to server at \"(?:localhost|127\.0\.0\.1)\".*?failed",
            r"ECONNREFUSED\s+(?:127\.0\.0\.1|localhost):(?P<port>\d+)",
            r"OperationalError.*?(?:could not translate host name|Connection refused).*?(?:localhost|127\.0\.0\.1)",
        ),
        root_cause="The application uses 'localhost' for its database host, which inside a container points at the container itself.",
        explanation=(
            "Each container has its own network namespace, so localhost is the container, not "
            "the host machine or the database container. The database host must be the Docker "
            "service/DNS name (e.g. 'postgres') or the host gateway address."
        ),
        student_explanation=(
            "Inside a container, 'localhost' means the container itself — not your computer. "
            "Your database is a different container, so use its service name (like 'postgres') "
            "as the host instead of 'localhost'."
        ),
        fixes=(
            F(
                "code_change",
                "Use the database service name as the host",
                "Set DATABASE_URL to postgresql://user:pass@postgres:5432/db and keep the host "
                "configurable so it still works locally.",
                params={"file": "environment / settings"},
                requires_code_change=True,
                patch="DATABASE_URL=postgresql://user:pass@postgres:5432/appdb  # not @localhost",
            ),
            F(
                "set_env_var",
                "Override DATABASE_URL for this deployment",
                "Point the app at the managed database container on this server.",
                params={"key": "DATABASE_URL", "value": ""},
            ),
        ),
        confidence=0.93,
    ),
    S(
        key="db_auth_failed",
        title="Database authentication failed",
        stage="runtime",
        severity="high",
        patterns=(
            r"password authentication failed for user \"(?P<user>[\w\-]+)\"",
            r"Access denied for user '(?P<user>[\w\-]+)'@",
            r"MongoServerError: Authentication failed",
        ),
        root_cause="The database rejected the credentials supplied for user '{user}'.",
        explanation=(
            "Either the deployment is using development credentials, the database user was "
            "never created on this server, or the password contains characters that need "
            "URL-encoding inside the connection string."
        ),
        student_explanation=(
            "The database refused your app's username or password. Check the credentials "
            "registered for this deployment — and remember special characters in a password "
            "must be URL-encoded inside a connection string."
        ),
        fixes=(
            F(
                "set_env_var",
                "Correct the database credentials for this deployment",
                "Update the connection string; the value is stored write-only.",
                params={"key": "DATABASE_URL", "value": ""},
            ),
        ),
    ),
    S(
        key="db_relation_missing",
        title="Database schema has not been migrated",
        stage="runtime",
        severity="high",
        patterns=(
            r"relation \"(?P<table>[\w\.]+)\" does not exist",
            r"Table '(?P<table>[\w\.]+)' doesn't exist",
            r"no such table: (?P<table>[\w\.]+)",
            r"column \"(?P<column>[\w]+)\" of relation .*? does not exist",
        ),
        root_cause="Table '{table}' is missing — migrations have not been run against this database.",
        explanation=(
            "The application code expects a schema version that the database has not reached. "
            "Migrations run on the developer's laptop are not automatically applied to the "
            "server's database."
        ),
        student_explanation=(
            "Your app expects a table called '{table}' that doesn't exist yet on the server's "
            "database. The migration that creates it needs to be run here too."
        ),
        fixes=(
            F(
                "run_migration",
                "Run the migration inside the container",
                "Schema changes can lose data, so a named human has to approve this one.",
                params={"command": "alembic upgrade head"},
                risk="dangerous",
            ),
            F(
                "code_change",
                "Run migrations from the entrypoint",
                "Make the container self-migrating so every deploy converges on the right schema.",
                params={"file": "entrypoint.sh"},
                requires_code_change=True,
                patch="#!/bin/sh\nset -e\nalembic upgrade head\nexec \"$@\"",
            ),
        ),
        confidence=0.92,
    ),
    S(
        key="db_too_many_connections",
        title="Database connection pool exhausted",
        stage="runtime",
        severity="high",
        patterns=(
            r"(?:FATAL|ERROR).*?too many (?:clients|connections)",
            r"remaining connection slots are reserved",
            r"QueuePool limit of size \d+ overflow \d+ reached",
        ),
        root_cause="The application opened more database connections than the server allows.",
        explanation=(
            "Usually a connection created per request and never closed, or several student "
            "projects sharing one Postgres instance with default pool sizes. The shared "
            "database becomes unavailable for everyone, so this is an ecosystem-level problem."
        ),
        student_explanation=(
            "Your app keeps opening new database connections without closing them, and the "
            "database ran out. Use one shared connection pool instead of connecting per request."
        ),
        fixes=(
            F(
                "code_change",
                "Use a bounded connection pool",
                "Create the engine/pool once at startup with pool_size and max_overflow set; "
                "never per request.",
                params={"file": "database module"},
                requires_code_change=True,
            ),
            F("restart_container", "Restart to release leaked connections", "Immediate mitigation while the fix is written."),
        ),
    ),
    # ------------------------------------------------------------------ #
    # MEMORY / RESOURCES
    # ------------------------------------------------------------------ #
    S(
        key="oom_killed",
        title="Container killed by the kernel out-of-memory killer",
        stage="runtime",
        severity="critical",
        patterns=(
            r"Killed process \d+ .*?(?:total-vm|anon-rss)",
            r"Out of memory: Killed process",
            r"OOMKilled",
            r"exited with code 137",
            r"JavaScript heap out of memory",
            r"MemoryError",
        ),
        root_cause="The container exceeded its memory limit and was killed (exit 137).",
        explanation=(
            "Exit code 137 is SIGKILL from the OOM killer. On a shared server this often takes "
            "neighbouring projects down with it, so the memory limit matters. Common causes: "
            "loading a whole dataset or model into memory at startup, or an unbounded cache."
        ),
        student_explanation=(
            "Your app used more memory than it was allowed and the server stopped it. This "
            "usually happens when you load a big file or model fully into memory."
        ),
        fixes=(
            F(
                "set_memory_limit",
                "Raise the memory limit for this container",
                "Short-term mitigation while the memory use is investigated.",
                params={"memory_mb": 2048},
            ),
            F(
                "code_change",
                "Stream or paginate instead of loading everything into memory",
                "Load models once at startup, not per request; read large files in chunks; "
                "bound any in-process cache.",
                params={"file": "data loading code"},
                requires_code_change=True,
            ),
        ),
        confidence=0.94,
    ),
    S(
        key="disk_full",
        title="No disk space left on the server",
        stage="infra",
        severity="critical",
        patterns=(
            r"No space left on device",
            r"write error: No space left",
            r"failed to register layer.*?no space left",
            r"disk quota exceeded",
        ),
        root_cause="The server's filesystem is full, so builds and writes are failing.",
        explanation=(
            "Almost always accumulated Docker build cache, dangling images from repeated "
            "rebuilds, and unrotated container logs. This affects every project on the box, "
            "not just this one."
        ),
        student_explanation=(
            "The server ran out of storage space. This is an infrastructure problem, not "
            "something wrong with your code."
        ),
        fixes=(
            F("prune_docker", "Prune dangling images and build cache", "Reclaims the largest share of space with no risk to running containers."),
            F("prune_logs", "Truncate oversized container logs", "Applies a rotation cap so this does not recur."),
        ),
        confidence=0.96,
    ),
    S(
        key="cannot_allocate_memory",
        title="Server cannot allocate memory for new processes",
        stage="infra",
        severity="critical",
        patterns=(
            r"fork/exec .*?: cannot allocate memory",
            r"Cannot allocate memory",
            r"fork: retry: Resource temporarily unavailable",
        ),
        root_cause="The server has exhausted available RAM across all running containers.",
        explanation=(
            "This is a host-level condition. One project's memory growth is starving the whole "
            "server. Identify the container with runaway RSS before restarting anything."
        ),
        student_explanation="The server itself is out of memory. A DevOps engineer needs to look at this.",
        fixes=(F("escalate_to_devops", "Escalate — host-level memory exhaustion", "Requires a human to decide which workload to constrain.", params={"reason": "host RAM exhausted"}),),
    ),
    # ------------------------------------------------------------------ #
    # DOCKER BUILD
    # ------------------------------------------------------------------ #
    S(
        key="dockerfile_copy_missing",
        title="Dockerfile COPY references a path that is not in the build context",
        stage="build",
        severity="high",
        patterns=(
            r'COPY failed: (?:file not found in build context|stat) .*?"?(?P<path>[^"\s]+)"?',
            r'failed to compute cache key: .*?"(?P<path>[^"]+)": not found',
            r"lstat .*?(?P<path>[\w\.\-/]+): no such file or directory",
        ),
        root_cause="The Dockerfile copies '{path}', which does not exist in the build context.",
        explanation=(
            "Either the file is git-ignored (so CI never checked it out), the path is relative "
            "to the wrong directory, or .dockerignore excludes it. The build context is the "
            "directory passed to `docker build`, not the Dockerfile's directory."
        ),
        student_explanation=(
            "Your Dockerfile tries to copy '{path}', but that file isn't there when the server "
            "builds. Check that it's committed to GitHub and not listed in .gitignore or "
            ".dockerignore."
        ),
        fixes=(
            F(
                "code_change",
                "Fix the COPY path or commit the missing file",
                "Confirm the file appears in `git ls-files` and is not matched by .dockerignore.",
                params={"file": "Dockerfile"},
                requires_code_change=True,
            ),
        ),
        confidence=0.93,
    ),
    S(
        key="docker_base_image_pull_failed",
        title="Base image could not be pulled",
        stage="build",
        severity="high",
        patterns=(
            r"(?:manifest for|pull access denied for) (?P<image>[\w\.\-/:]+).*?(?:not found|denied)",
            r"failed to resolve source metadata for (?P<image>[\w\.\-/:]+)",
            r"toomanyrequests: You have reached your pull rate limit",
        ),
        root_cause="Docker could not pull the base image '{image}'.",
        explanation=(
            "Either the tag does not exist, the image is private, or the server hit Docker "
            "Hub's anonymous pull rate limit — which is common when many student builds run "
            "from the same public IP."
        ),
        student_explanation=(
            "The server couldn't download the base image '{image}'. Check the image name and "
            "tag are spelled correctly."
        ),
        fixes=(
            F("code_change", "Pin a base image tag that exists", "Verify with `docker manifest inspect <image>`.", params={"file": "Dockerfile"}, requires_code_change=True),
            F("escalate_to_devops", "Configure a registry mirror or authenticated pull", "Rate limiting is a shared-infrastructure fix.", params={"reason": "docker hub rate limit"}),
        ),
    ),
    S(
        key="docker_exec_format_error",
        title="Architecture mismatch between build and run host",
        stage="runtime",
        severity="high",
        patterns=(
            r"exec (?:user process caused: )?exec format error",
            r"exec .*?: exec format error",
            r"The requested image's platform \(linux/(?P<built>\w+)\) does not match",
        ),
        root_cause="The image was built for a different CPU architecture than the server runs.",
        explanation=(
            "Classic Apple Silicon symptom: an image built on an arm64 laptop will not run on "
            "an amd64 server. The build must target the server's platform."
        ),
        student_explanation=(
            "You built the image on a Mac with an M-series chip, and the server uses a "
            "different chip type. Build for the server's architecture instead."
        ),
        fixes=(
            F(
                "code_change",
                "Build for linux/amd64 in CI",
                "Add platforms: linux/amd64 to the build step, or build on the self-hosted "
                "runner which already matches the server.",
                params={"file": ".github/workflows/deploy.yml"},
                requires_code_change=True,
                patch="- uses: docker/build-push-action@v5\n  with:\n    platforms: linux/amd64",
            ),
        ),
        confidence=0.95,
    ),
    S(
        key="permission_denied_entrypoint",
        title="Entrypoint script is not executable",
        stage="runtime",
        severity="high",
        patterns=(
            r"exec .*?(?P<path>[\w\.\-/]+): permission denied",
            r'starting container process caused.*?"(?P<path>[^"]+)": permission denied',
        ),
        root_cause="The entrypoint '{path}' does not have the executable bit set inside the image.",
        explanation=(
            "Git preserves the executable bit only if it was set when the file was committed. "
            "Files created on Windows commonly arrive without it."
        ),
        student_explanation=(
            "The server isn't allowed to run your startup script. It needs to be marked as "
            "executable."
        ),
        fixes=(
            F(
                "code_change",
                "chmod +x the entrypoint in the Dockerfile",
                "Do it in the image so it does not depend on how the file was committed.",
                params={"file": "Dockerfile"},
                requires_code_change=True,
                patch="COPY entrypoint.sh /entrypoint.sh\nRUN chmod +x /entrypoint.sh",
            ),
        ),
    ),
    S(
        key="crlf_line_endings",
        title="Shell script has Windows line endings",
        stage="runtime",
        severity="medium",
        patterns=(
            r"(?:/bin/sh|/bin/bash)\^M: bad interpreter",
            r"\$'\\r': command not found",
            r"standard_init_linux\.go.*?exec format error",
        ),
        root_cause="A shell script in the image has CRLF line endings, so Linux cannot execute it.",
        explanation="The shebang line ends with a carriage return, which Linux treats as part of the interpreter path.",
        student_explanation=(
            "Your script was saved with Windows-style line endings. Linux can't read the first "
            "line properly. Convert it to Unix line endings."
        ),
        fixes=(
            F(
                "code_change",
                "Force LF line endings for shell scripts",
                "Add a .gitattributes rule so this cannot come back.",
                params={"file": ".gitattributes"},
                requires_code_change=True,
                patch="*.sh text eol=lf\nentrypoint.sh text eol=lf",
            ),
        ),
    ),
    # ------------------------------------------------------------------ #
    # NGINX / PROXY
    # ------------------------------------------------------------------ #
    S(
        key="nginx_502_upstream_refused",
        title="Nginx 502 — upstream connection refused",
        stage="proxy",
        severity="critical",
        patterns=(
            r"connect\(\) failed \(111: Connection refused\) while connecting to upstream",
            r"upstream: \"http://127\.0\.0\.1:(?P<port>\d+)",
            r"no live upstreams while connecting to upstream",
        ),
        root_cause="Nginx is proxying to port {port}, but nothing is listening there.",
        explanation=(
            "The reverse proxy is healthy; the application behind it is not. Either the "
            "container crashed, it is still starting, or it binds a different port than the "
            "one Nginx was configured with."
        ),
        student_explanation=(
            "The web server can reach the internet fine, but your app isn't answering. Your "
            "app has either crashed or is listening on a different port than expected."
        ),
        fixes=(
            F("restart_container", "Restart the application container", "Recovers a crashed process."),
            F("reassign_port", "Re-sync the Nginx upstream with the container's real port", "Regenerates the vhost from the port registry so the two agree."),
        ),
        sources=("nginx",),
        confidence=0.9,
    ),
    S(
        key="nginx_bind_conflict",
        title="Nginx cannot bind its listen address",
        stage="proxy",
        severity="critical",
        patterns=(
            r"nginx: \[emerg\] bind\(\) to 0\.0\.0\.0:(?P<port>\d+) failed \(98: Address already in use\)",
            r"nginx: \[emerg\] still could not bind",
        ),
        root_cause="Nginx cannot bind port {port} because another process already holds it.",
        explanation="Usually a second Nginx instance, or a container publishing 80/443 directly instead of going through the proxy.",
        student_explanation="The main web server couldn't start because something else is using its port.",
        fixes=(F("escalate_to_devops", "Escalate — proxy port conflict affects every site", "Host-level conflict.", params={"reason": "nginx cannot bind"}),),
        sources=("nginx", "system"),
    ),
    S(
        key="nginx_duplicate_server_name",
        title="Duplicate Nginx server_name",
        stage="proxy",
        severity="high",
        patterns=(
            r"nginx: \[warn\] conflicting server name \"(?P<domain>[\w\.\-]+)\"",
            r"duplicate upstream \"(?P<upstream>[\w\.\-]+)\"",
        ),
        root_cause="Two Nginx site configs declare the same server_name '{domain}'.",
        explanation=(
            "Two deployments claimed the same subdomain. Nginx silently serves the first one "
            "it loads, so one team's traffic reaches the other team's app."
        ),
        student_explanation="Two projects are registered on the same web address, so requests go to the wrong app.",
        fixes=(F("apply_nginx_config", "Regenerate site configs from the registry", "The domain registry is the single source of truth; stale hand-written files get removed.", params={}),),
        sources=("nginx",),
    ),
    S(
        key="nginx_413_body_too_large",
        title="Nginx rejects large uploads (413)",
        stage="proxy",
        severity="medium",
        patterns=(r"client intended to send too large body", r"413 Request Entity Too Large"),
        root_cause="Uploads exceed Nginx's client_max_body_size (1 MB by default).",
        explanation="The application never sees the request; Nginx rejects it first.",
        student_explanation="File uploads are being blocked because they're bigger than the web server allows by default.",
        fixes=(F("apply_nginx_config", "Raise client_max_body_size for this site", "Regenerates the vhost with a larger upload limit.", params={"client_max_body_size": "25m"}),),
        sources=("nginx",),
    ),
    S(
        key="nginx_upstream_timeout",
        title="Nginx 504 — upstream timed out",
        stage="proxy",
        severity="high",
        patterns=(r"upstream timed out \(110: Connection timed out\)", r"504 Gateway Time-?out"),
        root_cause="The application did not respond within Nginx's proxy_read_timeout.",
        explanation=(
            "Either a genuinely slow request (unindexed query, synchronous external API call) "
            "or a deadlocked worker. Raising the timeout hides the symptom; find the slow path."
        ),
        student_explanation="Your app took too long to answer a request, so the web server gave up waiting.",
        fixes=(
            F("code_change", "Move slow work off the request path", "Long jobs belong in a background worker; the request should return immediately.", params={"file": "request handlers"}, requires_code_change=True),
            F("apply_nginx_config", "Temporarily raise proxy_read_timeout", "Mitigation only — does not fix the slow endpoint.", params={"proxy_read_timeout": "120s"}),
        ),
        sources=("nginx",),
    ),
    S(
        key="ssl_cert_expired",
        title="TLS certificate expired or missing",
        stage="proxy",
        severity="critical",
        patterns=(
            r"certificate has expired",
            r"SSL_CTX_use_PrivateKey_file.*?failed",
            r"cannot load certificate \"(?P<path>[^\"]+)\"",
            r"NET::ERR_CERT_DATE_INVALID",
        ),
        root_cause="The TLS certificate for this site is expired or unreadable.",
        explanation="Certbot renewal has stopped running, or the renewal hook never reloads Nginx so the old cert stays in memory.",
        student_explanation="The security certificate for your website has expired, so browsers show a warning.",
        fixes=(
            F("escalate_to_devops", "Renew the certificate and fix the renewal hook", "Certificate management is platform-level.", params={"reason": "expired TLS certificate"}),
            F("reload_nginx", "Reload Nginx to pick up a renewed certificate", "Nginx caches certificates in memory until reload."),
        ),
        sources=("nginx", "system"),
    ),
    # ------------------------------------------------------------------ #
    # GITHUB ACTIONS / CI
    # ------------------------------------------------------------------ #
    S(
        key="actions_secret_missing",
        title="GitHub Actions secret is empty",
        stage="build",
        severity="high",
        patterns=(
            r"Error: Input required and not supplied: (?P<key>[\w\-]+)",
            r"secrets\.(?P<key>[A-Z0-9_]+).*?(?:is empty|not set)",
            r"Error: Username and password required",
        ),
        root_cause="The workflow references a secret '{key}' that is not configured on the repository.",
        explanation=(
            "Secrets do not transfer when a repository is moved into the organisation, and they "
            "are never available to workflows triggered from forks."
        ),
        student_explanation=(
            "The build needs a secret value called '{key}', but it isn't set on this repository. "
            "Secrets don't come along when a repo is transferred."
        ),
        fixes=(F("escalate_to_devops", "Add the missing repository/organisation secret", "Secrets are set by a maintainer, not by the pipeline.", params={"reason": "missing Actions secret {key}"}),),
        sources=("actions",),
    ),
    S(
        key="actions_no_runner",
        title="No self-hosted runner matched the job labels",
        stage="build",
        severity="high",
        patterns=(
            r"No runner matching the specified labels was found",
            r"Waiting for a runner to pick up this job",
            r"This request was automatically failed because there were no enabled runners",
        ),
        root_cause="The workflow requested runner labels that no online self-hosted runner provides.",
        explanation=(
            "Either the runner service is down on the deployment server, or `runs-on` was left "
            "as ubuntu-latest / has a typo'd label."
        ),
        student_explanation=(
            "The build never started because no build machine was available for the labels your "
            "workflow asked for."
        ),
        fixes=(
            F("code_change", "Use the incubator's runner labels", "Set runs-on to the labels the self-hosted runner registers with.", params={"file": ".github/workflows/deploy.yml"}, requires_code_change=True, patch="runs-on: [self-hosted, linux, x64, viljaops]"),
            F("escalate_to_devops", "Check the runner service on the deployment server", "The runner may be stopped or offline.", params={"reason": "no online self-hosted runner"}),
        ),
        sources=("actions",),
        confidence=0.93,
    ),
    S(
        key="actions_permission_denied_docker",
        title="Runner user cannot talk to the Docker daemon",
        stage="build",
        severity="high",
        patterns=(
            r"permission denied while trying to connect to the Docker daemon socket",
            r"Got permission denied while trying to connect to the Docker daemon",
            r"dial unix /var/run/docker\.sock.*?permission denied",
        ),
        root_cause="The self-hosted runner's user is not a member of the docker group.",
        explanation="The runner can check out code but cannot build images until its user can access /var/run/docker.sock.",
        student_explanation="The build machine isn't allowed to use Docker. This is a server setup issue, not your code.",
        fixes=(F("escalate_to_devops", "Add the runner user to the docker group and restart the runner", "usermod -aG docker <runner-user>; the service must restart to pick up the new group.", params={"reason": "runner not in docker group"}),),
        sources=("actions",),
        confidence=0.96,
    ),
    S(
        key="test_failure",
        title="Test suite failed",
        stage="build",
        severity="medium",
        patterns=(
            r"(?P<failed>\d+) failed,?\s+\d+ passed",
            r"Tests:\s+(?P<failed>\d+) failed",
            r"FAIL\s+.*?\.(?:test|spec)\.[jt]sx?",
            r"AssertionError",
        ),
        root_cause="The pipeline stopped because {failed} test(s) failed.",
        explanation="The deployment gate is working as intended — the failing assertion is the thing to fix.",
        student_explanation="Some of your tests didn't pass, so the deployment stopped on purpose to protect the live version.",
        fixes=(F("code_change", "Fix the failing assertion", "Reproduce locally with the same command the pipeline runs.", params={"file": "test suite"}, requires_code_change=True),),
        sources=("actions",),
        confidence=0.75,
    ),
    S(
        key="build_timeout",
        title="Build exceeded its time limit",
        stage="build",
        severity="medium",
        patterns=(r"The job running on runner .*? has exceeded the maximum execution time", r"Error: The operation was canceled\.", r"context deadline exceeded"),
        root_cause="The build ran past the configured timeout and was cancelled.",
        explanation="Usually an uncached dependency install, a native package compiling from source, or a hung network call.",
        student_explanation="Your build took too long and was stopped. Caching dependencies usually fixes this.",
        fixes=(F("code_change", "Cache dependencies and order Dockerfile layers by change frequency", "Copy the manifest and install before copying source, so the install layer is cached.", params={"file": "Dockerfile"}, requires_code_change=True, patch="COPY requirements.txt .\nRUN pip install -r requirements.txt\nCOPY . ."),),
        sources=("actions",),
    ),
    # ------------------------------------------------------------------ #
    # APPLICATION RUNTIME
    # ------------------------------------------------------------------ #
    S(
        key="container_exit_immediately",
        title="Container exits immediately after start",
        stage="runtime",
        severity="high",
        patterns=(r"exited with code (?P<code>[1-9]\d*)\s*$", r"Container .*? (?:Exited|died) \((?P<code>\d+)\)"),
        root_cause="The container's main process exited with code {code} right after start.",
        explanation=(
            "A container lives exactly as long as PID 1. An exit this fast is a startup error "
            "(bad config, missing dependency) or a foreground command that returns immediately."
        ),
        student_explanation=(
            "Your container starts and stops straight away. The main command finished instead "
            "of staying running — check the very first lines of the container's logs."
        ),
        fixes=(
            F("code_change", "Keep the main process in the foreground", "Servers must not daemonise inside a container; PID 1 has to stay alive.", params={"file": "Dockerfile CMD"}, requires_code_change=True),
            F("restart_container", "Restart and capture the startup logs", "Confirms whether the failure is deterministic."),
        ),
        confidence=0.7,
    ),
    S(
        key="restart_loop",
        title="Container is in a restart loop",
        stage="runtime",
        severity="critical",
        patterns=(r"Restarting \((?P<code>\d+)\) \d+ seconds ago", r"back-off .*? restarting failed container"),
        root_cause="The container keeps crashing and Docker keeps restarting it (exit code {code}).",
        explanation="A restart policy is masking a deterministic startup failure. Each restart also burns CPU on a shared server.",
        student_explanation="Your app crashes as soon as it starts, and the server keeps trying again. The real error is in the first few log lines of each attempt.",
        fixes=(F("escalate_to_devops", "Stop the loop and diagnose the startup failure", "Restarting again will not help until the startup error is fixed.", params={"reason": "crash loop"}),),
        confidence=0.9,
    ),
    S(
        key="unhandled_promise_rejection",
        title="Unhandled promise rejection crashed the Node process",
        stage="runtime",
        severity="high",
        patterns=(r"UnhandledPromiseRejection", r"\[UNHANDLED REJECTION\]", r"ERR_UNHANDLED_REJECTION"),
        root_cause="An async error was never caught, and modern Node terminates the process for it.",
        explanation="A rejected promise with no catch handler is fatal from Node 15 onward.",
        student_explanation="An error happened inside an async function and nothing handled it, so Node shut the whole app down.",
        fixes=(F("code_change", "Wrap async handlers in try/catch", "Add error middleware and catch rejections at the boundary.", params={"file": "route handlers"}, requires_code_change=True),),
    ),
    S(
        key="cors_blocked",
        title="Frontend requests blocked by CORS",
        stage="runtime",
        severity="medium",
        patterns=(r"has been blocked by CORS policy", r"No 'Access-Control-Allow-Origin' header is present"),
        root_cause="The backend does not allow the deployed frontend's origin.",
        explanation="CORS is usually configured for http://localhost:3000 during development and never updated to the deployed domain.",
        student_explanation="Your backend only trusts requests from your laptop's address. It needs to also trust your live website's address.",
        fixes=(
            F("code_change", "Allow the deployed origin from configuration", "Read allowed origins from an environment variable so dev and production differ without a code change.", params={"file": "CORS config"}, requires_code_change=True, patch='CORS_ORIGINS=https://team.apps.vnrvjiet.in'),
            F("set_env_var", "Set CORS_ORIGINS for this deployment", "Injects the deployed domain without a rebuild.", params={"key": "CORS_ORIGINS", "value": ""}),
        ),
    ),
    S(
        key="frontend_calls_localhost",
        title="Frontend is calling the API at localhost",
        stage="runtime",
        severity="high",
        patterns=(r"(?:GET|POST|PUT|DELETE) http://localhost:(?P<port>\d+)/", r"axios.*?baseURL.*?localhost", r"Failed to fetch.*?localhost:(?P<port2>\d+)"),
        root_cause="The built frontend bundle points at http://localhost:{port}, which is the visitor's own machine.",
        explanation=(
            "Frontend environment variables are baked in at build time. If the API URL was not "
            "set during the build, the bundle ships with the developer's localhost URL and every "
            "visitor's browser tries to call their own computer."
        ),
        student_explanation=(
            "Your website is asking for data from 'localhost', which means the visitor's own "
            "computer, not your server. The API address has to be set when the site is built."
        ),
        fixes=(
            F("code_change", "Set the API base URL at build time", "VITE_API_URL / NEXT_PUBLIC_API_URL must be present during the build step, not only at runtime.", params={"file": "frontend build config"}, requires_code_change=True, patch="ARG VITE_API_URL\nENV VITE_API_URL=$VITE_API_URL\nRUN npm run build"),
        ),
        confidence=0.88,
    ),
    S(
        key="secret_committed",
        title="Credential committed to the repository",
        stage="build",
        severity="critical",
        patterns=(
            r"(?P<kind>AKIA[0-9A-Z]{16})",
            r"(?P<kind>sk-[A-Za-z0-9]{20,})",
            r"(?P<kind>ghp_[A-Za-z0-9]{36})",
            r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----",
        ),
        root_cause="A live credential is present in the repository and must be treated as compromised.",
        explanation="Anything pushed to git is in the history permanently. Deleting the line does not revoke the key.",
        student_explanation=(
            "A password or API key is saved inside your code on GitHub. Anyone who sees the "
            "repository can use it. It has to be cancelled and replaced — deleting the line "
            "isn't enough, because git remembers old versions."
        ),
        fixes=(
            F("rotate_secret", "Revoke and reissue the credential now", "The old value is permanently in git history.", params={"secret_kind": "{kind}"}, risk="dangerous"),
            F("code_change", "Move the value to platform-managed environment variables", "Add the file to .gitignore and commit a .env.example with dummy values.", params={"file": ".gitignore"}, requires_code_change=True),
        ),
        confidence=0.98,
    ),
)


# --------------------------------------------------------------------------- #
_COMPILED: list[tuple[Signature, list[re.Pattern]]] = [
    (sig, [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in sig.patterns]) for sig in SIGNATURES
]

BY_KEY: dict[str, Signature] = {s.key: s for s in SIGNATURES}


def _fmt(template: str, groups: dict) -> str:
    out = template
    for k, v in groups.items():
        if v is None:
            continue
        out = out.replace("{" + k + "}", str(v))
    return re.sub(r"\{(\w+)\}", lambda m: m.group(1).replace("_", " "), out)


def match_signatures(events: list[dict], *, max_results: int = 6) -> list[dict]:
    """Score every signature against a list of normalized log events.

    `events` items: {"id", "source", "message", "level", "ts"}
    Returns candidates sorted by score, each with the evidence that matched.
    """
    hits: dict[str, dict] = {}

    for ev in events:
        msg = ev.get("message") or ""
        if not msg:
            continue
        source = ev.get("source", "")
        for sig, patterns in _COMPILED:
            if sig.sources and source and source not in sig.sources:
                continue

            # Try every pattern, not just the first that hits: a single log line
            # often matches one pattern that identifies the failure and another
            # that carries the detail we need for the fix (e.g. an Nginx 502
            # line names both the refusal and the upstream port).
            groups: dict = {}
            first_match = None
            for pat in patterns:
                m = pat.search(msg)
                if not m:
                    continue
                if first_match is None:
                    first_match = m
                groups.update({k: v for k, v in (m.groupdict() or {}).items() if v})

            if first_match is None:
                continue

            entry = hits.setdefault(
                sig.key,
                {
                    "signature_key": sig.key,
                    "title": sig.title,
                    "stage": sig.stage,
                    "severity": sig.severity,
                    "base_confidence": sig.confidence,
                    "hit_count": 0,
                    "groups": {},
                    "evidence": [],
                    "sources": set(),
                },
            )
            entry["hit_count"] += 1
            entry["groups"].update(groups)
            entry["sources"].add(source)
            if len(entry["evidence"]) < 5:
                entry["evidence"].append(
                    {
                        "id": ev.get("id"),
                        "source": source,
                        "ts": ev.get("ts"),
                        "line": msg[:500],
                        "matched": first_match.group(0)[:200],
                    }
                )

    candidates = []
    for key, entry in hits.items():
        sig = BY_KEY[key]
        groups = entry["groups"]
        # More hits and more independent sources => more confident, capped.
        conf = min(
            0.99,
            entry["base_confidence"]
            + min(entry["hit_count"] - 1, 4) * 0.01
            + (len(entry["sources"]) - 1) * 0.02,
        )
        candidates.append(
            {
                "signature_key": key,
                "title": sig.title,
                "stage": sig.stage,
                "severity": sig.severity,
                "confidence": round(conf, 3),
                "hit_count": entry["hit_count"],
                "sources": sorted(entry["sources"]),
                "root_cause": _fmt(sig.root_cause, groups),
                "explanation": _fmt(sig.explanation, groups),
                "student_explanation": _fmt(sig.student_explanation, groups),
                "extracted": groups,
                "evidence": entry["evidence"],
                "fixes": [
                    {
                        "action_type": f.action_type,
                        "title": _fmt(f.title, groups),
                        "rationale": _fmt(f.rationale, groups),
                        "params": {k: _fmt(str(v), groups) for k, v in f.params.items()},
                        "risk": f.risk,
                        "requires_code_change": f.requires_code_change,
                        "patch": f.patch,
                    }
                    for f in sig.fixes
                ],
            }
        )

    severity_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    candidates.sort(
        key=lambda c: (c["confidence"], severity_rank.get(c["severity"], 0), c["hit_count"]),
        reverse=True,
    )
    return candidates[:max_results]
