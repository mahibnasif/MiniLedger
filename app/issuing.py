"""Card authorisation decisions.

This is the real-time decision point: Stripe holds a card transaction open and
asks whether to let it through. The answer comes from the ledger, and only from
the ledger.

WHAT "APPROVE" MEANS HERE, AND WHAT IT DOES NOT

Approving posts the debit immediately: the wallet is debited and
`house:card_settlement` is credited, through the same `execute_transfer` used
by the HTTP API. There is no separate "pending hold" concept.

That is a simplification, and it is worth being precise about which part is
simplified, because the obvious worry is not the real one.

  NOT a problem: concurrent authorisations double-spending. Posting the debit
  immediately means the second authorisation locks the same account row, reads
  a balance that already reflects the first, and declines. The Phase 2 ordered
  row locks handle this with no new machinery -- there is a test.

  IS a problem: an authorisation that is approved and then never captured, or
  is reversed or expires. The money has already left the wallet and nothing
  puts it back. A production card programme models the authorisation as a hold
  against available balance, settles it on capture, and releases it on expiry.

Building holds properly would mean a transfer whose status changes over time,
and `transfers` is append-only by design -- no UPDATE. The honest version is a
pending hold plus a compensating reversal transfer on expiry, driven by the
`issuing_authorization.updated` and `issuing_transaction.created` events. That
is named in the README's "What I deliberately left out" rather than half-built
here.

WHY THE DECISION IS RECORDED EVEN WHEN IT IS A DECLINE

An approval leaves a transfer behind. A decline leaves nothing, so without
`card_authorizations` a declined card would be completely invisible after the
fact -- and "why was my card refused?" is the question a card programme spends
most of its support time on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import InsufficientFunds, LedgerError
from app.transfers import execute_transfer

# The account card spending settles against. A liability: once a card
# transaction is approved we owe that money onward to the card network, so it
# leaves the customer's wallet but has not yet left the business.
SETTLEMENT_ACCOUNT_NAME = "house:card_settlement"

# Stripe's own decline reason vocabulary, so the values we store line up with
# what the dashboard and the API report rather than being a private dialect.
DECLINE_INSUFFICIENT_FUNDS = "insufficient_funds"
DECLINE_CARD_UNKNOWN = "card_inactive"
DECLINE_WEBHOOK_ERROR = "webhook_error"


@dataclass(frozen=True)
class AuthorizationDecision:
    """What we decided, and why. Returned to Stripe and recorded in the ledger."""

    approved: bool
    reason: str | None = None
    transfer_id: uuid.UUID | None = None
    balance_at_decision: int = 0
    replayed: bool = False

    def as_stripe_response(self) -> dict:
        """The body Stripe expects back on an `issuing_authorization.request`.

        Stripe reads the decision from the webhook response itself, so this
        endpoint is not merely acknowledging an event -- it is answering a
        question while a cardholder stands at a terminal.
        """
        if self.approved:
            return {"approve": True}
        return {"approve": False, "decline_reason": self.reason}


@dataclass(frozen=True)
class AuthorizationRequest:
    """The parts of a Stripe authorisation this decision actually depends on."""

    stripe_authorization_id: str
    stripe_card_id: str
    amount: int
    merchant_name: str | None = None


def _load_card(session: Session, stripe_card_id: str):
    return session.execute(
        text(
            "SELECT c.id, c.account_id, a.name AS account_name "
            "FROM cards c JOIN accounts a ON a.id = c.account_id "
            "WHERE c.stripe_card_id = :stripe_card_id"
        ),
        {"stripe_card_id": stripe_card_id},
    ).one_or_none()


def _existing_decision(session: Session, stripe_authorization_id: str):
    return session.execute(
        text(
            "SELECT decision, decline_reason, transfer_id, balance_at_decision "
            "FROM card_authorizations WHERE stripe_authorization_id = :id"
        ),
        {"id": stripe_authorization_id},
    ).one_or_none()


def _settlement_account_id(session: Session) -> uuid.UUID | None:
    return session.execute(
        text("SELECT id FROM accounts WHERE name = :name"),
        {"name": SETTLEMENT_ACCOUNT_NAME},
    ).scalar_one_or_none()


def _record(
    session: Session,
    request: AuthorizationRequest,
    card_id: uuid.UUID,
    decision: AuthorizationDecision,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO card_authorizations
                (stripe_authorization_id, card_id, amount, merchant_name,
                 decision, decline_reason, transfer_id, balance_at_decision)
            VALUES
                (:auth_id, :card_id, :amount, :merchant_name,
                 :decision, :decline_reason, :transfer_id, :balance)
            """
        ),
        {
            "auth_id": request.stripe_authorization_id,
            "card_id": card_id,
            "amount": request.amount,
            "merchant_name": request.merchant_name,
            "decision": "approved" if decision.approved else "declined",
            "decline_reason": None if decision.approved else decision.reason,
            "transfer_id": decision.transfer_id,
            "balance": decision.balance_at_decision,
        },
    )


def decide(session: Session, request: AuthorizationRequest) -> AuthorizationDecision:
    """Approve or decline `request` against the ledger. Does not commit.

    The caller owns the transaction so that the decision record, the transfer
    and the postings all land together or not at all. A decision recorded
    without its transfer -- or a transfer without its decision -- would be a
    lie in the audit trail.
    """
    # Stripe retries webhook deliveries. A retry must replay the ORIGINAL
    # decision: re-deciding would evaluate against a balance that has moved,
    # and could approve a second debit for a card transaction that already
    # happened once.
    previous = _existing_decision(session, request.stripe_authorization_id)
    if previous is not None:
        return AuthorizationDecision(
            approved=previous.decision == "approved",
            reason=previous.decline_reason,
            transfer_id=previous.transfer_id,
            balance_at_decision=previous.balance_at_decision,
            replayed=True,
        )

    card = _load_card(session, request.stripe_card_id)
    if card is None:
        # No mapping means we cannot know whose money this is. Declining is the
        # only safe answer; approving would be spending an unknown balance.
        return AuthorizationDecision(
            approved=False, reason=DECLINE_CARD_UNKNOWN, balance_at_decision=0
        )

    settlement_id = _settlement_account_id(session)
    if settlement_id is None:
        raise LedgerError(
            f"{SETTLEMENT_ACCOUNT_NAME} does not exist; run python -m scripts.seed"
        )

    # The balance is NOT checked here before attempting the transfer. That
    # would be the same time-of-check-to-time-of-use race Phase 2 already
    # solved: a balance read outside the row lock is stale by the time it is
    # used. execute_transfer takes the lock, re-reads, and raises
    # InsufficientFunds atomically -- so the decline is derived from the
    # attempt rather than from a guess made before it.
    try:
        body, was_replay = execute_transfer(
            session,
            # Stripe's authorisation id as the idempotency key. It is stable
            # across retries and unique per card transaction, which is exactly
            # what the Phase 2 machinery wants -- no new mechanism needed.
            idempotency_key=f"issuing_authorization:{request.stripe_authorization_id}",
            source_account_id=card.account_id,
            destination_account_id=settlement_id,
            amount=request.amount,
            description=(
                f"card authorisation {request.stripe_authorization_id}"
                + (f" at {request.merchant_name}" if request.merchant_name else "")
            ),
        )
    except InsufficientFunds as exc:
        decision = AuthorizationDecision(
            approved=False,
            reason=DECLINE_INSUFFICIENT_FUNDS,
            balance_at_decision=exc.balance,
        )
        _record(session, request, card.id, decision)
        return decision

    balance_after = session.execute(
        text(
            "SELECT COALESCE(posted_credits - posted_debits, 0) "
            "FROM account_balances WHERE account_id = :a"
        ),
        {"a": card.account_id},
    ).scalar_one_or_none()

    decision = AuthorizationDecision(
        approved=True,
        transfer_id=uuid.UUID(body["id"]),
        # The balance the customer had *before* this authorisation, which is
        # the figure that justifies the approval.
        balance_at_decision=int(balance_after or 0) + request.amount,
        replayed=was_replay,
    )
    _record(session, request, card.id, decision)
    return decision
