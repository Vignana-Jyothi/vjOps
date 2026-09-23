"""The root cause engine.

Pipeline:
    correlate -> deterministic signature match -> retrieve similar past incidents
             -> LLM refinement (optional) -> validate -> persist incident + fixes

Two invariants make this safe to run against real student deployments:

1. The engine always produces an answer. If the GPU node is down, the signature
   layer's answer stands on its own with its own confidence.
2. Every claim the model makes is validated before it is stored. Evidence ids
   that aren't in the timeline are dropped, action types outside the whitelist
   are rewritten to `escalate_to_devops`, and confidence is capped by how much
   surviving evidence there is.
"""

from __future__ import annotations

import logging
from datetime import timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..llm.client import llm
from ..llm.prompts import RCA_SYSTEM, rca_user_prompt
from ..models import (
    Deployment,
    FixAction,
    FixStatus,
    Incident,
    IncidentStatus,
    Project,
    Risk,
    utcnow,
)
from ..rag import store as rag
from ..tracing import record as record_trace
from .correlate import build_timeline
from .fixes import ACTION_CATALOG, allowed_action_summary, spec_for
from .signatures import BY_KEY, match_signatures

log = logging.getLogger(__name__)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def analyze_deployment(db: Session, deployment_id: str, *, use_llm: bool = True) -> Incident | None:
    dep = db.get(Deployment, deployment_id)
    if not dep:
        return None
    project = db.get(Project, dep.project_id)

    timeline = build_timeline(db, deployment_id)
    events = timeline["events"]
    if not events:
        log.info("No log events for deployment %s; nothing to analyze", deployment_id)
        return None

    candidates = match_signatures(events)
    similar = _retrieve_similar(db, events, candidates)

    verdict = _llm_verdict(
        project=project,
        dep=dep,
        timeline=timeline,
        candidates=candidates,
        similar=similar,
    ) if use_llm else None

    result = _merge(candidates, verdict, timeline, similar)
    if not result:
        return None

    incident = _persist(db, dep, project, result, timeline, similar)
    return incident


# --------------------------------------------------------------------------- #
def _retrieve_similar(db: Session, events: list[dict], candidates: list[dict]) -> list[dict]:
    query_parts = [c["title"] for c in candidates[:2]]
    query_parts += [e["message"][:200] for e in events if e["level"] in ("error", "critical")][:3]
    query = "\n".join(query_parts) or events[-1]["message"][:300]
    try:
        hits = rag.search(db, query, k=4)
    except Exception as exc:
        log.warning("KB retrieval failed: %s", exc)
        return []
    return [
        {
            "title": h["title"],
            "signature_key": h.get("signature_key", ""),
            "source": h.get("source", ""),
            "similarity": round(float(h.get("score") or 0), 3),
            "content": (h.get("content") or "")[:900],
        }
        for h in hits
        if (h.get("score") or 0) > 0.3
    ]


def _llm_verdict(*, project, dep, timeline, candidates, similar) -> dict | None:
    context = {
        "project": {
            "name": getattr(project, "name", ""),
            "repo": getattr(project, "github_repo", ""),
            "stack_note": "student startup project deployed via GitHub Actions -> Docker -> Nginx",
        },
        "deployment": {
            "status": dep.status,
            "phase": dep.phase,
            "commit": dep.commit_sha[:8],
            "port": dep.port,
            "domain": dep.domain,
            "container": dep.container_name,
        },
        "candidate_causes": [
            {
                "signature_key": c["signature_key"],
                "title": c["title"],
                "root_cause": c["root_cause"],
                "stage": c["stage"],
                "rule_confidence": c["confidence"],
                "matched_evidence_ids": [e["id"] for e in c["evidence"]],
            }
            for c in candidates
        ],
        "similar": similar,
        "allowed_actions": allowed_action_summary(),
        "timeline_text": timeline["text"],
        "metrics": {
            **(timeline.get("metrics") or {}),
            "correlation_signals": timeline.get("signals", []),
        },
    }
    verdict = llm.complete_json(RCA_SYSTEM, rca_user_prompt(context), fallback={})
    if not verdict.get("_llm_used"):
        return None
    return verdict


def _merge(candidates: list[dict], verdict: dict | None, timeline: dict, similar: list[dict]) -> dict | None:
    """Combine the rule engine and the model into one validated result."""
    valid_ids = {e["id"] for e in timeline["events"]}
    top = candidates[0] if candidates else None

    if not verdict:
        if not top:
            return _unknown(timeline)
        return {
            "signature_key": top["signature_key"],
            "title": top["title"],
            "stage": top["stage"],
            "severity": top["severity"],
            "confidence": top["confidence"],
            "root_cause": top["root_cause"],
            "explanation": top["explanation"] + " (Rule-engine diagnosis; the language model was unavailable.)",
            "student_explanation": top["student_explanation"],
            "evidence": [
                {"id": e["id"], "why": "matched failure signature", "line": e["line"], "source": e["source"]}
                for e in top["evidence"]
                if e["id"] in valid_ids
            ],
            "fixes": top["fixes"],
            "source": "signature",
        }

    # --- validate the model's evidence -----------------------------------
    raw_ev = verdict.get("evidence") or []
    evidence = []
    lines = {e["id"]: e for e in timeline["events"]}
    for item in raw_ev:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id", "")).strip()
        if eid in valid_ids:
            evidence.append(
                {
                    "id": eid,
                    "why": str(item.get("why", ""))[:400],
                    "line": lines[eid]["message"][:500],
                    "source": lines[eid]["source"],
                }
            )
    hallucinated = len(raw_ev) - len(evidence)

    sig_key = ""
    matched = None
    for c in candidates:
        rc = (verdict.get("root_cause") or "").lower()
        if c["signature_key"] in (verdict.get("signature_key") or "") or _overlap(c["root_cause"], rc):
            matched = c
            sig_key = c["signature_key"]
            break

    try:
        confidence = float(verdict.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    # Penalise a model that cited evidence which does not exist.
    if hallucinated > 0:
        confidence *= max(0.4, 1 - 0.2 * hallucinated)
    if not evidence:
        confidence = min(confidence, 0.35)
    # Agreement with the rule engine is the strongest signal we have.
    if matched:
        confidence = min(0.99, (confidence + matched["confidence"]) / 2 + 0.05)
    elif top and top["confidence"] >= 0.93:
        # Rule engine is very sure and the model went elsewhere — trust the rules.
        return _merge(candidates, None, timeline, similar)

    fixes = _validate_fixes(verdict.get("fixes") or [], matched)

    return {
        "signature_key": sig_key,
        "title": (matched or {}).get("title") or (verdict.get("root_cause") or "Deployment failure")[:120],
        "stage": verdict.get("stage") or (matched or {}).get("stage") or "runtime",
        "severity": verdict.get("severity") if verdict.get("severity") in SEVERITY_ORDER else (matched or {}).get("severity", "medium"),
        "confidence": round(confidence, 3),
        "root_cause": (verdict.get("root_cause") or (matched or {}).get("root_cause") or "")[:600],
        "explanation": (verdict.get("explanation") or (matched or {}).get("explanation") or "")[:2000],
        "student_explanation": (verdict.get("student_explanation") or (matched or {}).get("student_explanation") or "")[:1200],
        "evidence": evidence,
        "fixes": fixes,
        "source": "hybrid" if matched else "llm",
        "hallucinated_evidence": hallucinated,
    }


def _overlap(a: str, b: str) -> bool:
    aw = {w for w in a.lower().split() if len(w) > 4}
    bw = {w for w in b.lower().split() if len(w) > 4}
    if not aw or not bw:
        return False
    return len(aw & bw) / len(aw) > 0.4


def _validate_fixes(raw_fixes: list, matched: dict | None) -> list[dict]:
    out: list[dict] = []
    for f in raw_fixes[:5]:
        if not isinstance(f, dict):
            continue
        action_type = str(f.get("action_type", "")).strip()
        if action_type not in ACTION_CATALOG:
            # Model invented an action. Keep the intent, drop the capability.
            out.append(
                {
                    "action_type": "escalate_to_devops",
                    "title": str(f.get("title", "Manual step required"))[:200],
                    "rationale": (
                        f"The model proposed '{action_type or 'an unnamed action'}', which is not in "
                        f"the allowed action list, so it cannot be executed automatically. "
                        f"Original reasoning: {str(f.get('rationale',''))[:300]}"
                    ),
                    "params": {"reason": "action outside whitelist"},
                    "risk": "safe",
                    "requires_code_change": False,
                    "patch": str(f.get("patch", ""))[:4000],
                }
            )
            continue
        spec = spec_for(action_type)
        out.append(
            {
                "action_type": action_type,
                "title": str(f.get("title", spec.title))[:200],
                "rationale": str(f.get("rationale", ""))[:800],
                "params": f.get("params") if isinstance(f.get("params"), dict) else {},
                # Risk is decided by the catalog, never by the model.
                "risk": spec.risk.value,
                "requires_code_change": bool(f.get("requires_code_change")) or not spec.executable,
                "patch": str(f.get("patch", ""))[:4000],
            }
        )

    if matched:
        seen = {f["action_type"] for f in out}
        for f in matched["fixes"]:
            if f["action_type"] not in seen:
                out.append({**f, "risk": spec_for(f["action_type"]).risk.value})
    if not out:
        out = [
            {
                "action_type": "escalate_to_devops",
                "title": "Escalate to a DevOps engineer",
                "rationale": "No confident automated fix could be derived from the available evidence.",
                "params": {"reason": "low confidence diagnosis"},
                "risk": "safe",
                "requires_code_change": False,
                "patch": "",
            }
        ]
    return out


def _unknown(timeline: dict) -> dict:
    tail = [e for e in timeline["events"] if e["level"] in ("error", "critical")][-3:]
    return {
        "signature_key": "",
        "title": "Unrecognized deployment failure",
        "stage": "runtime",
        "severity": "medium",
        "confidence": 0.2,
        "root_cause": "No known failure signature matched and the language model was unavailable.",
        "explanation": (
            "This failure does not match any pattern in the signature library. The most recent "
            "error lines are attached as evidence. Once a human confirms the cause, this "
            "incident becomes a new signature candidate."
        ),
        "student_explanation": "We couldn't identify this problem automatically. A DevOps mentor will take a look.",
        "evidence": [{"id": e["id"], "why": "most recent error", "line": e["message"][:500], "source": e["source"]} for e in tail],
        "fixes": [
            {
                "action_type": "escalate_to_devops",
                "title": "Escalate — unknown failure pattern",
                "rationale": "Unmatched failures are the ones worth a human's time; they become new signatures.",
                "params": {"reason": "no signature match"},
                "risk": "safe",
                "requires_code_change": False,
                "patch": "",
            }
        ],
        "source": "none",
    }


# --------------------------------------------------------------------------- #
def _persist(db: Session, dep: Deployment, project, result: dict, timeline: dict, similar: list[dict]) -> Incident:
    incident = Incident(
        project_id=dep.project_id,
        deployment_id=dep.id,
        title=result["title"][:255],
        status=IncidentStatus.diagnosed.value if result["confidence"] >= 0.4 else IncidentStatus.open.value,
        severity=result["severity"],
        stage=result["stage"],
        signature_key=result["signature_key"],
        root_cause=result["root_cause"],
        explanation=result["explanation"],
        student_explanation=result["student_explanation"],
        evidence=result["evidence"],
        correlation={
            "signals": timeline.get("signals", []),
            "sources": timeline.get("sources", []),
            "events_considered": timeline.get("total_raw_events", 0),
            "events_in_timeline": len(timeline.get("events", [])),
            "hallucinated_evidence_dropped": result.get("hallucinated_evidence", 0),
        },
        confidence=result["confidence"],
        analysis_source=result["source"],
        similar_incidents=similar,
    )
    db.add(incident)
    db.flush()

    auto = settings.auto_remediation
    for i, f in enumerate(result["fixes"]):
        spec = spec_for(f["action_type"])
        action = FixAction(
            incident_id=incident.id,
            action_type=f["action_type"],
            title=f["title"],
            rationale=f["rationale"],
            params=f.get("params") or {},
            risk=spec.risk.value,
            requires_code_change=bool(f.get("requires_code_change")) or not spec.executable,
            patch=f.get("patch") or "",
            order_index=i,
            status=FixStatus.proposed.value,
        )
        db.add(action)

    dep.error_summary = result["root_cause"][:2000]
    db.commit()

    log.info(
        "Incident %s created for deployment %s: %s (confidence %.2f, source=%s)",
        incident.id, dep.id, result["title"], result["confidence"], result["source"],
    )

    record_trace(
        db, "investigator",
        (
            f"Correlated {timeline.get('total_raw_events', 0)} raw events across "
            f"{len(timeline.get('sources', []))} source(s) into {len(timeline.get('events', []))}. "
            f"{len(similar)} similar past incident(s) retrieved. "
            f"Diagnosis: {result['title']} (confidence {result['confidence']:.2f}, source={result['source']})."
        ),
        project_id=dep.project_id,
        incident_id=incident.id,
        outcome="pass" if result["confidence"] >= 0.4 else "escalate",
        detail={"signature_key": result["signature_key"], "hallucinated_evidence": result.get("hallucinated_evidence", 0)},
    )
    return incident


# --------------------------------------------------------------------------- #
def learn_from_resolution(db: Session, incident: Incident) -> None:
    """Feed a human-confirmed incident back into the retrieval index.

    This is the flywheel: every incident a DevOps engineer confirms makes the
    next diagnosis of the same class better, without retraining anything.
    """
    if not incident.confirmed_root_cause:
        return
    body = (
        f"Stage: {incident.stage}\n"
        f"Signature: {incident.signature_key or 'novel'}\n"
        f"Confirmed root cause: {incident.confirmed_root_cause}\n"
        f"Confirmed fix: {incident.confirmed_fix}\n"
        f"AI first said: {incident.root_cause}\n"
        f"AI was correct: {incident.was_ai_correct}\n"
        "Key evidence:\n"
        + "\n".join(f"  - [{e.get('source')}] {e.get('line', '')[:200]}" for e in (incident.evidence or [])[:5])
    )
    try:
        rag.add_document(
            db,
            title=f"Resolved: {incident.title}",
            content=body,
            category="incident",
            signature_key=incident.signature_key,
            source="incident",
            incident_id=incident.id,
        )
    except Exception as exc:
        log.warning("Could not index resolved incident %s: %s", incident.id, exc)
