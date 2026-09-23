from __future__ import annotations

import json
from typing import Any, Iterator

from sqlalchemy import JSON, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


class VectorType(TypeDecorator):
    """pgvector column on Postgres, JSON list everywhere else (tests/SQLite)."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int = 768, **kw):
        self.dim = dim
        super().__init__(**kw)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                return dialect.type_descriptor(Vector(self.dim))
            except Exception:  # pragma: no cover - pgvector missing
                pass
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return list(value)

    def process_result_value(self, value: Any, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return list(value)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create the extension (if available) and all tables."""
    from . import models  # noqa: F401  (register metadata)

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception:
                # Plain postgres without pgvector: RAG falls back to
                # in-python cosine similarity over the JSON column.
                pass
    Base.metadata.create_all(engine)
