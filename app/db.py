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
