"""Balances are derived from the entry log, and the sign convention is right.

The sign rule is the part of double-entry that is easiest to get backwards, so
it is pinned down here in both directions rather than assumed.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ledger import (
    AccountTotals,
    balance_of,
    cached_all_snapshots,
    cached_balance,
    derive_all_snapshots,
    derive_balance,
    derive_totals,
    format_minor_units,
)
from tests.conftest import post_raw_transfer


# --- The sign rule, with no database involved --------------------------------


@pytest.mark.parametrize(
    ("normal_balance", "debits", "credits", "expected"),
    [
        # A customer wallet is credit-normal: crediting it gives the customer
        # money, debiting it takes money away.
        ("credit", 0, 5000, 5000),
        ("credit", 2000, 5000, 3000),
        ("credit", 5000, 0, -5000),
        # House cash is debit-normal: the same rows read the other way round.
        ("debit", 5000, 0, 5000),
        ("debit", 5000, 2000, 3000),
        ("debit", 0, 5000, -5000),
    ],
)
def test_balance_of_applies_the_accounts_normal_direction(
    normal_balance: str, debits: int, credits: int, expected: int
) -> None:
    totals = AccountTotals(posted_debits=debits, posted_credits=credits)
    assert balance_of(normal_balance, totals) == expected


def test_balance_of_rejects_an_unknown_direction() -> None:
    with pytest.raises(ValueError, match="unknown normal_balance"):
        balance_of("sideways", AccountTotals())


@pytest.mark.parametrize(
    ("minor_units", "expected"),
    [(0, "0.00 USD"), (5, "0.05 USD"), (5000, "50.00 USD"), (-1234, "-12.34 USD")],
)
def test_format_minor_units(minor_units: int, expected: str) -> None:
    assert format_minor_units(minor_units) == expected


# --- Derivation against a real ledger ----------------------------------------


def test_funding_a_wallet_moves_both_sides(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """One transfer, two opposite effects, still summing to zero overall."""
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
        description="opening float",
    )

    # Alice is credit-normal and was credited: she now holds $50.
    assert derive_balance(session, accounts["wallet:alice"]) == 5000

    # The float is credit-normal and was debited: it is now $50 in the hole,
    # which is correct -- we handed out money we did not take in.
    assert derive_balance(session, accounts["house:float"]) == -5000


def test_debit_normal_account_reads_the_other_way(session: Session) -> None:
    cash_id, wallet_id = session.execute(
        text(
            """
            INSERT INTO accounts (name, account_type, allow_negative_balance)
            VALUES ('house:cash', 'asset', true), ('wallet:carol', 'liability', false)
            RETURNING id
            """
        )
    ).scalars().all()
    session.commit()

    # A deposit: our bank cash goes up, and so does what we owe the customer.
    post_raw_transfer(
        session,
        debit_account_id=cash_id,
        credit_account_id=wallet_id,
        amount=5000,
        description="deposit",
    )

    # Debiting a debit-normal account increases it. Same two rows as above,
    # opposite reading, because the account type differs.
    assert derive_balance(session, cash_id) == 5000
    assert derive_balance(session, wallet_id) == 5000


def test_derivation_accumulates_over_many_transfers(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=10_000,
    )
    for _ in range(3):
        post_raw_transfer(
            session,
            debit_account_id=accounts["wallet:alice"],
            credit_account_id=accounts["wallet:bob"],
            amount=1_500,
        )

    assert derive_balance(session, accounts["wallet:alice"]) == 10_000 - 4_500
    assert derive_balance(session, accounts["wallet:bob"]) == 4_500

    totals = derive_totals(session, accounts["wallet:alice"])
    assert totals.posted_credits == 10_000
    assert totals.posted_debits == 4_500
    assert totals.entry_count == 4


def test_whole_ledger_sums_to_zero(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """The invariant that makes the ledger checkable end to end.

    Balances are per-account and signed by direction, so they do not sum to
    zero. The raw signed amounts do -- across every entry ever written.
    """
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=10_000,
    )
    post_raw_transfer(
        session,
        debit_account_id=accounts["wallet:alice"],
        credit_account_id=accounts["wallet:bob"],
        amount=2_500,
    )

    net = session.execute(text("SELECT SUM(signed_amount) FROM ledger_entries")).scalar_one()
    assert net == 0


def test_accounts_with_no_entries_still_appear(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """A LEFT JOIN, so reconciliation cannot silently skip an untouched account."""
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )

    snapshots = {s.name: s for s in derive_all_snapshots(session)}
    assert set(snapshots) == {"house:float", "wallet:alice", "wallet:bob"}

    assert snapshots["wallet:bob"].balance == 0
    assert snapshots["wallet:bob"].totals.entry_count == 0
    assert snapshots["wallet:bob"].totals.last_entry_id is None
    assert snapshots["wallet:alice"].balance == 5000


def test_last_entry_id_tracks_the_log_high_water_mark(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    highest = session.execute(text("SELECT max(id) FROM ledger_entries")).scalar_one()
    assert derive_totals(session, accounts["wallet:alice"]).last_entry_id == highest


# --- Reading the cache -------------------------------------------------------


def test_cached_reads_come_from_the_cache_table_not_the_log(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """Proof that the two paths are genuinely independent.

    Nothing maintains the cache until Phase 2, so writing a deliberately wrong
    row here and seeing `cached_balance` report it -- while `derive_balance`
    reports the truth -- is exactly the drift that Phase 3 exists to catch.
    """
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    session.execute(
        text(
            "INSERT INTO account_balances (account_id, posted_credits, entry_count) "
            "VALUES (:a, 9999, 1)"
        ),
        {"a": accounts["wallet:alice"]},
    )
    session.commit()

    assert derive_balance(session, accounts["wallet:alice"]) == 5000
    assert cached_balance(session, accounts["wallet:alice"]) == 9999

    cached = {s.name: s for s in cached_all_snapshots(session)}
    assert cached["wallet:alice"].balance == 9999
    # No cache row at all must read as zero, not disappear.
    assert cached["wallet:bob"].balance == 0
