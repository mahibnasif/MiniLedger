"""The posting path: locking, overdraft rules, and cache maintenance."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import AccountNotFound, InsufficientFunds, InvalidTransfer
from app.ledger import cached_all_snapshots, derive_all_snapshots, derive_balance
from app.transfers import post_transfer


def test_transfer_moves_both_sides(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    posted = post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=5000,
        description="opening float",
    )
    session.commit()

    assert posted.amount == 5000
    assert posted.currency == "USD"
    assert posted.description == "opening float"

    assert derive_balance(session, accounts["wallet:alice"]) == 5000
    assert derive_balance(session, accounts["house:float"]) == -5000


def test_cache_agrees_with_the_log_after_posting(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """The property Phase 3 will police. Asserted here so a regression in the
    write path is caught at the point it is introduced."""
    post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=10_000,
    )
    for _ in range(3):
        post_transfer(
            session,
            source_account_id=accounts["wallet:alice"],
            destination_account_id=accounts["wallet:bob"],
            amount=1_200,
        )
    session.commit()

    derived = {s.name: s for s in derive_all_snapshots(session)}
    cached = {s.name: s for s in cached_all_snapshots(session)}

    assert set(derived) == set(cached)
    for name in derived:
        assert derived[name].totals == cached[name].totals, name
        assert derived[name].balance == cached[name].balance, name


def test_entry_count_and_high_water_mark_are_maintained(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    post_transfer(
        session,
        source_account_id=accounts["wallet:alice"],
        destination_account_id=accounts["wallet:bob"],
        amount=1000,
    )
    session.commit()

    cached = {s.name: s for s in cached_all_snapshots(session)}
    assert cached["wallet:alice"].totals.entry_count == 2
    assert cached["wallet:bob"].totals.entry_count == 1

    highest = session.execute(text("SELECT max(id) FROM ledger_entries")).scalar_one()
    assert cached["wallet:bob"].totals.last_entry_id == highest


def test_every_transfer_still_sums_to_zero(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=7_500,
    )
    session.commit()
    assert (
        session.execute(text("SELECT SUM(signed_amount) FROM ledger_entries")).scalar_one()
        == 0
    )


# --- Overdraft rules ---------------------------------------------------------


def test_wallet_cannot_be_overdrawn(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=1_000,
    )
    session.commit()

    with pytest.raises(InsufficientFunds) as excinfo:
        post_transfer(
            session,
            source_account_id=accounts["wallet:alice"],
            destination_account_id=accounts["wallet:bob"],
            amount=1_500,
        )

    error = excinfo.value
    assert error.account_name == "wallet:alice"
    assert error.balance == 1_000
    assert error.requested == 1_500
    assert error.shortfall == 500

    session.rollback()
    assert derive_balance(session, accounts["wallet:alice"]) == 1_000


def test_spending_the_exact_balance_is_allowed(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """Off-by-one guard: zero is a legal balance, negative is not."""
    post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=1_000,
    )
    post_transfer(
        session,
        source_account_id=accounts["wallet:alice"],
        destination_account_id=accounts["wallet:bob"],
        amount=1_000,
    )
    session.commit()
    assert derive_balance(session, accounts["wallet:alice"]) == 0


def test_float_account_may_go_negative(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """allow_negative_balance is per-account data, not a hardcoded type rule."""
    post_transfer(
        session,
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=999_999,
    )
    session.commit()
    assert derive_balance(session, accounts["house:float"]) == -999_999


def test_credit_can_overdraw_a_debit_normal_account(session: Session) -> None:
    """The destination is checked too, because crediting an asset reduces it."""
    cash_id, wallet_id = (
        session.execute(
            text(
                """
                INSERT INTO accounts (name, account_type, allow_negative_balance)
                VALUES ('house:cash', 'asset', false),
                       ('wallet:carol', 'liability', true)
                RETURNING id
                """
            )
        )
        .scalars()
        .all()
    )
    session.commit()

    # Crediting house:cash decreases it, and it holds nothing yet.
    with pytest.raises(InsufficientFunds) as excinfo:
        post_transfer(
            session,
            source_account_id=wallet_id,
            destination_account_id=cash_id,
            amount=2_500,
        )
    assert excinfo.value.account_name == "house:cash"


# --- Rejected requests -------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, -5000])
def test_non_positive_amounts_are_rejected(
    session: Session, accounts: dict[str, uuid.UUID], amount: int
) -> None:
    with pytest.raises(InvalidTransfer, match="amount must be positive"):
        post_transfer(
            session,
            source_account_id=accounts["house:float"],
            destination_account_id=accounts["wallet:alice"],
            amount=amount,
        )


def test_self_transfer_is_rejected(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(InvalidTransfer, match="must be different accounts"):
        post_transfer(
            session,
            source_account_id=accounts["wallet:alice"],
            destination_account_id=accounts["wallet:alice"],
            amount=100,
        )


def test_unknown_account_is_rejected(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    missing = uuid.uuid4()
    with pytest.raises(AccountNotFound) as excinfo:
        post_transfer(
            session,
            source_account_id=accounts["wallet:alice"],
            destination_account_id=missing,
            amount=100,
        )
    assert excinfo.value.account_id == missing


def test_opposing_transfers_lock_accounts_in_the_same_order(
    session: Session, accounts: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deadlock avoidance lives in the ORDER the lock statements are issued.

    A test that only checked final balances would still pass if the ordering
    were dropped, and the bug would surface later as random "deadlock detected"
    errors under load. So the order itself is asserted.

    The property that matters is not merely "sorted" but that Alice-to-Bob and
    Bob-to-Alice reach for the *same* account first. That is what makes a
    waits-for cycle impossible.
    """
    from app import transfers as transfers_module

    # Fund both wallets before the spy is installed, so only the transfers
    # under test contribute to the recorded order.
    for wallet in ("wallet:alice", "wallet:bob"):
        post_transfer(
            session,
            source_account_id=accounts["house:float"],
            destination_account_id=accounts[wallet],
            amount=10_000,
        )
    session.commit()

    lock_order: list[uuid.UUID] = []
    original_execute = session.execute

    def spy(statement, params=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        if statement is transfers_module._LOCK_ACCOUNT and params:
            lock_order.append(params["account_id"])
        return original_execute(statement, params, *args, **kwargs)

    monkeypatch.setattr(session, "execute", spy)

    post_transfer(
        session,
        source_account_id=accounts["wallet:alice"],
        destination_account_id=accounts["wallet:bob"],
        amount=100,
    )
    session.commit()
    alice_to_bob = list(lock_order)

    lock_order.clear()
    post_transfer(
        session,
        source_account_id=accounts["wallet:bob"],
        destination_account_id=accounts["wallet:alice"],
        amount=100,
    )
    session.commit()
    bob_to_alice = list(lock_order)

    assert len(alice_to_bob) == 2
    assert alice_to_bob == sorted(alice_to_bob, key=str)
    # The whole point: reversing the transfer does not reverse the lock order.
    assert alice_to_bob == bob_to_alice
