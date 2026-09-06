"""Concurrency: the tests this whole design exists to pass.

These fire genuinely simultaneous HTTP requests at a real uvicorn server, each
from its own thread with its own connection and its own database session. A
sequential retry test would pass against a far weaker implementation -- even a
naive "SELECT then INSERT if absent" check survives being called twice in a
row. It only breaks when two callers are inside it at the same moment, which is
exactly the situation a timeout-and-retry produces in production.

Every test here uses a threading.Barrier so all threads are released at the
same instant, rather than trickling in while earlier ones have already
committed.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

CONCURRENCY = 10


@dataclass
class Result:
    status: int
    replayed: str | None
    transfer_id: str | None
    error: str | None


def _fire(
    base_url: str,
    requests: list[tuple[str, dict]],
) -> list[Result]:
    """Send every (idempotency_key, body) pair at the same instant.

    Each thread opens its own client and warms the TCP connection BEFORE
    waiting on the barrier, so the barrier release is followed immediately by
    the POST rather than by connection setup. Without that warm-up the threads
    would still be handshaking at different times and the requests would arrive
    staggered, quietly weakening the test.
    """
    barrier = Barrier(len(requests))

    def send(item: tuple[str, dict]) -> Result:
        idempotency_key, body = item
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            client.get("/health")  # warm the connection
            barrier.wait()
            response = client.post(
                "/transfers", json=body, headers={"Idempotency-Key": idempotency_key}
            )

        payload = response.json()
        return Result(
            status=response.status_code,
            replayed=response.headers.get("Idempotent-Replay"),
            transfer_id=payload.get("id"),
            error=payload.get("error"),
        )

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(send, requests))


def _transfer_body(
    accounts: dict[str, uuid.UUID], source: str, destination: str, amount: int
) -> dict:
    return {
        "source_account_id": str(accounts[source]),
        "destination_account_id": str(accounts[destination]),
        "amount": amount,
    }


def _fund(
    live_server: str, accounts: dict[str, uuid.UUID], wallet: str, amount: int
) -> None:
    response = httpx.post(
        f"{live_server}/transfers",
        json=_transfer_body(accounts, "house:float", wallet, amount),
        headers={"Idempotency-Key": f"fund-{wallet}-{amount}"},
        timeout=30.0,
    )
    response.raise_for_status()


# --- The headline test -------------------------------------------------------


def test_concurrent_duplicate_requests_create_exactly_one_transfer(
    live_server: str, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    """10 identical requests, same key, fired simultaneously. One transfer."""
    body = _transfer_body(accounts, "house:float", "wallet:alice", 5000)
    results = _fire(live_server, [("same-key", body)] * CONCURRENCY)

    # Every caller got a success. None of them saw an error, and none of them
    # had to know they were racing.
    assert [r.status for r in results] == [201] * CONCURRENCY

    # Exactly one did the work; the rest waited on the unique index and then
    # replayed its result.
    assert sum(1 for r in results if r.replayed == "false") == 1
    assert sum(1 for r in results if r.replayed == "true") == CONCURRENCY - 1

    # All ten callers were handed the SAME transfer.
    assert len({r.transfer_id for r in results}) == 1

    # And the ledger agrees: one transfer, one pair of postings, one key.
    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM ledger_entries")).scalar_one() == 2
    assert (
        session.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 1
    )

    alice = session.execute(
        text("SELECT posted_credits FROM account_balances WHERE account_id = :a"),
        {"a": accounts["wallet:alice"]},
    ).scalar_one()
    assert alice == 5000, "money moved more than once"


def test_concurrent_duplicates_with_a_different_payload_conflict(
    live_server: str, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    """Same key, two different amounts, fired together.

    Whichever request wins the race creates its transfer; the others must be
    told their key was reused rather than handed a transfer for an amount they
    did not ask for.
    """
    requests = [
        ("same-key", _transfer_body(accounts, "house:float", "wallet:alice", amount))
        for amount in (1000, 2000) * (CONCURRENCY // 2)
    ]
    results = _fire(live_server, requests)

    created = [r for r in results if r.status == 201]
    conflicted = [r for r in results if r.status == 409]

    assert len(created) + len(conflicted) == CONCURRENCY
    assert len(created) >= 1
    assert all(r.error == "IdempotencyKeyConflict" for r in conflicted)
    assert len({r.transfer_id for r in created}) == 1

    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 1


# --- Balance integrity under concurrency -------------------------------------


def test_concurrent_distinct_transfers_do_not_corrupt_the_balance(
    live_server: str, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    """10 different transfers, 10 different keys, all at once.

    This is the lost-update test. Without the row lock, several transactions
    would read the same starting balance and write back totals computed from
    it, and some postings would vanish from the cache.
    """
    _fund(live_server, accounts, "wallet:alice", 10 * 1_000)

    requests = [
        (f"key-{i}", _transfer_body(accounts, "wallet:alice", "wallet:bob", 1_000))
        for i in range(CONCURRENCY)
    ]
    results = _fire(live_server, requests)

    assert [r.status for r in results] == [201] * CONCURRENCY
    assert len({r.transfer_id for r in results}) == CONCURRENCY

    rows = dict(
        session.execute(
            text(
                """
                SELECT a.name, b.posted_credits - b.posted_debits AS balance
                FROM accounts a JOIN account_balances b ON b.account_id = a.id
                """
            )
        ).all()  # type: ignore[arg-type]
    )
    assert rows["wallet:alice"] == 0
    assert rows["wallet:bob"] == 10_000

    # The cache is not merely self-consistent; it matches the log.
    drift = session.execute(
        text(
            """
            SELECT count(*)
            FROM account_balances b
            JOIN (
                SELECT account_id,
                       COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0)  AS d,
                       COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0) AS c
                FROM ledger_entries GROUP BY account_id
            ) e ON e.account_id = b.account_id
            WHERE e.d <> b.posted_debits OR e.c <> b.posted_credits
            """
        )
    ).scalar_one()
    assert drift == 0


def test_concurrent_spending_cannot_overdraw(
    live_server: str, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    """Alice has 5_000. Ten simultaneous requests try to spend 1_000 each.

    Exactly five must succeed. This is the check-then-act race: if the balance
    were read outside the lock, every request would see 5_000, all ten would
    consider themselves affordable, and the wallet would end up at -5_000.
    """
    _fund(live_server, accounts, "wallet:alice", 5_000)

    requests = [
        (f"spend-{i}", _transfer_body(accounts, "wallet:alice", "wallet:bob", 1_000))
        for i in range(CONCURRENCY)
    ]
    results = _fire(live_server, requests)

    succeeded = [r for r in results if r.status == 201]
    rejected = [r for r in results if r.status == 422]

    assert len(succeeded) == 5, f"expected 5 successes, got {len(succeeded)}"
    assert len(rejected) == 5
    assert all(r.error == "InsufficientFunds" for r in rejected)

    balance = session.execute(
        text(
            "SELECT posted_credits - posted_debits FROM account_balances "
            "WHERE account_id = :a"
        ),
        {"a": accounts["wallet:alice"]},
    ).scalar_one()
    assert balance == 0, "wallet was overdrawn"


def test_opposing_transfers_do_not_deadlock(
    live_server: str, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    """Alice->Bob and Bob->Alice, interleaved and simultaneous.

    This is the case ordered locking exists for. Without it, transactions grab
    one account each and wait on the other; Postgres breaks the cycle by
    killing one, which surfaces as a 500 to a caller who did nothing wrong.
    """
    _fund(live_server, accounts, "wallet:alice", 20_000)
    _fund(live_server, accounts, "wallet:bob", 20_000)

    requests = []
    for i in range(CONCURRENCY):
        source, destination = (
            ("wallet:alice", "wallet:bob")
            if i % 2 == 0
            else ("wallet:bob", "wallet:alice")
        )
        requests.append(
            (f"cross-{i}", _transfer_body(accounts, source, destination, 500))
        )

    results = _fire(live_server, requests)

    # No deadlock means no 500s. A killed transaction would surface as one.
    assert [r.status for r in results] == [201] * CONCURRENCY, (
        f"unexpected statuses: {[(r.status, r.error) for r in results]}"
    )

    # Money is conserved: five transfers each way at 500, so both wallets end
    # exactly where they started.
    rows = dict(
        session.execute(
            text(
                """
                SELECT a.name, b.posted_credits - b.posted_debits AS balance
                FROM accounts a JOIN account_balances b ON b.account_id = a.id
                """
            )
        ).all()  # type: ignore[arg-type]
    )
    assert rows["wallet:alice"] == 20_000
    assert rows["wallet:bob"] == 20_000

    assert (
        session.execute(
            text("SELECT SUM(signed_amount) FROM ledger_entries")
        ).scalar_one()
        == 0
    )
