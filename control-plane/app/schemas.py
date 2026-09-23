from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


# ------------------------------ auth ------------------------------ #
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    name: str
    email: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str = ""
    role: Literal["admin", "devops", "mentor", "student"] = "student"


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_active: bool

    class Config:
        from_attributes = True


# ------------------------------ servers ------------------------------ #
class ServerCreate(BaseModel):
    name: str
    hostname: str = ""
    cpu_cores: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    port_range_start: int | None = None
    port_range_end: int | None = None


class ServerOut(BaseModel):
    id: str
    name: str
    hostname: str
    cpu_cores: int
    ram_mb: int
    disk_gb: int
    port_range_start: int
    port_range_end: int
    last_seen: datetime | None
    agent_version: str
    online: bool

    class Config:
        from_attributes = True


class ServerEnrolled(ServerOut):
    agent_token: str


# ------------------------------ projects ------------------------------ #
class ProjectCreate(BaseModel):
    name: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,60}$")
    github_repo: str = ""
    team_name: str = ""
    default_branch: str = "main"
    owner_id: str | None = None
    mentor_id: str | None = None
    server_id: str | None = None
    domain: str = ""
    health_path: str = "/health"


class ProjectUpdate(BaseModel):
    name: str | None = None
    github_repo: str | None = None
    team_name: str | None = None
    mentor_id: str | None = None
    server_id: str | None = None
    domain: str | None = None
    health_path: str | None = None
    archived: bool | None = None


class ProjectOut(BaseModel):
    id: str
    slug: str
    name: str
    team_name: str
    github_repo: str
    default_branch: str
    domain: str
    health_path: str
    server_id: str | None
    owner_id: str | None
    mentor_id: str | None
    archived: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ------------------------------ analysis ------------------------------ #
class AnalyzeRequest(BaseModel):
    ref: str = ""
    use_llm: bool = True
    expected_users: int = 50


class AnalysisOut(BaseModel):
    id: str
    project_id: str
    commit_sha: str
    score: int
    grade: str
    blocking_count: int
    breakdown: dict
    detected: dict
    findings: list
    plan: dict
    artifacts: dict
    llm_review: str
    duration_ms: int
    created_at: datetime

    class Config:
        from_attributes = True


# ------------------------------ deployments ------------------------------ #
class DeploymentStart(BaseModel):
    repo: str = ""
    project_slug: str = ""
    commit_sha: str = ""
    branch: str = "main"
    gh_run_id: str = ""
    gh_run_url: str = ""
    triggered_by: str = ""
    container_name: str = ""
    image: str = ""


class DeploymentStartOut(BaseModel):
    id: str
    project_id: str
    port: int | None
    domain: str
    container_name: str
    status: str


class DeploymentFinish(BaseModel):
    status: str
    phase: str = ""
    image: str = ""
    container_name: str = ""
    error_summary: str = ""
    analyze: bool = True
    # "not_attempted" (default) | "succeeded" | "failed" — see Deployment.rollback_status
    rollback_status: str = "not_attempted"


class DeploymentOut(BaseModel):
    id: str
    project_id: str
    server_id: str | None
    commit_sha: str
    branch: str
    status: str
    phase: str
    container_name: str
    image: str
    port: int | None
    domain: str
    gh_run_id: str
    gh_run_url: str
    triggered_by: str
    error_summary: str
    started_at: datetime
    finished_at: datetime | None
    rollback_status: str

    class Config:
        from_attributes = True


# ------------------------------ logs & metrics ------------------------------ #
class LogIngest(BaseModel):
    deployment_id: str | None = None
    project_slug: str | None = None
    source: Literal["actions", "docker", "nginx", "nginx_error", "nginx_access", "app", "system"]
    text: str = ""
    container: str = ""
    stream: str = "stdout"


class MetricIngest(BaseModel):
    deployment_id: str | None = None
    container_name: str | None = None
    cpu_pct: float = 0
    mem_mb: float = 0
    mem_limit_mb: float = 0
    restarts: int = 0
    disk_pct: float = 0
    net_rx_kb: float = 0
    net_tx_kb: float = 0
    http_5xx: int = 0
    http_p95_ms: float = 0


class HeartbeatIn(BaseModel):
    agent_version: str = ""
    cpu_cores: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    disk_pct: float = 0
    load_avg: float = 0
    observed_ports: list[dict] = []
    containers: list[dict] = []
    metrics: list[MetricIngest] = []


class CommandOut(BaseModel):
    id: str
    kind: str
    payload: dict
    fix_action_id: str | None


class CommandResult(BaseModel):
    command_id: str
    ok: bool
    output: str = ""
    error: str = ""
    detail: dict = {}


# ------------------------------ incidents ------------------------------ #
class IncidentOut(BaseModel):
    id: str
    project_id: str
    deployment_id: str | None
    title: str
    status: str
    severity: str
    stage: str
    signature_key: str
    root_cause: str
    explanation: str
    student_explanation: str
    evidence: list
    correlation: dict
    confidence: float
    analysis_source: str
    similar_incidents: list
    created_at: datetime
    resolved_at: datetime | None
    confirmed_root_cause: str
    confirmed_fix: str
    was_ai_correct: bool | None
    auto_verified: bool = False

    class Config:
        from_attributes = True


class FixActionOut(BaseModel):
    id: str
    incident_id: str
    action_type: str
    title: str
    rationale: str
    params: dict
    risk: str
    requires_code_change: bool
    patch: str
    status: str
    order_index: int
    approved_by: str | None
    approved_at: datetime | None
    executed_at: datetime | None
    result: dict
    verification_status: str = "not_applicable"
    verified_at: datetime | None = None
    verification_detail: dict = {}

    class Config:
        from_attributes = True


class AgentTraceOut(BaseModel):
    id: str
    stage: str
    outcome: str
    summary: str
    detail: dict
    fix_action_id: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class ApproveFix(BaseModel):
    params: dict = {}
    note: str = ""


class ConfirmIncident(BaseModel):
    confirmed_root_cause: str
    confirmed_fix: str = ""
    was_ai_correct: bool
    add_to_knowledge_base: bool = True


# ------------------------------ infra ------------------------------ #
class PortAllocateRequest(BaseModel):
    server_id: str
    project_id: str | None = None
    deployment_id: str | None = None
    purpose: str = "app"
    preferred: int | None = None


class NginxRequest(BaseModel):
    project_id: str
    server_id: str | None = None
    domain: str
    upstream_port: int | None = None
    options: dict[str, Any] = {}


class InfraRecommendRequest(BaseModel):
    project_id: str
    expected_users: int = 50
