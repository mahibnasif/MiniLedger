"""Idempotency at the service layer.

Concurrency is covered separately in test_concurrent_transfers.py. These tests
pin down the sequential semantics: what a replay returns, what counts as a
conflict, and what happens to a key whose request failed.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import IdempotencyKeyConflict, InsufficientFunds
from app.idempotency import canonical_request_hash, claim, complete
from app.ledger import derive_balance
from app.transfers import TRANSFERS_ENDPOINT, execute_transfer


# --- Hashing -----------------------------------------------------------------


def test_hash_ignores_key_order() -> None:
    """A retry that serialises its fields differently is the same request."""
    assert canonical_request_hash({"a": 1, "b": 2}) == canonical_request_hash(
        {"b": 2, "a": 1}
    )


def test_hash_changes_with_content() -> None:
    assert canonical_request_hash({"amount": 100}) != canonical_request_hash(
        {"amount": 101}
    )


def test_hash_is_sha256_hex() -> None:
    """The column has a CHECK on length 64; this keeps them in agreement."""
    digest = canonical_request_hash({"amount": 100})
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


# --- Claiming ----------------------------------------------------------------


def test_first_claim_is_new(session: Session) -> None:
    result = claim(
        session,
        endpoint=TRANSFERS_ENDPOINT,
        idempotency_key="k-1",
        request_hash="a" * 64,
    )
    assert result.is_new is True
    assert result.transfer_id is None
    assert result.response_body is None


def test_claim_of_a_completed_key_is_not_new(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    body, was_replay = execute_transfer(
        session,
        idempotency_key="k-1",
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    session.commit()
    assert was_replay is False

    again = claim(
        session,
        endpoint=TRANSFERS_ENDPOINT,
        idempotency_key="k-1",
        request_hash="a" * 64,
    )
    assert again.is_new is False
    assert str(again.transfer_id) == body["id"]


def test_keys_are_scoped_per_endpoint(session: Session) -> None:
    """The same key string on a different route is not a cache hit."""
    first = claim(
        session, endpoint="POST /transfers", idempotency_key="k", request_hash="a" * 64
    )
    second = claim(
        session, endpoint="POST /payouts", idempotency_key="k", request_hash="a" * 64
    )
    assert first.is_new is True
    assert second.is_new is True


def test_claim_does_not_overwrite_the_original_hash(session: Session) -> None:
    """The no-op UPDATE must preserve the FIRST request's hash.

    If it wrote EXCLUDED.request_hash instead, the evidence that two requests
    differed would be destroyed and every conflicting retry would look like a
    valid replay.
    """
    original_hash = "a" * 64
    claim(
        session,
        endpoint=TRANSFERS_ENDPOINT,
        idempotency_key="k-1",
        request_hash=original_hash,
    )
    session.commit()

    result = claim(
        session,
        endpoint=TRANSFERS_ENDPOINT,
        idempotency_key="k-1",
        request_hash="b" * 64,
    )
    assert result.stored_request_hash == original_hash


# --- Replay ------------------------------------------------------------------


def test_retry_replays_instead_of_creating_a_second_transfer(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    kwargs = {
        "source_account_id": accounts["house:float"],
        "destination_account_id": accounts["wallet:alice"],
        "amount": 5000,
        "description": "opening",
    }

    first, replayed_first = execute_transfer(session, idempotency_key="k-1", **kwargs)
    session.commit()

    second, replayed_second = execute_transfer(session, idempotency_key="k-1", **kwargs)
    session.commit()

    assert replayed_first is False
    assert replayed_second is True
    assert first == second

    transfer_count = session.execute(text("SELECT count(*) FROM transfers")).scalar_one()
    assert transfer_count == 1
    assert derive_balance(session, accounts["wallet:alice"]) == 5000


def test_different_keys_create_different_transfers(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    kwargs = {
        "source_account_id": accounts["house:float"],
        "destination_account_id": accounts["wallet:alice"],
        "amount": 5000,
    }
    execute_transfer(session, idempotency_key="k-1", **kwargs)
    session.commit()
    execute_transfer(session, idempotency_key="k-2", **kwargs)
    session.commit()

    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 2
    assert derive_balance(session, accounts["wallet:alice"]) == 10_000


def test_same_key_different_payload_is_a_conflict(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    execute_transfer(
        session,
        idempotency_key="k-1",
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    session.commit()

    with pytest.raises(IdempotencyKeyConflict):
        execute_transfer(
            session,
            idempotency_key="k-1",
            source_account_id=accounts["house:float"],
            destination_account_id=accounts["wallet:alice"],
            amount=9999,  # different amount, same key
        )
    session.rollback()

    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", 6000),
        ("description", "something else"),
    ],
)
def test_any_payload_field_changing_is_a_conflict(
    session: Session, accounts: dict[str, uuid.UUID], field: str, value: object
) -> None:
    kwargs: dict = {
        "source_account_id": accounts["house:float"],
        "destination_account_id": accounts["wallet:alice"],
        "amount": 5000,
        "description": "original",
    }
    execute_transfer(session, idempotency_key="k-1", **kwargs)
    session.commit()

    with pytest.raises(IdempotencyKeyConflict):
        execute_transfer(session, idempotency_key="k-1", **{**kwargs, field: value})
    session.rollback()


# --- Failed requests ---------------------------------------------------------


def test_a_failed_request_releases_its_key(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """The guarantee is "at most one TRANSFER per key", not one attempt.

    Nothing moved, so there is nothing to protect. The claim rolls back with
    the failed transfer, and the key works again once the problem is fixed.
    """
    with pytest.raises(InsufficientFunds):
        execute_transfer(
            session,
            idempotency_key="k-1",
            source_account_id=accounts["wallet:alice"],  # empty
            destination_account_id=accounts["wallet:bob"],
            amount=5000,
        )
    session.rollback()

    # The claim row went with it.
    assert (
        session.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 0
    )

    # Fund Alice, then reuse the same key successfully.
    execute_transfer(
        session,
        idempotency_key="funding",
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=10_000,
    )
    session.commit()

    _body, was_replay = execute_transfer(
        session,
        idempotency_key="k-1",
        source_account_id=accounts["wallet:alice"],
        destination_account_id=accounts["wallet:bob"],
        amount=5000,
    )
    session.commit()

    assert was_replay is False
    assert derive_balance(session, accounts["wallet:bob"]) == 5000


def test_completion_columns_are_written_together(
    session: Session, accounts: dict[str, uuid.UUID]
) -> None:
    """The all-or-nothing CHECK should never be the thing that catches a bug,
    but it must actually be enforceable."""
    execute_transfer(
        session,
        idempotency_key="k-1",
        source_account_id=accounts["house:float"],
        destination_account_id=accounts["wallet:alice"],
        amount=5000,
    )
    session.commit()

    row = session.execute(
        text(
            "SELECT transfer_id, response_status, response_body, completed_at "
            "FROM idempotency_keys WHERE idempotency_key = 'k-1'"
        )
    ).one()
    assert row.transfer_id is not None
    assert row.response_status == 201
    assert row.response_body is not None
    assert row.completed_at is not None


def test_half_completed_rows_are_rejected_by_the_database(session: Session) -> None:
    complete_args = claim(
        session,
        endpoint=TRANSFERS_ENDPOINT,
        idempotency_key="k-1",
        request_hash="a" * 64,
    )
    assert complete_args.is_new

    with pytest.raises(Exception) as excinfo:
        session.execute(
            text(
                "UPDATE idempotency_keys SET response_status = 201 "
                "WHERE idempotency_key = 'k-1'"
            )
        )
        session.commit()
    assert "completion_is_all_or_nothing" in str(excinfo.value)
