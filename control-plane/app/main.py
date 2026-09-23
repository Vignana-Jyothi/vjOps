from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import check_production_safety, settings
from .db import SessionLocal, init_db
from .llm.client import llm
from .models import Role, User
from .routers import agents, auth, dataset, deployments, incidents, infra, observability, projects, webhooks
from .security import hash_password

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("viljaops")


def bootstrap_admin() -> None:
    db = SessionLocal()
    try:
        if db.query(User).count():
            return
        admin = User(
            email=settings.bootstrap_admin_email.lower(),
            name="Platform Admin",
            role=Role.admin.value,
            hashed_password=hash_password(settings.bootstrap_admin_password),
        )
        db.add(admin)
        db.commit()
        log.warning(
            "Created bootstrap admin %s — change this password immediately.",
            admin.email,
        )
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = check_production_safety(settings)
    if problems:
        # Refuse to come up rather than come up unsafe — an operator can
        # always set ENVIRONMENT=development to skip this while iterating,
        # but that has to be a deliberate choice, not the accidental default.
        msg = "Refusing to start in production with unsafe defaults:\n  - " + "\n  - ".join(problems)
        log.error(msg)
        raise RuntimeError(msg)
    init_db()
    bootstrap_admin()
    health = llm.health()
    if health["available"]:
        log.info("LLM backend reachable at %s (model %s)", health["backend"], health["configured_model"])
        if not health.get("model_loaded"):
            log.warning(
                "Model '%s' is not loaded on the backend. Run: ollama pull %s",
                health["configured_model"], health["configured_model"],
            )
    else:
        log.warning(
            "LLM backend unavailable (%s). The rule engine will handle diagnosis on its own "
            "until inference is back — nothing else is affected.",
            health.get("error"),
        )
    yield


app = FastAPI(
    title="ViljaOps",
    version="1.0.0",
    description=(
        "AI DevOps intelligence for a university startup incubator: pre-deployment "
        "readiness analysis, correlated root cause analysis of deployment failures, "
        "and continuous risk detection — with every corrective action gated on human approval."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins.split(",") if settings.cors_allowed_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth, projects, deployments, incidents, agents, infra, observability, webhooks, dataset):
    app.include_router(r.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error. The incident has been logged."})


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "service": "viljaops"}


@app.get("/api/system/status", tags=["meta"])
def system_status():
    """Everything an operator needs to know about whether the platform is healthy."""
    from sqlalchemy import text

    from .db import engine

    db_ok, db_error = True, ""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok, db_error = False, str(exc)[:300]

    health = llm.health()
    return {
        "database": {"ok": db_ok, "error": db_error},
        "llm": health,
        "auto_remediation": settings.auto_remediation,
        "port_range": [settings.port_range_start, settings.port_range_end],
        "degraded_mode": not health["available"],
        "degraded_note": (
            "Inference is unavailable, so diagnoses come from the deterministic signature "
            "library only. Detection, correlation, port allocation, Nginx generation and fix "
            "execution are unaffected."
            if not health["available"]
            else None
        ),
    }
