from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..analyzers import repo as repo_analyzer
from ..analyzers.plan import recommend_resources
from ..config import settings
from ..db import get_db
from ..deps import audit, current_user, require_devops
from ..models import Deployment, Incident, Project, RepoAnalysis, RiskSnapshot, Role, User
from ..schemas import (
    AnalysisOut,
    AnalyzeRequest,
    InfraRecommendRequest,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _visible(db: Session, user: User):
    q = db.query(Project)
    if user.role == Role.student.value:
        q = q.filter(Project.owner_id == user.id)
    return q


@router.get("", response_model=list[ProjectOut])
def list_projects(
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    q = _visible(db, user)
    if not include_archived:
        q = q.filter(Project.archived.is_(False))
    return q.order_by(Project.created_at.desc()).limit(500).all()


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db), actor: User = Depends(require_devops)):
    if db.query(Project).filter(Project.slug == payload.slug).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Slug '{payload.slug}' is already taken")
    project = Project(**payload.model_dump())
    db.add(project)
    db.commit()
    audit(db, "project.create", actor=actor.email, actor_role=actor.role, target_type="project", target_id=project.id)
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = _visible(db, user).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db), actor: User = Depends(require_devops)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(project, k, v)
    db.commit()
    audit(db, "project.update", actor=actor.email, actor_role=actor.role, target_type="project", target_id=project.id)
    return project


@router.get("/{project_id}/overview")
def overview(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = _visible(db, user).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    analysis = (
        db.query(RepoAnalysis).filter(RepoAnalysis.project_id == project_id).order_by(RepoAnalysis.created_at.desc()).first()
    )
    deployments = (
        db.query(Deployment).filter(Deployment.project_id == project_id).order_by(Deployment.started_at.desc()).limit(10).all()
    )
    incidents = (
        db.query(Incident).filter(Incident.project_id == project_id).order_by(Incident.created_at.desc()).limit(10).all()
    )
    risk = db.query(RiskSnapshot).filter(RiskSnapshot.project_id == project_id).order_by(RiskSnapshot.ts.desc()).first()

    return {
        "project": ProjectOut.model_validate(project).model_dump(),
        "readiness": {
            "score": analysis.score,
            "grade": analysis.grade,
            "blocking_count": analysis.blocking_count,
            "analyzed_at": analysis.created_at.isoformat(),
        }
        if analysis
        else None,
        "deployments": [
            {
                "id": d.id,
                "status": d.status,
                "commit_sha": d.commit_sha[:8],
                "branch": d.branch,
                "port": d.port,
                "started_at": d.started_at.isoformat(),
                "finished_at": d.finished_at.isoformat() if d.finished_at else None,
                "error_summary": d.error_summary[:200],
            }
            for d in deployments
        ],
        "incidents": [
            {
                "id": i.id,
                "title": i.title,
                "status": i.status,
                "severity": i.severity,
                "confidence": i.confidence,
                "created_at": i.created_at.isoformat(),
            }
            for i in incidents
        ],
        "risk": {
            "score": risk.risk_score,
            "band": risk.band,
            "needs_mentor": risk.needs_mentor,
            "recommendation": risk.recommendation,
        }
        if risk
        else None,
    }


# --------------------------------------------------------------------------- #
# Layer 1
# --------------------------------------------------------------------------- #
@router.post("/{project_id}/analyze", response_model=AnalysisOut, status_code=201)
def analyze_project(
    project_id: str,
    payload: AnalyzeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    project = _visible(db, user).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.github_repo:
        raise HTTPException(400, "This project has no GitHub repository configured")

    dest = Path(settings.repo_cache_dir) / project.slug
    clone = repo_analyzer.clone_repo(project.github_repo, dest, ref=payload.ref or project.default_branch, token=settings.github_token)
    if not clone["ok"]:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Could not clone {project.github_repo}. Check the repository exists and the platform "
            f"has access to it.\n\n{clone['error']}",
        )
    if clone["size_mb"] > settings.max_repo_mb:
        raise HTTPException(413, f"Repository is {clone['size_mb']} MB, above the {settings.max_repo_mb} MB analysis limit")

    result = repo_analyzer.analyze(dest, use_llm=payload.use_llm)
    result["plan"]["resources"] = recommend_resources(
        result["detected"], expected_users=payload.expected_users, use_llm=payload.use_llm
    )

    analysis = RepoAnalysis(
        project_id=project.id,
        commit_sha=clone["commit_sha"],
        score=result["score"],
        grade=result["grade"],
        blocking_count=result["blocking_count"],
        breakdown=result["breakdown"],
        detected=result["detected"],
        findings=result["findings"],
        plan=result["plan"],
        artifacts=result["artifacts"],
        llm_review=result.get("llm_review", ""),
        duration_ms=result["duration_ms"],
    )
    db.add(analysis)
    db.commit()
    audit(
        db, "project.analyze", actor=user.email, actor_role=user.role,
        target_type="project", target_id=project.id,
        detail={"score": result["score"], "blockers": result["blocking_count"]},
    )
    return analysis


@router.get("/{project_id}/analyses", response_model=list[AnalysisOut])
def list_analyses(project_id: str, limit: int = Query(10, le=50), db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not _visible(db, user).filter(Project.id == project_id).first():
        raise HTTPException(404, "Project not found")
    return (
        db.query(RepoAnalysis)
        .filter(RepoAnalysis.project_id == project_id)
        .order_by(RepoAnalysis.created_at.desc())
        .limit(limit)
        .all()
    )


@router.post("/infra/recommend")
def infra_recommendation(payload: InfraRecommendRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    analysis = (
        db.query(RepoAnalysis)
        .filter(RepoAnalysis.project_id == payload.project_id)
        .order_by(RepoAnalysis.created_at.desc())
        .first()
    )
    if not analysis:
        raise HTTPException(404, "Run a repository analysis first — the recommendation is derived from the detected stack")
    return recommend_resources(analysis.detected, expected_users=payload.expected_users)
