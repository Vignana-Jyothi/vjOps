"""Nginx config generation with a safe apply path.

The dangerous part of automating Nginx is that one bad config takes down every
site on the box, not just the one being changed. So the apply protocol is:

    render -> static lint here -> write to a staging path on the server
           -> `nginx -t` on the server -> swap into sites-enabled -> `nginx -t`
           -> reload; on ANY failure, restore the previous file and reload again

The agent enforces the second half; this module owns rendering, linting and the
domain-uniqueness rules that prevent two teams claiming one address.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from ..models import NginxConfig, Project, Server, utcnow

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "nginx"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)

DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")

DEFAULT_OPTIONS = {
    "enable_ssl": False,
    "client_max_body_size": "10m",
    "proxy_read_timeout": "60s",
    "proxy_send_timeout": "60s",
    "proxy_connect_timeout": "5s",
    "websocket": False,
    "websocket_path": "/ws",
    "rate_limit": False,
    "rate_limit_burst": 20,
    "extra_locations": [],
}


class NginxError(RuntimeError):
    pass


def validate_domain(db: Session, domain: str, project_id: str) -> None:
    domain = (domain or "").strip().lower()
    if not DOMAIN_RE.match(domain):
        raise NginxError(f"'{domain}' is not a valid domain name")
    clash = (
        db.query(NginxConfig)
        .filter(NginxConfig.domain == domain, NginxConfig.project_id != project_id, NginxConfig.status.in_(("applied", "validated")))
        .first()
    )
    if clash:
        other = db.get(Project, clash.project_id)
        raise NginxError(
            f"Domain {domain} is already served for project '{getattr(other, 'name', clash.project_id)}'. "
            "Two server_name entries for the same host make Nginx route traffic to whichever "
            "config loads first — pick a different subdomain."
        )


def render(project: Project, *, domain: str, upstream_port: int, options: dict | None = None, config_id: str = "", version: int = 1) -> str:
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    tmpl = _env.get_template("app.conf.j2")
    return tmpl.render(
        project={"name": project.name, "slug": project.slug},
        domain=domain.strip().lower(),
        upstream_port=int(upstream_port),
        upstream_name=f"viljaops_{re.sub(r'[^a-z0-9_]', '_', project.slug.lower())}",
        options=opts,
        config_id=config_id or "pending",
        version=version,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
    )


def lint(config_text: str) -> list[str]:
    """Cheap structural checks before the config ever reaches a server.

    `nginx -t` on the box is authoritative, but catching a brace mismatch here
    saves a round trip and keeps obviously-broken configs off the host.
    """
    problems: list[str] = []

    depth = 0
    in_str = False
    quote = ""
    for i, ch in enumerate(config_text):
        if in_str:
            if ch == quote and config_text[i - 1] != "\\":
                in_str = False
            continue
        if ch in "'\"":
            in_str, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                problems.append("Unbalanced braces: a '}' closes a block that was never opened")
                break
    if depth > 0:
        problems.append(f"Unbalanced braces: {depth} block(s) left open")

    if "server_name" not in config_text:
        problems.append("No server_name directive")
    if "proxy_pass" not in config_text:
        problems.append("No proxy_pass directive — nothing would be forwarded to the app")
    if re.search(r"proxy_pass\s+http://[^;\s]+\s*$", config_text, re.M):
        problems.append("proxy_pass directive is missing its terminating semicolon")

    for m in re.finditer(r"^\s*(?!#)([a-z_]+)\s+[^;{}\n]+$", config_text, re.M):
        line = m.group(0).strip()
        if not line.endswith(("{", "}", ";")):
            problems.append(f"Directive may be missing a semicolon: {line[:70]}")

    if re.search(r"ssl_certificate\s", config_text) and not re.search(r"ssl_certificate_key\s", config_text):
        problems.append("ssl_certificate set without ssl_certificate_key")

    ports = re.findall(r"^\s*listen\s+(?:\[::\]:)?(\d+)", config_text, re.M)
    if not ports:
        problems.append("No listen directive")

    return problems


def create_config(
    db: Session,
    project: Project,
    server: Server,
    *,
    domain: str,
    upstream_port: int,
    options: dict | None = None,
) -> NginxConfig:
    validate_domain(db, domain, project.id)

    prev = (
        db.query(NginxConfig)
        .filter(NginxConfig.project_id == project.id)
        .order_by(NginxConfig.version.desc())
        .first()
    )
    version = (prev.version + 1) if prev else 1

    cfg = NginxConfig(
        project_id=project.id,
        server_id=server.id,
        domain=domain.strip().lower(),
        upstream_port=upstream_port,
        options=options or {},
        version=version,
        status="draft",
    )
    db.add(cfg)
    db.flush()

    text = render(project, domain=domain, upstream_port=upstream_port, options=options, config_id=cfg.id, version=version)
    problems = lint(text)

    cfg.rendered = text
    cfg.checksum = hashlib.sha256(text.encode()).hexdigest()
    cfg.status = "failed" if problems else "validated"
    cfg.validation_output = "\n".join(problems) if problems else "static lint passed; awaiting `nginx -t` on the server"
    db.commit()

    if problems:
        raise NginxError("Generated config failed validation:\n" + "\n".join(problems))
    return cfg


def apply_payload(cfg: NginxConfig, project: Project) -> dict:
    """The exact instructions handed to the agent. The agent does no thinking."""
    return {
        "config_id": cfg.id,
        "site_name": project.slug,
        "domain": cfg.domain,
        "upstream_port": cfg.upstream_port,
        "checksum": cfg.checksum,
        "content": cfg.rendered,
        "target_path": f"/etc/nginx/sites-available/viljaops-{project.slug}.conf",
        "enabled_path": f"/etc/nginx/sites-enabled/viljaops-{project.slug}.conf",
        "backup_suffix": f".viljaops-bak-{int(utcnow().timestamp())}",
        # The agent refuses to reload unless this passes first.
        "require_nginx_test": True,
        "rollback_on_failure": True,
    }


def mark_applied(db: Session, cfg: NginxConfig, result: dict) -> NginxConfig:
    ok = bool(result.get("ok"))
    cfg.status = "applied" if ok else "failed"
    cfg.validation_output = (result.get("nginx_test_output") or result.get("error") or "")[:4000]
    if ok:
        cfg.applied_at = utcnow()
        # Only one applied config per project.
        (
            db.query(NginxConfig)
            .filter(NginxConfig.project_id == cfg.project_id, NginxConfig.id != cfg.id, NginxConfig.status == "applied")
            .update({"status": "superseded"}, synchronize_session=False)
        )
    db.commit()
    return cfg
