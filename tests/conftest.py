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


# Folds a set of just-written entries into account_balances exactly the way
# app/transfers.py does. Duplicated deliberately rather than imported: a test
# that reuses the production statement cannot tell you whether the production
# statement is right.
_FOLD_INTO_CACHE = """
    , deltas AS (
        SELECT account_id,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)  AS posted_debits,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0) AS posted_credits,
               COUNT(*) AS entry_count,
               MAX(id)  AS last_entry_id
        FROM new_entries GROUP BY account_id
    )
    INSERT INTO account_balances AS b
        (account_id, posted_debits, posted_credits, entry_count, last_entry_id, updated_at)
    SELECT account_id, posted_debits, posted_credits, entry_count, last_entry_id, now()
    FROM deltas
    ON CONFLICT (account_id) DO UPDATE SET
        posted_debits  = b.posted_debits  + EXCLUDED.posted_debits,
        posted_credits = b.posted_credits + EXCLUDED.posted_credits,
        entry_count    = b.entry_count    + EXCLUDED.entry_count,
        last_entry_id  = GREATEST(COALESCE(b.last_entry_id, 0), EXCLUDED.last_entry_id),
        updated_at     = now()
"""


def post_raw_transfer(
    session: Session,
    *,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    amount: int,
    description: str | None = None,
    maintain_cache: bool = False,
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

    entries = """
        WITH new_entries AS (
            INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
            VALUES (:tid, :debit_account,  'debit',  :amount),
                   (:tid, :credit_account, 'credit', :amount)
            RETURNING id, account_id, direction, amount
        )
    """
    # Without the fold, the cache falls behind and reconciliation reports
    # drift -- which is what most of these tests want. Pass maintain_cache=True
    # when the test needs a ledger that is internally perfect, so that whatever
    # it IS testing is the only thing wrong.
    if maintain_cache:
        entries += _FOLD_INTO_CACHE
    else:
        entries += " SELECT 1 FROM new_entries"

    session.execute(
        text(entries),
        {
            "tid": transfer_id,
            "debit_account": debit_account_id,
            "credit_account": credit_account_id,
            "amount": amount,
        },
    )
    session.commit()
    return transfer_id


@pytest.fixture()
def client(engine: Engine, session: Session):
    """A TestClient whose requests each get their OWN database session.

    Depends on `session` so the truncation in that fixture runs first, but
    deliberately does not hand that session to the app: sharing one session
    across requests would hide exactly the cross-connection behaviour -- row
    locks, ON CONFLICT contention -- that this project is about.
    """
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app

    factory = sessionmaker(bind=engine, class_=Session)

    def override_get_session() -> Iterator[Session]:
        request_session = factory()
        try:
            yield request_session
        finally:
            request_session.close()

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def live_server(engine: Engine, session: Session) -> Iterator[str]:
    """A real uvicorn server on an ephemeral port, in a background thread.

    TestClient is not good enough for the concurrency tests. It drives the ASGI
    app through a portal, which does not reproduce N independent clients each
    holding their own connection and contending on the same index. Those tests
    need genuine sockets and genuine parallel database connections, so they get
    a genuine server.

    The server runs in this process, so dependency_overrides still applies and
    requests can be pointed at the test database.
    """
    import threading
    import time

    import uvicorn

    from app.db import get_session
    from app.main import app

    factory = sessionmaker(bind=engine, class_=Session)

    def override_get_session() -> Iterator[Session]:
        request_session = factory()
        try:
            yield request_session
        finally:
            request_session.close()

    app.dependency_overrides[get_session] = override_get_session

    # port=0 lets the OS pick a free port, so parallel test runs cannot collide.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not (server.started and server.servers):
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 30s")
        time.sleep(0.02)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        app.dependency_overrides.clear()


@pytest.fixture()
def funded(session: Session, accounts: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    """A small, healthy ledger: Alice funded with 50000, Bob with 25000.

    Funded through execute_transfer rather than raw SQL, so every transfer has
    an idempotency key and the provenance check has a clean baseline.
    """
    from app.transfers import execute_transfer

    for wallet, amount in (("wallet:alice", 50_000), ("wallet:bob", 25_000)):
        execute_transfer(
            session,
            idempotency_key=f"seed:{wallet}",
            source_account_id=accounts["house:float"],
            destination_account_id=accounts[wallet],
            amount=amount,
            description=f"opening balance for {wallet}",
        )
    session.commit()
    return accounts


@pytest.fixture()
def settlement(session: Session, funded: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    """The funded chart of accounts plus the card settlement account."""
    from app.issuing import SETTLEMENT_ACCOUNT_NAME

    settlement_id = session.execute(
        text(
            "INSERT INTO accounts (name, account_type, allow_negative_balance) "
            "VALUES (:name, 'liability', false) RETURNING id"
        ),
        {"name": SETTLEMENT_ACCOUNT_NAME},
    ).scalar_one()
    session.commit()
    return {**funded, SETTLEMENT_ACCOUNT_NAME: settlement_id}


@pytest.fixture()
def card(session: Session, settlement: dict[str, uuid.UUID]) -> str:
    """A Stripe card bound to Alice's wallet, which holds 50000."""
    session.execute(
        text(
            "INSERT INTO cards (stripe_card_id, stripe_cardholder_id, account_id, last4) "
            "VALUES ('ic_test_alice', 'ich_test_alice', :account_id, '4242')"
        ),
        {"account_id": settlement["wallet:alice"]},
    )
    session.commit()
    return "ic_test_alice"
