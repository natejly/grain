from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(database_url: str):
    if database_url.startswith("sqlite"):
        path = database_url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # `timeout` is the busy-timeout in seconds: a writer that can't acquire the
        # lock waits instead of raising "database is locked" immediately. WAL lets
        # readers and a writer coexist, so the lock is rarely contended to begin
        # with. Together they stop the recurring lock errors under concurrent
        # embedding/ingestion writes in dev. Harmless on :memory: (SQLite keeps
        # memory mode and ignores the journal pragma).
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True, future=True)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

