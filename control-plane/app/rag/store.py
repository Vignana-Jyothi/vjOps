from __future__ import annotations

import logging

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..llm.embeddings import cosine, embed
from ..models import KBDocument

log = logging.getLogger(__name__)


def add_document(
    db: Session,
    *,
    title: str,
    content: str,
    category: str = "general",
    signature_key: str = "",
    source: str = "seed",
    incident_id: str | None = None,
) -> KBDocument:
    doc = KBDocument(
        title=title,
        content=content,
        category=category,
        signature_key=signature_key,
        source=source,
        incident_id=incident_id,
        embedding=embed(f"{title}\n{content}"),
    )
    db.add(doc)
    db.commit()
    return doc


def search(db: Session, query: str, k: int = 5, category: str | None = None) -> list[dict]:
    """Vector search with a graceful path for non-pgvector databases."""
    qvec = embed(query)
    if qvec is None:
        return []

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        try:
            where = "WHERE embedding IS NOT NULL"
            params: dict = {"q": str(qvec), "k": k}
            if category:
                where += " AND category = :category"
                params["category"] = category
            rows = db.execute(
                sql_text(
                    f"""
                    SELECT id, title, content, category, signature_key, source,
                           1 - (embedding <=> CAST(:q AS vector)) AS score
                    FROM kb_documents
                    {where}
                    ORDER BY embedding <=> CAST(:q AS vector)
                    LIMIT :k
                    """
                ),
                params,
            ).mappings().all()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("pgvector search failed (%s); falling back to in-python cosine", exc)

    q = db.query(KBDocument)
    if category:
        q = q.filter(KBDocument.category == category)
    scored = []
    for doc in q.limit(2000).all():
        if not doc.embedding:
            continue
        scored.append(
            {
                "id": doc.id,
                "title": doc.title,
                "content": doc.content,
                "category": doc.category,
                "signature_key": doc.signature_key,
                "source": doc.source,
                "score": cosine(qvec, list(doc.embedding)),
            }
        )
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:k]
