"""The database rejects data that would corrupt the ledger.

Every test here writes raw SQL rather than going through application code. If
an invariant only holds because app/ is careful, it is not really an invariant
-- it is a convention waiting for the first script somebody writes at 2am.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import post_raw_transfer

# --- Generated columns -------------------------------------------------------


@pytest.mark.parametrize(
    ("account_type", "expected_normal_balance"),
    [
        ("asset", "debit"),
        ("expense", "debit"),
        ("liability", "credit"),
        ("equity", "credit"),
        ("revenue", "credit"),
    ],
)
def test_normal_balance_is_derived_from_account_type(
    session: Session, account_type: str, expected_normal_balance: str
) -> None:
    normal_balance = session.execute(
        text(
            "INSERT INTO accounts (name, account_type) VALUES (:n, :t) "
            "RETURNING normal_balance"
        ),
        {"n": f"probe:{account_type}", "t": account_type},
    ).scalar_one()
    assert normal_balance == expected_normal_balance


def test_normal_balance_cannot_be_overridden(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """It is a generated column, so there is no code path that can set it wrong."""
    with pytest.raises(Exception) as excinfo:
        session.execute(
            text(
                "UPDATE accounts SET normal_balance = 'debit' WHERE name = 'wallet:alice'"
            )
        )
    assert "can only be updated to DEFAULT" in str(excinfo.value)


def test_signed_amount_encodes_direction(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    rows = session.execute(
        text("SELECT direction, amount, signed_amount FROM ledger_entries ORDER BY id")
    ).all()

    assert [(r.direction, r.amount, r.signed_amount) for r in rows] == [
        ("debit", 5000, -5000),
        ("credit", 5000, 5000),
    ]


# --- CHECK constraints -------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "sql", "expected_constraint"),
    [
        (
            "unknown account type",
            "INSERT INTO accounts (name, account_type) VALUES ('x', 'chequing')",
            "ck_accounts_account_type_known",
        ),
        (
            "unsupported currency",
            "INSERT INTO accounts (name, account_type, currency) "
            "VALUES ('x', 'asset', 'EUR')",
            "ck_accounts_currency_supported",
        ),
        (
            "blank account name",
            "INSERT INTO accounts (name, account_type) VALUES ('   ', 'asset')",
            "ck_accounts_name_not_blank",
        ),
    ],
)
def test_account_constraints_reject_bad_rows(
    session: Session, label: str, sql: str, expected_constraint: str
) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        session.execute(text(sql))
        session.commit()
    assert expected_constraint in str(excinfo.value)


@pytest.mark.parametrize(
    ("label", "amount", "same_account", "expected_constraint"),
    [
        ("zero amount", 0, False, "ck_transfers_amount_positive"),
        ("negative amount", -100, False, "ck_transfers_amount_positive"),
        ("self transfer", 100, True, "ck_transfers_distinct_accounts"),
    ],
)
def test_transfer_constraints_reject_bad_rows(
    session: Session,
    accounts: dict[str, uuid.UUID],
    label: str,
    amount: int,
    same_account: bool,
    expected_constraint: str,
) -> None:
    src = accounts["house:float"]
    dst = src if same_account else accounts["wallet:alice"]

    with pytest.raises(IntegrityError) as excinfo:
        session.execute(
            text(
                "INSERT INTO transfers (source_account_id, destination_account_id, amount) "
                "VALUES (:src, :dst, :amount)"
            ),
            {"src": src, "dst": dst, "amount": amount},
        )
        session.commit()
    assert expected_constraint in str(excinfo.value)


def test_entry_amount_must_be_positive(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """Direction carries the sign; a negative amount is always a bug."""
    transfer_id = session.execute(
        text(
            "INSERT INTO transfers (source_account_id, destination_account_id, amount) "
            "VALUES (:src, :dst, 100) RETURNING id"
        ),
        {"src": accounts["house:float"], "dst": accounts["wallet:alice"]},
    ).scalar_one()

    with pytest.raises(IntegrityError) as excinfo:
        session.execute(
            text(
                "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
                "VALUES (:tid, :acct, 'credit', -100)"
            ),
            {"tid": transfer_id, "acct": accounts["wallet:alice"]},
        )
        session.commit()
    assert "ck_ledger_entries_amount_positive" in str(excinfo.value)


# --- Deferred balancing trigger ----------------------------------------------


def test_balanced_transfer_commits(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    transfer_id = post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    net = session.execute(
        text("SELECT SUM(signed_amount) FROM ledger_entries WHERE transfer_id = :t"),
        {"t": transfer_id},
    ).scalar_one()
    assert net == 0


def test_one_sided_posting_is_rejected_at_commit(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """Money appearing from nowhere: a credit with no matching debit."""
    transfer_id = session.execute(
        text(
            "INSERT INTO transfers (source_account_id, destination_account_id, amount) "
            "VALUES (:src, :dst, 5000) RETURNING id"
        ),
        {"src": accounts["house:float"], "dst": accounts["wallet:alice"]},
    ).scalar_one()

    session.execute(
        text(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
            "VALUES (:tid, :acct, 'credit', 5000)"
        ),
        {"tid": transfer_id, "acct": accounts["wallet:alice"]},
    )

    # The INSERT itself succeeds. The trigger is DEFERRABLE INITIALLY DEFERRED,
    # so the violation surfaces at COMMIT -- which is exactly what lets a
    # two-statement transfer be legal in between.
    with pytest.raises(IntegrityError) as excinfo:
        session.commit()
    assert "double-entry requires at least 2" in str(excinfo.value)


def test_unbalanced_legs_are_rejected_at_commit(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    transfer_id = session.execute(
        text(
            "INSERT INTO transfers (source_account_id, destination_account_id, amount) "
            "VALUES (:src, :dst, 500) RETURNING id"
        ),
        {"src": accounts["house:float"], "dst": accounts["wallet:alice"]},
    ).scalar_one()

    session.execute(
        text(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
            "VALUES (:tid, :debit_account, 'debit', 500), "
            "       (:tid, :credit_account, 'credit', 400)"
        ),
        {
            "tid": transfer_id,
            "debit_account": accounts["house:float"],
            "credit_account": accounts["wallet:alice"],
        },
    )

    with pytest.raises(IntegrityError) as excinfo:
        session.commit()
    assert "does not balance" in str(excinfo.value)


# --- Append-only enforcement -------------------------------------------------


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("update an entry", "UPDATE ledger_entries SET amount = 1 WHERE id = :id"),
        ("delete an entry", "DELETE FROM ledger_entries WHERE id = :id"),
    ],
)
def test_ledger_entries_are_append_only(
    session: Session, accounts: dict[str, uuid.UUID], label: str, sql: str
) -> None:
    post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    entry_id = session.execute(text("SELECT min(id) FROM ledger_entries")).scalar_one()

    with pytest.raises(IntegrityError) as excinfo:
        session.execute(text(sql), {"id": entry_id})
        session.commit()
    assert "ledger_entries is append-only" in str(excinfo.value)


def test_transfers_cannot_be_deleted(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    transfer_id = post_raw_transfer(
        session,
        debit_account_id=accounts["house:float"],
        credit_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    with pytest.raises(IntegrityError) as excinfo:
        session.execute(text("DELETE FROM transfers WHERE id = :id"), {"id": transfer_id})
        session.commit()
    assert "transfers is append-only" in str(excinfo.value)


# --- Referential integrity ---------------------------------------------------


def test_entry_cannot_reference_a_missing_transfer(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        session.execute(
            text(
                "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
                "VALUES (:tid, :acct, 'credit', 100)"
            ),
            {"tid": uuid.uuid4(), "acct": accounts["wallet:alice"]},
        )
        session.commit()
    assert "fk_ledger_entries_transfer_id" in str(excinfo.value)
