from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .config import get_config

config = get_config()

# mode=ro&immutable=1 makes SQLite itself refuse writes to the snapshot; query_only is a
# second guard. Never add() or commit() through CorticoSession.
cortico_engine = create_engine(
    f"sqlite:///file:{config.cortico_db_abspath}?mode=ro&immutable=1&uri=true",
    connect_args={"check_same_thread": False},
)


@event.listens_for(cortico_engine, "connect")
def _enforce_query_only(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA query_only = ON;")


CorticoSession = sessionmaker(bind=cortico_engine, autoflush=False, autocommit=False)
CorticoBase = declarative_base()

tagging_engine = create_engine(
    f"sqlite:///{config.tagging_db_abspath}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(tagging_engine, "connect")
def _enable_foreign_keys(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys = ON;")


TaggingSession = sessionmaker(bind=tagging_engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_cortico_db():
    db: Session = CorticoSession()
    try:
        yield db
    finally:
        db.close()


def get_tagging_db():
    db: Session = TaggingSession()
    try:
        yield db
    finally:
        db.close()
