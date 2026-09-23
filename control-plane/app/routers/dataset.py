from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..dataset.export import build_records, export
from ..deps import audit, require_devops
from ..models import Incident, Role, User
from ..rag import store as rag

router = APIRouter(prefix="/api/dataset", tags=["dataset"])


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: User = Depends(require_devops)):
    total = db.query(Incident).count()
    confirmed = db.query(Incident).filter(Incident.confirmed_root_cause != "").count()
    by_sig: dict[str, int] = {}
    for i in db.query(Incident).filter(Incident.confirmed_root_cause != "").all():
        key = i.signature_key or "novel"
        by_sig[key] = by_sig.get(key, 0) + 1
    return {
        "incidents_total": total,
        "confirmed": confirmed,
        "unconfirmed": total - confirmed,
        "ready_for_export": confirmed,
        "by_signature": dict(sorted(by_sig.items(), key=lambda kv: kv[1], reverse=True)),
        "guidance": (
            "Confirm root causes as incidents are resolved. Around 200-300 confirmed incidents "
            "across a good spread of signatures is where fine-tuning a small local model starts "
            "to beat prompting it. Below that, the evaluation split is still valuable on its own."
        ),
    }


@router.post("/export")
def export_dataset(
    eval_fraction: float = Query(0.2, ge=0.0, le=0.5),
    include_unconfirmed: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_devops),
):
    out = Path(settings.repo_cache_dir) / "dataset" / "incidents.jsonl"
    card = export(out, eval_fraction=eval_fraction, include_unconfirmed=include_unconfirmed)
    audit(db, "dataset.export", actor=user.email, actor_role=user.role, detail={"records": card.get("total_records", 0)})
    return card


@router.get("/download/{split}")
def download(split: str, user: User = Depends(require_devops)):
    if split not in ("train", "eval", "card"):
        return {"error": "split must be train, eval or card"}
    suffix = ".card.json" if split == "card" else f".{split}.jsonl"
    path = Path(settings.repo_cache_dir) / "dataset" / f"incidents{suffix}"
    if not path.exists():
        return {"error": "Run POST /api/dataset/export first"}
    return FileResponse(path, filename=path.name, media_type="application/json")


@router.get("/preview")
def preview(limit: int = Query(3, le=10), db: Session = Depends(get_db), _: User = Depends(require_devops)):
    """See exactly what would leave the building before exporting anything."""
    records = build_records(db)
    return {"count": len(records), "sample": records[:limit]}


@router.get("/kb/search")
def kb_search(q: str, k: int = Query(5, le=20), db: Session = Depends(get_db), _: User = Depends(require_devops)):
    return rag.search(db, q, k=k)
