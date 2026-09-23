from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .db import Base, VectorType


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    devops = "devops"
    mentor = "mentor"
    student = "student"


class DeploymentStatus(str, enum.Enum):
    queued = "queued"
    building = "building"
    deploying = "deploying"
    running = "running"
    failed = "failed"
    stopped = "stopped"


class IncidentStatus(str, enum.Enum):
    open = "open"
    analyzing = "analyzing"
    diagnosed = "diagnosed"
    fixing = "fixing"
    resolved = "resolved"
    dismissed = "dismissed"


class FixStatus(str, enum.Enum):
    proposed = "proposed"
    approved = "approved"
    rejected = "rejected"
    executing = "executing"
    succeeded = "succeeded"
    failed = "failed"


class Risk(str, enum.Enum):
    safe = "safe"          # read-only or trivially reversible
    moderate = "moderate"  # restarts / config reloads
    dangerous = "dangerous"  # data loss possible — never auto-run


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=Role.student.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- #
# Fleet
# --------------------------------------------------------------------------- #
class Server(Base):
    __tablename__ = "servers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    token_hash: Mapped[str] = mapped_column(String(255), default="")
    cpu_cores: Mapped[int] = mapped_column(Integer, default=0)
    ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    disk_gb: Mapped[int] = mapped_column(Integer, default=0)
    port_range_start: Mapped[int] = mapped_column(Integer, default=settings.port_range_start)
    port_range_end: Mapped[int] = mapped_column(Integer, default=settings.port_range_end)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def online(self) -> bool:
        if not self.last_seen:
            return False
        last = self.last_seen
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (utcnow() - last).total_seconds() < 120


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    team_name: Mapped[str] = mapped_column(String(160), default="")
    github_repo: Mapped[str] = mapped_column(String(255), default="")  # org/repo
    default_branch: Mapped[str] = mapped_column(String(80), default="main")
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    mentor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    server_id: Mapped[str | None] = mapped_column(ForeignKey("servers.id"), nullable=True)
    domain: Mapped[str] = mapped_column(String(255), default="")
    # Used by the Verification Agent's affirmative health check (see
    # remediation/verify.py) — a plain HTTP GET against this path on the
    # deployment's mapped host port after a fix executes. Layer 1 already
    # tells students to add one; nothing previously acted on it after the
    # fact.
    health_path: Mapped[str] = mapped_column(String(255), default="/health")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    deployments: Mapped[list["Deployment"]] = relationship(back_populates="project")


class RepoAnalysis(Base):
    """Layer 1 output — deployment readiness."""

    __tablename__ = "repo_analyses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    commit_sha: Mapped[str] = mapped_column(String(40), default="")
    score: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[str] = mapped_column(String(2), default="F")
    blocking_count: Mapped[int] = mapped_column(Integer, default=0)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    detected: Mapped[dict] = mapped_column(JSON, default=dict)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_review: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    server_id: Mapped[str | None] = mapped_column(ForeignKey("servers.id"), nullable=True)
    commit_sha: Mapped[str] = mapped_column(String(40), default="")
    branch: Mapped[str] = mapped_column(String(80), default="main")
    status: Mapped[str] = mapped_column(String(20), default=DeploymentStatus.queued.value, index=True)
    phase: Mapped[str] = mapped_column(String(40), default="")
    container_name: Mapped[str] = mapped_column(String(120), default="")
    image: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain: Mapped[str] = mapped_column(String(255), default="")
    gh_run_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    gh_run_url: Mapped[str] = mapped_column(String(400), default="")
    triggered_by: Mapped[str] = mapped_column(String(120), default="")
    error_summary: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the workflow's own rollback step (see viljaops-deploy.yml) actually
    # achieved after a failed deploy — "not_attempted" (nothing to roll back
    # to, or this deployment itself succeeded), "succeeded" (verified the
    # restored container is running), or "failed" (rollback was attempted
    # but the container did not come up — the project has NO application
    # running at all, which is a materially worse situation than "the new
    # version failed to deploy" and deserves to be visible as such).
    rollback_status: Mapped[str] = mapped_column(String(16), default="not_attempted")

    project: Mapped[Project] = relationship(back_populates="deployments")


class LogEvent(Base):
    """Normalized log line from any source. This is the RCA engine's raw fuel."""

    __tablename__ = "log_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    server_id: Mapped[str | None] = mapped_column(ForeignKey("servers.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(24), index=True)  # actions|docker|nginx|app|system
    stream: Mapped[str] = mapped_column(String(24), default="stdout")
    level: Mapped[str] = mapped_column(String(12), default="info", index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


Index("ix_log_events_dep_ts", LogEvent.deployment_id, LogEvent.ts)


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"), nullable=True, index=True)
    server_id: Mapped[str | None] = mapped_column(ForeignKey("servers.id"), nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cpu_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mem_mb: Mapped[float] = mapped_column(Float, default=0.0)
    mem_limit_mb: Mapped[float] = mapped_column(Float, default=0.0)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    disk_pct: Mapped[float] = mapped_column(Float, default=0.0)
    net_rx_kb: Mapped[float] = mapped_column(Float, default=0.0)
    net_tx_kb: Mapped[float] = mapped_column(Float, default=0.0)
    http_5xx: Mapped[int] = mapped_column(Integer, default=0)
    http_p95_ms: Mapped[float] = mapped_column(Float, default=0.0)


# --------------------------------------------------------------------------- #
# Incidents & fixes (Layer 2)
# --------------------------------------------------------------------------- #
class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default=IncidentStatus.open.value, index=True)
    severity: Mapped[str] = mapped_column(String(12), default="medium")
    stage: Mapped[str] = mapped_column(String(24), default="")  # build|deploy|runtime|proxy
    signature_key: Mapped[str] = mapped_column(String(80), default="", index=True)
    root_cause: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    student_explanation: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    correlation: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    analysis_source: Mapped[str] = mapped_column(String(24), default="")  # signature|llm|hybrid
    similar_incidents: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Human-confirmed ground truth — this is what makes the fine-tuning set real
    confirmed_root_cause: Mapped[str] = mapped_column(Text, default="")
    confirmed_fix: Mapped[str] = mapped_column(Text, default="")
    confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    was_ai_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exported_to_dataset: Mapped[bool] = mapped_column(Boolean, default=False)
    # Operational confirmation that an approved fix actually held, distinct from
    # `was_ai_correct` (which judges the *diagnosis*, confirmed by a human for
    # the training set). Set by the Verification Agent, never by a human.
    auto_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class FixAction(Base):
    __tablename__ = "fix_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(255), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(12), default=Risk.moderate.value)
    requires_code_change: Mapped[bool] = mapped_column(Boolean, default=False)
    patch: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=FixStatus.proposed.value, index=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # --- Verification (Phase 4 close-out: "apply -> verify", not just "apply") ---
    # not_applicable: advisory fix, nothing was executed
    # pending:        commands succeeded, watching metrics/anomalies for the grace window
    # passed:         the ActionSpec.verify criterion held for the whole window
    # failed:         a recurrence was observed; the incident was reopened
    # unknown:        window elapsed with no telemetry to judge by (agent silent)
    verification_status: Mapped[str] = mapped_column(String(16), default="not_applicable", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentCommand(Base):
    """Work queue the server agent long-polls. Nothing runs that isn't here."""

    __tablename__ = "agent_commands"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id"), index=True)
    fix_action_id: Mapped[str | None] = mapped_column(ForeignKey("fix_actions.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Infrastructure ownership
# --------------------------------------------------------------------------- #
class PortAllocation(Base):
    __tablename__ = "port_allocations"
    __table_args__ = (UniqueConstraint("server_id", "port", name="uq_server_port"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id"), index=True)
    port: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(80), default="app")
    status: Mapped[str] = mapped_column(String(16), default="allocated", index=True)  # allocated|released|reserved
    note: Mapped[str] = mapped_column(String(255), default="")
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NginxConfig(Base):
    __tablename__ = "nginx_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("servers.id"))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    upstream_port: Mapped[int] = mapped_column(Integer)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    rendered: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|validated|applied|failed|rolled_back
    validation_output: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Layer 3 — risk
# --------------------------------------------------------------------------- #
class RiskSnapshot(Base):
    __tablename__ = "risk_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    band: Mapped[str] = mapped_column(String(12), default="green")
    signals: Mapped[list] = mapped_column(JSON, default=list)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    needs_mentor: Mapped[bool] = mapped_column(Boolean, default=False)


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    metric: Mapped[str] = mapped_column(String(32), default="")
    severity: Mapped[str] = mapped_column(String(12), default="warning")
    value: Mapped[float] = mapped_column(Float, default=0.0)
    baseline: Mapped[float] = mapped_column(Float, default=0.0)
    z_score: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    prediction: Mapped[str] = mapped_column(Text, default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# Knowledge base (RAG)
# --------------------------------------------------------------------------- #
class KBDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(48), default="general", index=True)
    signature_key: Mapped[str] = mapped_column(String(80), default="", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(48), default="seed")  # seed|incident
    incident_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding = mapped_column(VectorType(settings.embed_dim), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentTrace(Base):
    """One line per pipeline stage, so the four-agent lifecycle in the design
    doc (Repository -> Investigator -> Remediation -> Verification) is
    something a person can actually read on an incident, not just an
    architecture diagram.

    This does not replace the engine/analyzer code that does the work — it is
    a narration layer written by that code as it runs.
    """

    __tablename__ = "agent_traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), nullable=True, index=True)
    fix_action_id: Mapped[str | None] = mapped_column(ForeignKey("fix_actions.id"), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(24), index=True)  # repository|investigator|remediation|verification
    outcome: Mapped[str] = mapped_column(String(16), default="info")  # info|pass|fail|escalate
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(160), default="system")
    actor_role: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(48), default="")
    target_id: Mapped[str] = mapped_column(String(48), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
