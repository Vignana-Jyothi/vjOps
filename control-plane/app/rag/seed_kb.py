"""Seed the knowledge base from the signature library plus incubator playbooks.

This gives retrieval something useful on day one, before any real incidents have
been confirmed. As engineers confirm incidents, `learn_from_resolution` adds
records from this specific environment alongside these.
"""

from __future__ import annotations

import logging

from ..db import SessionLocal, init_db
from ..models import KBDocument
from ..rca.signatures import SIGNATURES
from . import store

log = logging.getLogger(__name__)

PLAYBOOKS = [
    (
        "Port allocation policy on shared incubator servers",
        "infrastructure",
        """Every deployment gets a host port from the platform's port registry, never a
hard-coded one. Applications bind 0.0.0.0 inside the container on their natural
port (8000, 3000, ...) and the platform publishes that to an allocated host port
bound to 127.0.0.1 only. Nothing except Nginx is reachable from outside the
server.

Symptoms that this policy was bypassed: 'port is already allocated' on deploy,
two projects fighting over the same port after a reboot, or a container that
works until another team deploys.

Ports 22, 80, 443, 5432, 6379, 8080, 27017 and 11434 are never allocated.""",
    ),
    (
        "Why student apps fail after deployment but work locally",
        "runtime",
        """The five recurring causes, in the order they actually occur:

1. localhost. Inside a container it means the container; in a browser bundle it
   means the visitor's machine. Database hosts must be service names; frontend
   API URLs must be the public domain.
2. Missing environment variables. .env is git-ignored, so it never reaches the
   image. The values must be registered with the platform.
3. Missing dependencies. Installed by hand on the laptop, never added to
   requirements.txt or package.json.
4. Architecture mismatch. Images built on Apple Silicon do not run on amd64
   servers. Build on the self-hosted runner.
5. Memory. A laptop has 16 GB; the container limit is 512 MB. Loading a whole
   dataset or ML model into memory is fatal.""",
    ),
    (
        "Nginx change protocol",
        "proxy",
        """A bad Nginx config takes down every site on the server, not just one. The
platform therefore never edits a live config in place. It writes to a staging
path, runs `nginx -t`, swaps the file into sites-enabled, runs `nginx -t` again,
and only then reloads. Any failure restores the previous file and reloads again.

Per-project access and error logs are mandatory: without them a 502 cannot be
attributed to a project, and root cause analysis degrades to guesswork.

Two configs with the same server_name make Nginx serve whichever loaded first —
one team's traffic silently reaches another team's app. The domain registry
prevents this at generation time.""",
    ),
    (
        "Reading exit codes",
        "runtime",
        """0   clean exit — for a server, this usually means the process daemonised and
    PID 1 returned. The container will stop.
1   generic application error. Read the first lines of the log, not the last.
125 the docker run command itself was wrong.
126 the entrypoint is not executable (chmod +x) — often CRLF line endings.
127 the command in CMD/ENTRYPOINT does not exist in the image.
137 SIGKILL — almost always the OOM killer. Raise the limit or fix the leak.
139 segfault — usually a native dependency compiled for a different platform.
143 SIGTERM — a normal shutdown request; the container was asked to stop.""",
    ),
    (
        "Docker build cache and layer ordering",
        "build",
        """The single highest-leverage change to a student Dockerfile: copy the
dependency manifest and install BEFORE copying the source.

    COPY requirements.txt .
    RUN pip install -r requirements.txt
    COPY . .

With this order, editing a source file reuses the cached install layer and the
build takes seconds. With `COPY . .` first, every commit reinstalls every
dependency — which is also the most common reason builds hit the timeout and
the most common reason the server's disk fills with build cache.""",
    ),
    (
        "Escalation policy",
        "process",
        """Escalate to a human DevOps engineer when:
  - diagnosis confidence is below 0.5
  - the same signature has recurred three or more times for one project
  - the proposed fix is classified dangerous (data loss possible)
  - the condition is host-level (disk full, host RAM exhausted, Nginx cannot bind)
  - no signature matched and the model produced no evidence-backed answer

Unmatched failures are the valuable ones. Each confirmed novel cause becomes a
new signature and a new dataset record.""",
    ),
    (
        "What the platform will and will not do automatically",
        "policy",
        """The platform proposes; a named human approves. Every executed action is
recorded with the approver's identity, the exact command, and its output.

Never automatic under any setting: stopping another team's container, setting a
secret value, running database migrations, rotating credentials.

The agent has no general shell. It executes only the whitelisted action types,
with parameters supplied by the control plane. If the model proposes something
outside that list, the proposal is downgraded to an escalation.""",
    ),
]


def seed(force: bool = False) -> dict:
    init_db()
    db = SessionLocal()
    try:
        existing = db.query(KBDocument).filter(KBDocument.source == "seed").count()
        if existing and not force:
            return {"skipped": True, "existing_seed_docs": existing}

        if force:
            db.query(KBDocument).filter(KBDocument.source == "seed").delete()
            db.commit()

        count = 0
        for sig in SIGNATURES:
            body = (
                f"Failure: {sig.title}\n"
                f"Stage: {sig.stage}  Severity: {sig.severity}\n\n"
                f"Root cause pattern: {sig.root_cause}\n\n"
                f"Why it happens: {sig.explanation}\n\n"
                f"Plain-language version: {sig.student_explanation}\n\n"
                "Log patterns that identify it:\n"
                + "\n".join(f"  - {p}" for p in sig.patterns)
                + "\n\nFixes:\n"
                + "\n".join(f"  - [{f.action_type}] {f.title}: {f.rationale}" for f in sig.fixes)
            )
            store.add_document(
                db,
                title=sig.title,
                content=body,
                category="signature",
                signature_key=sig.key,
                source="seed",
            )
            count += 1

        for title, category, body in PLAYBOOKS:
            store.add_document(db, title=title, content=body, category=category, source="seed")
            count += 1

        return {"seeded": count}
    finally:
        db.close()


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(seed(force="--force" in sys.argv), indent=2))
