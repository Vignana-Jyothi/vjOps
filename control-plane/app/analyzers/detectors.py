"""Static detection of what a student repository actually is.

Everything here is filesystem + regex only: no code is executed from the repo,
because we are analyzing untrusted student code on infrastructure that hosts
other teams' projects.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build",
    ".next", ".nuxt", "target", "vendor", ".idea", ".vscode", "coverage", ".pytest_cache",
    "site-packages", ".mypy_cache", ".gradle", "bin", "obj",
}
TEXT_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yml", ".yaml", ".toml", ".cfg",
    ".ini", ".env", ".txt", ".md", ".sh", ".java", ".go", ".rb", ".php", ".rs",
    ".html", ".css", ".sql", ".xml", ".gradle", ".properties", ".conf", "",
}
MAX_FILE_BYTES = 400_000


def walk_files(root: Path, limit: int = 6000) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".terraform")]
        for fn in filenames:
            p = Path(dirpath) / fn
            out.append(p)
            if len(out) >= limit:
                return out
    return out


def read_text(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return ""
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------- #
LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".java": "java", ".go": "go", ".rb": "ruby", ".php": "php",
    ".rs": "rust", ".cs": "csharp",
}

FRAMEWORK_MARKERS = [
    # (name, kind, file globs, content regex)
    ("fastapi", "backend", ("requirements.txt", "pyproject.toml"), r"\bfastapi\b"),
    ("django", "backend", ("requirements.txt", "pyproject.toml", "manage.py"), r"\bdjango\b"),
    ("flask", "backend", ("requirements.txt", "pyproject.toml"), r"\bflask\b"),
    ("express", "backend", ("package.json",), r'"express"'),
    ("nestjs", "backend", ("package.json",), r'"@nestjs/core"'),
    ("spring-boot", "backend", ("pom.xml", "build.gradle"), r"spring-boot"),
    ("gin", "backend", ("go.mod",), r"gin-gonic/gin"),
    ("rails", "backend", ("Gemfile",), r"\brails\b"),
    ("laravel", "backend", ("composer.json",), r"laravel/framework"),
    ("react", "frontend", ("package.json",), r'"react"'),
    ("next", "frontend", ("package.json",), r'"next"'),
    ("vue", "frontend", ("package.json",), r'"vue"'),
    ("angular", "frontend", ("package.json",), r'"@angular/core"'),
    ("svelte", "frontend", ("package.json",), r'"svelte"'),
    ("vite", "build", ("package.json",), r'"vite"'),
    ("streamlit", "backend", ("requirements.txt", "pyproject.toml"), r"\bstreamlit\b"),
]

DB_MARKERS = [
    ("postgresql", r"psycopg|postgres(?:ql)?://|\bpg\b|\"pg\"|sqlalchemy\.postgres|POSTGRES_"),
    ("mysql", r"mysqlclient|pymysql|mysql://|\"mysql2\"|MYSQL_"),
    ("mongodb", r"pymongo|mongoose|mongodb(?:\+srv)?://|MONGO_"),
    ("redis", r"\bredis\b|REDIS_URL"),
    ("sqlite", r"sqlite:///|sqlite3"),
    ("elasticsearch", r"elasticsearch"),
]

ML_MARKERS = r"\b(torch|tensorflow|transformers|ultralytics|sentence-transformers|xgboost|lightgbm|opencv-python|whisper|diffusers)\b"

ENV_PATTERNS = [
    re.compile(r"os\.environ\[[\"'](?P<k>[A-Z][A-Z0-9_]{2,})[\"']\]"),
    re.compile(r"os\.environ\.get\(\s*[\"'](?P<k>[A-Z][A-Z0-9_]{2,})[\"']"),
    re.compile(r"os\.getenv\(\s*[\"'](?P<k>[A-Z][A-Z0-9_]{2,})[\"']"),
    re.compile(r"process\.env\.(?P<k>[A-Z][A-Z0-9_]{2,})"),
    re.compile(r"process\.env\[[\"'](?P<k>[A-Z][A-Z0-9_]{2,})[\"']\]"),
    re.compile(r"System\.getenv\(\s*\"(?P<k>[A-Z][A-Z0-9_]{2,})\""),
    re.compile(r"\bEnv\.get\(\"(?P<k>[A-Z][A-Z0-9_]{2,})\""),
    re.compile(r"\$\{(?P<k>[A-Z][A-Z0-9_]{2,})(?::[-?][^}]*)?\}"),
]

PORT_PATTERNS = [
    re.compile(r"EXPOSE\s+(?P<port>\d{2,5})"),
    re.compile(r"\.listen\(\s*(?P<port>\d{2,5})"),
    re.compile(r"port\s*[=:]\s*(?P<port>\d{2,5})", re.I),
    re.compile(r"--port[= ](?P<port>\d{2,5})"),
    re.compile(r"runserver\s+0\.0\.0\.0:(?P<port>\d{2,5})"),
    re.compile(r"uvicorn.*?--port\s+(?P<port>\d{2,5})"),
]

HEALTH_PATTERNS = re.compile(
    r"[\"'`/](?:health|healthz|_health|livez|readyz|ping|status)[\"'`/]?", re.I
)

LOCALHOST_API = re.compile(
    r"[\"'`](?:https?://)?(?:localhost|127\.0\.0\.1)(?::(?P<port>\d{2,5}))?(?P<path>/[\w/\-]*)?[\"'`]"
)


def detect(root: Path) -> dict:
    files = walk_files(root)
    names = {rel(root, p) for p in files}
    basenames = {p.name for p in files}

    langs: dict[str, int] = {}
    env_vars: dict[str, list[str]] = {}
    ports: dict[int, list[str]] = {}
    dbs: set[str] = set()
    frameworks: dict[str, str] = {}
    localhost_refs: list[dict] = []
    has_health = False
    ml = False
    loc = 0

    manifest_text: dict[str, str] = {}
    for p in files:
        if p.name in {
            "requirements.txt", "pyproject.toml", "package.json", "go.mod", "Gemfile",
            "pom.xml", "build.gradle", "composer.json", "Pipfile", "setup.py",
        }:
            manifest_text[p.name] = read_text(p)

    for p in files:
        ext = p.suffix.lower()
        r = rel(root, p)
        if ext in LANG_BY_EXT:
            langs[LANG_BY_EXT[ext]] = langs.get(LANG_BY_EXT[ext], 0) + 1
        if ext not in TEXT_EXT and p.name not in {"Dockerfile", "Makefile", "Procfile"}:
            continue
        text = read_text(p)
        if not text:
            continue
        loc += text.count("\n")

        for pat in ENV_PATTERNS:
            for m in pat.finditer(text):
                k = m.group("k")
                if k in {"PATH", "HOME", "PWD", "USER", "LANG", "TERM", "SHELL", "NODE_ENV"}:
                    continue
                env_vars.setdefault(k, [])
                if r not in env_vars[k] and len(env_vars[k]) < 5:
                    env_vars[k].append(r)

        for pat in PORT_PATTERNS:
            for m in pat.finditer(text):
                try:
                    port = int(m.group("port"))
                except (ValueError, IndexError):
                    continue
                if 1 <= port <= 65535 and port not in (0,):
                    ports.setdefault(port, [])
                    if r not in ports[port] and len(ports[port]) < 5:
                        ports[port].append(r)

        for db_name, pattern in DB_MARKERS:
            if re.search(pattern, text, re.I):
                dbs.add(db_name)

        if re.search(ML_MARKERS, text, re.I):
            ml = True
        if HEALTH_PATTERNS.search(text):
            has_health = True

        # Frontend code shipping a localhost API URL is a top-3 real failure.
        if ext in {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".env", ""} or p.name.startswith(".env"):
            for m in LOCALHOST_API.finditer(text):
                if len(localhost_refs) < 12:
                    localhost_refs.append({"file": r, "match": m.group(0)[:120]})

    for name, kind, manifests, pattern in FRAMEWORK_MARKERS:
        for mf in manifests:
            body = manifest_text.get(mf) or (read_text(root / mf) if (root / mf).exists() else "")
            if body and re.search(pattern, body, re.I):
                frameworks[name] = kind
                break

    # Docker & compose
    dockerfiles = sorted(n for n in names if Path(n).name.lower().startswith("dockerfile"))
    composes = sorted(n for n in names if re.match(r"(docker-)?compose(\.\w+)?\.ya?ml$", Path(n).name, re.I))
    dockerfile_info = analyze_dockerfile(root / dockerfiles[0]) if dockerfiles else {}
    compose_info = analyze_compose(root / composes[0]) if composes else {}

    workflows = sorted(n for n in names if n.startswith(".github/workflows/"))

    return {
        "languages": dict(sorted(langs.items(), key=lambda kv: kv[1], reverse=True)),
        "primary_language": max(langs, key=langs.get) if langs else "unknown",
        "frameworks": frameworks,
        "backend": [k for k, v in frameworks.items() if v == "backend"],
        "frontend": [k for k, v in frameworks.items() if v == "frontend"],
        "databases": sorted(dbs),
        "ml_workload": ml,
        "env_vars": env_vars,
        "ports": {str(k): v for k, v in sorted(ports.items())},
        "has_health_endpoint": has_health,
        "localhost_references": localhost_refs,
        "dockerfiles": dockerfiles,
        "compose_files": composes,
        "dockerfile": dockerfile_info,
        "compose": compose_info,
        "workflows": workflows,
        "has_dockerignore": ".dockerignore" in names,
        "has_gitignore": ".gitignore" in names,
        "has_env_example": any(n.lower() in {".env.example", ".env.sample", ".env.template", "env.example"} for n in names),
        "has_readme": any(n.lower().startswith("readme") for n in names),
        "has_tests": any(re.search(r"(^|/)(tests?|__tests__|spec)/", n) or re.search(r"\.(test|spec)\.[jt]sx?$|^test_.*\.py$", Path(n).name) for n in names),
        "has_lockfile": bool(basenames & {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "go.sum"}),
        "committed_env_files": sorted(n for n in names if Path(n).name in {".env", ".env.local", ".env.production"}),
        "file_count": len(files),
        "approx_loc": loc,
        "manifests": sorted(manifest_text.keys()),
    }


# --------------------------------------------------------------------------- #
def analyze_dockerfile(path: Path) -> dict:
    text = read_text(path)
    if not text:
        return {}
    lines = [l.strip() for l in text.splitlines()]
    instr = [(l.split()[0].upper(), l) for l in lines if l and not l.startswith("#") and l.split()]
    kinds = [k for k, _ in instr]
    froms = [l for k, l in instr if k == "FROM"]
    base = froms[0].split()[1] if froms else ""
    copies = [l for k, l in instr if k in ("COPY", "ADD")]
    runs = [l for k, l in instr if k == "RUN"]

    return {
        "path": path.name,
        "base_image": base,
        "base_image_pinned": bool(re.search(r":[\w\.\-]+$", base)) and not base.endswith(":latest"),
        "uses_latest_tag": base.endswith(":latest") or (":" not in base and base != ""),
        "multistage": len(froms) > 1,
        "stages": len(froms),
        "has_healthcheck": "HEALTHCHECK" in kinds,
        "has_user": "USER" in kinds,
        "runs_as_root": "USER" not in kinds,
        "has_expose": "EXPOSE" in kinds,
        "expose_ports": [int(m.group(1)) for l in instr if l[0] == "EXPOSE" for m in re.finditer(r"(\d{2,5})", l[1])],
        "has_workdir": "WORKDIR" in kinds,
        "copies_everything_first": bool(copies and re.match(r"(COPY|ADD)\s+\.\s", copies[0])),
        "installs_before_copy": _install_before_copy(instr),
        "apt_without_cleanup": any("apt-get install" in l and "rm -rf /var/lib/apt/lists" not in l for l in runs),
        "has_entrypoint": "ENTRYPOINT" in kinds or "CMD" in kinds,
        "cmd": next((l for k, l in reversed(instr) if k in ("CMD", "ENTRYPOINT")), ""),
        "instruction_count": len(instr),
    }


def _install_before_copy(instr: list[tuple[str, str]]) -> bool:
    """True when the dependency install happens before COPY . — i.e. cached well."""
    copy_all = next((i for i, (k, l) in enumerate(instr) if k in ("COPY", "ADD") and re.match(r"(COPY|ADD)\s+\.\s", l)), None)
    install = next((i for i, (k, l) in enumerate(instr) if k == "RUN" and re.search(r"(pip install|npm ci|npm install|yarn install|go mod download|bundle install)", l)), None)
    if copy_all is None or install is None:
        return True
    return install < copy_all


def analyze_compose(path: Path) -> dict:
    text = read_text(path)
    if not text:
        return {}
    try:
        import yaml

        data = yaml.safe_load(text) or {}
    except Exception:
        return {"parse_error": True}
    services = data.get("services") or {}
    published: list[dict] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for p in svc.get("ports") or []:
            s = str(p)
            m = re.match(r"^(?:(?P<host_ip>[\d\.]+):)?(?P<host>\d+):(?P<container>\d+)", s)
            if m:
                published.append({"service": name, "host_port": int(m.group("host")), "container_port": int(m.group("container")), "bound_to_all": not m.group("host_ip")})
    build_services = [
        name
        for name, svc in services.items()
        if isinstance(svc, dict) and svc.get("build") is not None
    ]
    return {
        "path": path.name,
        "services": list(services.keys()),
        "service_count": len(services),
        "build_services": build_services,
        "published_ports": published,
        "has_named_volumes": bool(data.get("volumes")),
        "uses_restart_policy": any(isinstance(s, dict) and s.get("restart") for s in services.values()),
        "has_healthchecks": any(isinstance(s, dict) and s.get("healthcheck") for s in services.values()),
        "has_resource_limits": any(isinstance(s, dict) and (s.get("deploy", {}) or {}).get("resources") or (isinstance(s, dict) and s.get("mem_limit")) for s in services.values()),
        "env_file_refs": sorted({str(e) for s in services.values() if isinstance(s, dict) for e in ([s.get("env_file")] if isinstance(s.get("env_file"), str) else (s.get("env_file") or []))}),
    }


def read_package_json(root: Path) -> dict:
    p = root / "package.json"
    if not p.exists():
        return {}
    try:
        return json.loads(read_text(p) or "{}")
    except json.JSONDecodeError:
        return {}
