"""Shared test fixtures.

Tests run against a real PostgreSQL database (TEST_DATABASE_URL), never SQLite
and never mocks. Most of what MiniLedger claims to guarantee -- deferred
constraint triggers, generated columns, row locking, ON CONFLICT -- is
PostgreSQL behaviour. A test suite that stubbed the database would prove
nothing about the thing being built.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import build_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# TRUNCATE rather than DELETE: the append-only triggers from migration 0002
# reject DELETE on ledger_entries and transfers, and TRUNCATE does not fire
# row-level triggers. RESTART IDENTITY resets the entry sequence so ids are
# predictable within a test.
TRUNCATE_ALL = text(
    "TRUNCATE idempotency_keys, ledger_entries, transfers, account_balances, "
    "accounts RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session")
def engine() -> Engine:
    settings = get_settings()
    url = settings.test_database_url
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # env.py prefers this over DATABASE_URL, so the suite can never migrate or
    # truncate the development database by accident.
    config.set_main_option("sqlalchemy.url", url)
    import os

    os.environ["ALEMBIC_DATABASE_URL"] = url

    # Round-tripping through base gives every session a known-good schema and
    # keeps the downgrade path honest -- a downgrade nobody runs is a downgrade
    # that does not work.
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    return build_engine(url)


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=True, class_=Session)
    with factory() as s:
        s.execute(TRUNCATE_ALL)
        s.commit()
        yield s
        s.rollback()


@pytest.fixture()
def accounts(session: Session) -> dict[str, uuid.UUID]:
    """A minimal chart of accounts: one float, two customer wallets."""
    rows = session.execute(
        text(
            """
            INSERT INTO accounts (name, account_type, allow_negative_balance)
            VALUES ('house:float',  'equity',    true),
                   ('wallet:alice', 'liability', false),
                   ('wallet:bob',   'liability', false)
            RETURNING id, name
            """
        )
    ).all()
    session.commit()
    return {r.name: r.id for r in rows}


def post_raw_transfer(
    session: Session,
    *,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    amount: int,
    description: str | None = None,
) -> uuid.UUID:
    """Write a balanced transfer with raw SQL, bypassing the application.

    Phase 1 has no posting service yet, and these tests are about the schema
    rather than about application code, so they insert directly. That is the
    point: the invariants under test hold against anything that can reach the
    database, not just against the happy path in app/.
    """
    transfer_id = session.execute(
        text(
            """
            INSERT INTO transfers
                (source_account_id, destination_account_id, amount, description)
            VALUES (:src, :dst, :amount, :description)
            RETURNING id
            """
        ),
        {
            "src": debit_account_id,
            "dst": credit_account_id,
            "amount": amount,
            "description": description,
        },
    ).scalar_one()

    session.execute(
        text(
            """
            INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
            VALUES (:tid, :debit_account,  'debit',  :amount),
                   (:tid, :credit_account, 'credit', :amount)
            """
        ),
        {
            "tid": transfer_id,
            "debit_account": debit_account_id,
            "credit_account": credit_account_id,
            "amount": amount,
        },
    )
    session.commit()
    return transfer_id
