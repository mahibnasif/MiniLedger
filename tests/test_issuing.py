"""Card authorisation decisions come from the ledger, not from a stub."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.issuing import (
    DECLINE_CARD_UNKNOWN,
    DECLINE_INSUFFICIENT_FUNDS,
    SETTLEMENT_ACCOUNT_NAME,
    AuthorizationRequest,
    decide,
)
from app.ledger import derive_balance
from app.reconciliation import reconcile


def _request(card_id: str, amount: int, auth_id: str = "iauth_1") -> AuthorizationRequest:
    return AuthorizationRequest(
        stripe_authorization_id=auth_id,
        stripe_card_id=card_id,
        amount=amount,
        merchant_name="Test Coffee",
    )


# --- Approval ----------------------------------------------------------------


def test_sufficient_balance_is_approved(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    decision = decide(session, _request(card, 2_500))
    session.commit()

    assert decision.approved is True
    assert decision.reason is None
    assert decision.transfer_id is not None
    # The balance that justified the approval: what Alice had beforehand.
    assert decision.balance_at_decision == 50_000

    assert decision.as_stripe_response() == {"approve": True}


def test_approval_moves_money_out_of_the_wallet(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    decide(session, _request(card, 2_500))
    session.commit()

    assert derive_balance(session, settlement["wallet:alice"]) == 47_500
    assert derive_balance(session, settlement[SETTLEMENT_ACCOUNT_NAME]) == 2_500


def test_approval_records_the_decision(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    decision = decide(session, _request(card, 2_500, auth_id="iauth_abc"))
    session.commit()

    row = session.execute(
        text(
            "SELECT decision, decline_reason, transfer_id, amount, merchant_name, "
            "       balance_at_decision "
            "FROM card_authorizations WHERE stripe_authorization_id = 'iauth_abc'"
        )
    ).one()

    assert row.decision == "approved"
    assert row.decline_reason is None
    assert row.transfer_id == decision.transfer_id
    assert row.amount == 2_500
    assert row.merchant_name == "Test Coffee"
    assert row.balance_at_decision == 50_000


def test_spending_the_exact_balance_is_approved(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    """Off-by-one guard at the decision boundary."""
    decision = decide(session, _request(card, 50_000))
    session.commit()

    assert decision.approved is True
    assert derive_balance(session, settlement["wallet:alice"]) == 0


# --- Decline -----------------------------------------------------------------


def test_insufficient_balance_is_declined(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    decision = decide(session, _request(card, 50_001))
    session.commit()

    assert decision.approved is False
    assert decision.reason == DECLINE_INSUFFICIENT_FUNDS
    assert decision.transfer_id is None
    assert decision.balance_at_decision == 50_000

    assert decision.as_stripe_response() == {
        "approve": False,
        "decline_reason": "insufficient_funds",
    }


def test_decline_moves_no_money_but_is_still_recorded(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    """The reason card_authorizations exists.

    A decline leaves no transfer behind, so without this row there would be no
    record at all that the card was ever presented.
    """
    decide(session, _request(card, 999_999, auth_id="iauth_declined"))
    session.commit()

    assert derive_balance(session, settlement["wallet:alice"]) == 50_000

    row = session.execute(
        text(
            "SELECT decision, decline_reason, transfer_id, balance_at_decision "
            "FROM card_authorizations WHERE stripe_authorization_id = 'iauth_declined'"
        )
    ).one()
    assert row.decision == "declined"
    assert row.decline_reason == DECLINE_INSUFFICIENT_FUNDS
    assert row.transfer_id is None
    assert row.balance_at_decision == 50_000


def test_unknown_card_is_declined(
    session: Session, settlement: dict[str, uuid.UUID]
) -> None:
    """No mapping means we cannot know whose money this is.

    Declining is the only safe answer -- approving would be spending a balance
    we cannot identify.
    """
    decision = decide(session, _request("ic_never_issued", 100))
    session.commit()

    assert decision.approved is False
    assert decision.reason == DECLINE_CARD_UNKNOWN

    # Nothing recorded either: there is no card row to hang the decision off.
    assert (
        session.execute(text("SELECT count(*) FROM card_authorizations")).scalar_one()
        == 0
    )


# --- Sequencing and retries --------------------------------------------------


def test_second_authorization_sees_the_first_one_spent(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    """The decision is against the CURRENT balance, not the opening one.

    This is what makes the no-holds design safe. Approving posts the debit
    immediately, so the next authorisation reads a balance that already
    reflects it and declines when the money is gone.
    """
    first = decide(session, _request(card, 30_000, auth_id="iauth_1"))
    session.commit()
    second = decide(session, _request(card, 30_000, auth_id="iauth_2"))
    session.commit()

    assert first.approved is True
    assert second.approved is False
    assert second.reason == DECLINE_INSUFFICIENT_FUNDS
    assert second.balance_at_decision == 20_000

    assert derive_balance(session, settlement["wallet:alice"]) == 20_000


def test_redelivered_authorization_replays_the_original_decision(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    """Stripe retries deliveries. A retry must not decide again.

    Re-deciding would evaluate against a balance that has moved since, and
    could approve a second debit for a card transaction that already happened.
    """
    first = decide(session, _request(card, 2_500, auth_id="iauth_same"))
    session.commit()
    second = decide(session, _request(card, 2_500, auth_id="iauth_same"))
    session.commit()

    assert first.approved is second.approved is True
    assert first.transfer_id == second.transfer_id
    assert second.replayed is True

    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 3
    assert derive_balance(session, settlement["wallet:alice"]) == 47_500


def test_redelivered_decline_replays_as_a_decline(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    """Even after the wallet has been topped up.

    The card transaction was refused at the terminal; replaying the delivery
    must not retroactively approve it.
    """
    first = decide(session, _request(card, 80_000, auth_id="iauth_poor"))
    session.commit()
    assert first.approved is False

    from app.transfers import execute_transfer

    execute_transfer(
        session,
        idempotency_key="topup",
        source_account_id=settlement["house:float"],
        destination_account_id=settlement["wallet:alice"],
        amount=100_000,
    )
    session.commit()

    second = decide(session, _request(card, 80_000, auth_id="iauth_poor"))
    session.commit()

    assert second.approved is False
    assert second.replayed is True
    assert second.reason == DECLINE_INSUFFICIENT_FUNDS


# --- The ledger stays honest -------------------------------------------------


def test_card_spending_leaves_a_reconcilable_ledger(
    session: Session, settlement: dict[str, uuid.UUID], card: str
) -> None:
    """Card authorisations go through the same posting path as everything else,
    so the Phase 3 auditor must still pass."""
    decide(session, _request(card, 2_500, auth_id="iauth_1"))
    session.commit()
    decide(session, _request(card, 1_000, auth_id="iauth_2"))
    session.commit()
    decide(session, _request(card, 999_999, auth_id="iauth_3"))  # declined
    session.commit()

    report = reconcile(session)
    assert report.ok, [f.summary for f in report.findings]

    assert (
        session.execute(text("SELECT SUM(signed_amount) FROM ledger_entries")).scalar_one()
        == 0
    )
