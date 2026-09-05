"""Engine and session plumbing.

Deliberately thin. The interesting correctness work in MiniLedger happens in
explicit SQL inside a transaction, not in the ORM session lifecycle, so this
module only has to hand out connections that behave predictably.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def build_engine(url: str | None = None) -> Engine:
    """Create an Engine for `url`, defaulting to DATABASE_URL."""
    settings = get_settings()
    return create_engine(
        url or settings.database_url,
        # Recycle dead connections instead of surfacing them as errors. Postgres
        # in Docker gets restarted a lot during development.
        pool_pre_ping=True,
        # POOL SIZING IS A CORRECTNESS-ADJACENT CHOICE HERE.
        #
        # A duplicate request does not fail fast -- it BLOCKS inside the
        # idempotency claim, holding its connection, until the original
        # transaction commits. So the pool has to be wide enough for the
        # expected number of simultaneously blocked retries, or a burst of
        # duplicates exhausts the pool and starts timing out requests that
        # would otherwise have succeeded.
        #
        # 20 + 10 is generous for a single-node demo. In production this would
        # be derived from Postgres max_connections divided across workers, with
        # a proper pooler in front.
        pool_size=20,
        max_overflow=10,
        pool_timeout=30,
        # Bound how long a connection attempt can hang.
        #
        # Without this, pointing the app at an unreachable host does not fail --
        # it BLOCKS, indefinitely, waiting on a TCP connect that will never be
        # answered. For the web app that is a stuck request. For the scheduled
        # reconciliation job it is worse: the job never finishes, never exits
        # non-zero, and never alerts. It just quietly stops running, and the
        # absence of a failure looks exactly like a pass.
        connect_args={"connect_timeout": 10},
        # Never autocommit a stray statement: every write in this project has to
        # sit inside a transaction we opened on purpose.
        future=True,
    )


engine: Engine = build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    # expire_on_commit=False would let us read attributes off an object after
    # commit, but it also lets stale money values escape a closed transaction.
    # Leaving it True forces a fresh read, which is what we want for balances.
    expire_on_commit=True,
    class_=Session,
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception.

    Scripts (seeding, reconciliation) use this. The HTTP layer gets its own
    request-scoped dependency in Phase 2.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI request-scoped session dependency.

    Lives here rather than in app/main.py so that every router depends on the
    SAME callable. FastAPI's dependency_overrides is a single non-recursive
    lookup -- overriding a placeholder that forwards to the real dependency
    would silently leave the forwarded-to one unoverridden, and tests would
    quietly run against the development database.

    Does not commit or roll back. Each route owns its own transaction
    boundary explicitly; see the note at the top of app/main.py.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
