from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def build_engine(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def _on_connect(conn, _):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

    Base.metadata.create_all(engine)
    return engine


def build_session_factory(db_path: Path) -> sessionmaker:
    engine = build_engine(db_path)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def open_session(factory: sessionmaker) -> Generator[Session, None, None]:
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
