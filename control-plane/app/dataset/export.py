"""Export confirmed incidents as an instruction-tuning / evaluation dataset.

This is the long game the incubator is uniquely positioned to play. A general
model knows generic DevOps. After a semester of confirmed incidents, this file
knows how deployments fail *in this specific environment* — the self-hosted
runner's quirks, the shared server's port pressure, the mistakes second-years
actually make.

Two splits come out of the same source:
  * `train` — instruction/response pairs for fine-tuning
  * `eval`  — held-out incidents scored against the confirmed root cause

Only human-confirmed incidents are exported. An unconfirmed AI diagnosis is a
guess, and training on your own guesses is how a model gets confidently wrong.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Deployment, FixAction, FixStatus, Incident, LogEvent, Project
from ..rca.fixes import allowed_action_summary
from .anonymize import Anonymizer, stable_id

SYSTEM = (
    "You are a DevOps root cause analysis engine for a university startup incubator. "
    "Given correlated logs from GitHub Actions, Docker, Nginx and the application, "
    "identify the root cause of the deployment failure, cite the evidence, and propose "
    "a fix from the allowed action list."
)


def build_records(db: Session, *, min_evidence: int = 1, include_unconfirmed: bool = False) -> list[dict]:
    q = db.query(Incident)
    if not include_unconfirmed:
        q = q.filter(Incident.confirmed_root_cause != "")
    incidents = q.order_by(Incident.created_at.asc()).all()

    records: list[dict] = []
    for inc in incidents:
        anon = Anonymizer()
        project = db.get(Project, inc.project_id)
        dep = db.get(Deployment, inc.deployment_id) if inc.deployment_id else None

        events = (
            db.query(LogEvent)
            .filter(LogEvent.deployment_id == inc.deployment_id)
            .order_by(LogEvent.ts.asc())
            .limit(400)
            .all()
        ) if inc.deployment_id else []

        errors = [e for e in events if e.level in ("error", "critical")]
        context_events = (errors or events)[-80:]
        if len(context_events) < min_evidence:
            continue

        timeline = "\n".join(
            f"[{i+1}] {e.ts.strftime('%H:%M:%S')} {e.source:<7} {e.level:<8} | {anon.scrub(e.message)[:600]}"
            for i, e in enumerate(context_events)
        )

        stack = {}
        if project:
            stack = {
                "project": anon.scrub_slug(project.slug),
                "repo": anon.scrub_repo(project.github_repo),
            }

        prompt = (
            f"## Deployment\n"
            f"stage: {inc.stage}\n"
            f"status: {getattr(dep, 'status', 'failed')}\n"
            f"{json.dumps(stack)}\n\n"
            f"## Correlated log timeline\n{timeline}\n\n"
            f"## Allowed fix actions\n"
            f"{json.dumps([a['action_type'] for a in allowed_action_summary()])}\n"
        )

        completion = {
            "root_cause": anon.scrub(inc.confirmed_root_cause or inc.root_cause),
            "stage": inc.stage,
            "severity": inc.severity,
            "signature_key": inc.signature_key or "novel",
            "explanation": anon.scrub(inc.explanation),
            "fix": anon.scrub(inc.confirmed_fix or ""),
        }

        records.append(
            {
                "id": stable_id(inc.id),
                "created_at": inc.created_at.astimezone(timezone.utc).isoformat(),
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": json.dumps(completion, indent=2)},
                ],
                "metadata": {
                    "signature_key": inc.signature_key or "novel",
                    "stage": inc.stage,
                    "severity": inc.severity,
                    "confirmed": bool(inc.confirmed_root_cause),
                    "ai_was_correct": inc.was_ai_correct,
                    "ai_original_diagnosis": anon.scrub(inc.root_cause),
                    "analysis_source": inc.analysis_source,
                    "confidence_at_time": inc.confidence,
                    "sources": sorted({e.source for e in context_events}),
                    "event_count": len(context_events),
                    # diagnosis -> intervention -> outcome, not just
                    # diagnosis -> label: what was actually approved and
                    # whether the Verification Agent found it held. Never
                    # part of the training target itself (`completion`
                    # above) — the model has to produce a diagnosis from
                    # logs alone, the same information it would have at
                    # inference time, before any of this exists. This is
                    # provenance for evaluating the pipeline as a whole,
                    # e.g. "of the incidents diagnosed as X and given fix Y,
                    # how many actually held" — not a second training signal
                    # bolted onto the first.
                    "intervention": _intervention_summary(db, inc),
                },
            }
        )
    return records


def _intervention_summary(db: Session, inc: Incident) -> dict:
    """What was actually done about this incident, and whether the
    Verification Agent found it held. `resolution` collapses that into one
    label for quick filtering; `fix_actions` keeps the detail."""
    fixes = (
        db.query(FixAction)
        .filter(FixAction.incident_id == inc.id)
        .order_by(FixAction.order_index.asc())
        .all()
    )
    fix_summaries = [
        {
            "action_type": f.action_type,
            "status": f.status,
            "verification_status": f.verification_status,
        }
        for f in fixes
    ]

    if not fixes:
        resolution = "no_remediation_attempted"
    elif any(f.verification_status == "passed" for f in fixes):
        resolution = "verified"
    elif any(f.verification_status == "failed" for f in fixes):
        resolution = "failed_verification"
    elif any(f.status == FixStatus.succeeded.value for f in fixes):
        resolution = "applied_unverified"
    elif any(f.status == FixStatus.proposed.value for f in fixes):
        resolution = "proposed_not_approved"
    else:
        resolution = "unresolved"

    return {"resolution": resolution, "fix_actions": fix_summaries}


def export(
    out_path: str | Path,
    *,
    eval_fraction: float = 0.2,
    seed: int = 1337,
    include_unconfirmed: bool = False,
) -> dict:
    db = SessionLocal()
    try:
        records = build_records(db, include_unconfirmed=include_unconfirmed)
    finally:
        db.close()

    if not records:
        return {"records": 0, "note": "No confirmed incidents yet. Confirm root causes in the dashboard first."}

    # Split by signature so the eval set measures generalization to unseen
    # instances of a class, not memorization of one incident.
    by_sig: dict[str, list[dict]] = {}
    for r in records:
        by_sig.setdefault(r["metadata"]["signature_key"], []).append(r)

    rng = random.Random(seed)
    train, evalset = [], []
    for sig, group in by_sig.items():
        rng.shuffle(group)
        n_eval = max(1, int(len(group) * eval_fraction)) if len(group) > 2 else 0
        evalset += group[:n_eval]
        train += group[n_eval:]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    train_path = out_path.with_suffix(".train.jsonl")
    eval_path = out_path.with_suffix(".eval.jsonl")

    for path, rows in ((train_path, train), (eval_path, evalset)):
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    card = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_records": len(records),
        "train": len(train),
        "eval": len(evalset),
        "signatures": {k: len(v) for k, v in sorted(by_sig.items(), key=lambda kv: len(kv[1]), reverse=True)},
        "confirmed_only": not include_unconfirmed,
        "anonymization": "emails, hostnames, IPs, home paths, hashes and repo names pseudonymized per-record; secrets destroyed",
        "files": {"train": str(train_path), "eval": str(eval_path)},
        "caveats": [
            "Records are drawn from one incubator's infrastructure and are not representative of DevOps generally.",
            "Labels are the confirming engineer's judgement, which can be wrong.",
            "Class balance follows real incident frequency — port conflicts and missing dependencies dominate.",
        ],
    }
    card_path = out_path.with_suffix(".card.json")
    card_path.write_text(json.dumps(card, indent=2), encoding="utf-8")
    card["files"]["card"] = str(card_path)
    return card


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Export the ViljaOps incident dataset")
    ap.add_argument("--out", default="/var/lib/viljaops/repos/incidents.jsonl")
    ap.add_argument("--eval-fraction", type=float, default=0.2)
    ap.add_argument("--include-unconfirmed", action="store_true")
    args = ap.parse_args()
    print(json.dumps(export(args.out, eval_fraction=args.eval_fraction, include_unconfirmed=args.include_unconfirmed), indent=2))
