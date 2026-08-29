"""Posting a transfer: the write path.

Everything here runs inside a transaction the caller opened and the caller
commits. Nothing in this module commits, because the idempotency claim and the
postings have to land or fail together -- see app/idempotency.py.

Two things in here are worth reading carefully, and both are commented at the
point they happen:

  * accounts are locked one at a time, in a deterministic order, to make
    deadlock structurally impossible rather than merely unlikely;
  * the balance cache is updated from the rows that were just appended to the
    log, in the same statement that appends them, so it can never be fed a
    number the application invented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import AccountNotFound, InsufficientFunds, InvalidTransfer
from app.ledger import AccountTotals, balance_of


@dataclass(frozen=True)
class LockedAccount:
    """An account row held under FOR UPDATE, with its cached totals."""

    id: uuid.UUID
    name: str
    normal_balance: str
    allow_negative_balance: bool
    totals: AccountTotals

    @property
    def balance(self) -> int:
        return balance_of(self.normal_balance, self.totals)


@dataclass(frozen=True)
class PostedTransfer:
    id: uuid.UUID
    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID
    amount: int
    currency: str
    description: str | None
    created_at: datetime


# Locks exactly one account row and reads its cached totals in the same
# round trip. FOR UPDATE OF a locks only the accounts row; the balance row is
# on the nullable side of an outer join and cannot be locked there anyway.
_LOCK_ACCOUNT = text(
    """
    SELECT a.id,
           a.name,
           a.normal_balance,
           a.allow_negative_balance,
           COALESCE(b.posted_debits, 0)  AS posted_debits,
           COALESCE(b.posted_credits, 0) AS posted_credits,
           COALESCE(b.entry_count, 0)    AS entry_count,
           b.last_entry_id
    FROM accounts a
    LEFT JOIN account_balances b ON b.account_id = a.id
    WHERE a.id = :account_id
    FOR UPDATE OF a
    """
)


def _lock_accounts(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, LockedAccount]:
    """Lock every account involved, in ascending id order.

    WHY ONE STATEMENT PER ACCOUNT, SORTED:

    Two concurrent transfers in opposite directions -- Alice to Bob, and Bob to
    Alice -- will each grab one account and then wait forever for the other.
    That is a textbook deadlock. Postgres would detect it and kill one
    transaction, so no money is corrupted, but a money API that randomly
    returns "deadlock detected" under load is not a working money API.

    Sorting the ids first means every transaction in the system reaches for the
    lowest-numbered account first. Two contending transactions therefore
    contend on the *same* row first, so one simply waits for the other. A cycle
    can never form, so the deadlock cannot happen rather than being retried
    after the fact.

    The locks are taken with separate statements rather than one
    `WHERE id = ANY(...) ORDER BY id FOR UPDATE`, because ORDER BY does not
    guarantee lock acquisition order -- the planner is free to lock rows as it
    finds them and sort afterwards. Explicit sequential statements are the only
    way to actually control the order, and two extra round trips is a cheap
    price for an invariant instead of a hope.
    """
    locked: dict[uuid.UUID, LockedAccount] = {}

    for account_id in sorted(set(account_ids), key=str):
        row = session.execute(_LOCK_ACCOUNT, {"account_id": account_id}).one_or_none()
        if row is None:
            raise AccountNotFound(account_id)
        locked[row.id] = LockedAccount(
            id=row.id,
            name=row.name,
            normal_balance=row.normal_balance,
            allow_negative_balance=row.allow_negative_balance,
            totals=AccountTotals(
                posted_debits=row.posted_debits,
                posted_credits=row.posted_credits,
                entry_count=row.entry_count,
                last_entry_id=row.last_entry_id,
            ),
        )
    return locked


def _assert_can_absorb(account: LockedAccount, *, debit: int, credit: int) -> None:
    """Reject a posting that would drive a restricted account negative.

    Checked for BOTH sides of the transfer, not just the debited one. Crediting
    a credit-normal wallet always increases it, but crediting a *debit-normal*
    account (house cash) decreases it -- so "the destination can't overdraw" is
    only true for one of the two account shapes. Applying the same rule to both
    sides removes the special case entirely.
    """
    if account.allow_negative_balance:
        return

    projected = AccountTotals(
        posted_debits=account.totals.posted_debits + debit,
        posted_credits=account.totals.posted_credits + credit,
    )
    projected_balance = balance_of(account.normal_balance, projected)

    if projected_balance < 0:
        raise InsufficientFunds(
            account_name=account.name,
            balance=account.balance,
            requested=debit or credit,
            shortfall=-projected_balance,
        )


# One statement that appends the postings AND folds them into the cache.
#
# The cache deltas are computed by the database from `new_entries` -- the rows
# this very statement just wrote -- rather than from values Python passed in.
# There is therefore no code path that can credit the cache with an amount that
# is not in the log. The cache can still be made wrong by tampering with the
# log afterwards, which is exactly what Phase 3 reconciliation looks for.
_POST_ENTRIES = text(
    """
    WITH new_entries AS (
        INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
        VALUES (:transfer_id, :debit_account_id,  'debit',  :amount),
               (:transfer_id, :credit_account_id, 'credit', :amount)
        RETURNING id, account_id, direction, amount
    ),
    deltas AS (
        SELECT account_id,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)  AS posted_debits,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0) AS posted_credits,
               COUNT(*)  AS entry_count,
               MAX(id)   AS last_entry_id
        FROM new_entries
        GROUP BY account_id
    )
    INSERT INTO account_balances AS b
        (account_id, posted_debits, posted_credits, entry_count, last_entry_id, updated_at)
    SELECT account_id, posted_debits, posted_credits, entry_count, last_entry_id, now()
    FROM deltas
    ON CONFLICT (account_id) DO UPDATE SET
        posted_debits  = b.posted_debits  + EXCLUDED.posted_debits,
        posted_credits = b.posted_credits + EXCLUDED.posted_credits,
        entry_count    = b.entry_count    + EXCLUDED.entry_count,
        -- GREATEST, not plain assignment: the high-water mark must never go
        -- backwards if statements ever land out of order.
        last_entry_id  = GREATEST(COALESCE(b.last_entry_id, 0), EXCLUDED.last_entry_id),
        updated_at     = now()
    RETURNING b.account_id, b.posted_debits, b.posted_credits, b.entry_count
    """
)


def post_transfer(
    session: Session,
    *,
    source_account_id: uuid.UUID,
    destination_account_id: uuid.UUID,
    amount: int,
    description: str | None = None,
) -> PostedTransfer:
    """Move `amount` minor units from source to destination.

    The source account is DEBITED and the destination is CREDITED. What that
    does to each balance depends on the account's normal direction -- see
    docs/schema.md.

    Does not commit. The caller owns the transaction, because the idempotency
    claim in app/idempotency.py has to commit or roll back together with these
    postings.
    """
    if amount <= 0:
        raise InvalidTransfer(f"amount must be positive, got {amount}")
    if source_account_id == destination_account_id:
        raise InvalidTransfer("source and destination must be different accounts")

    accounts = _lock_accounts(session, [source_account_id, destination_account_id])
    source = accounts[source_account_id]
    destination = accounts[destination_account_id]

    # Checked after the locks are held, never before. A balance read outside the
    # lock is a time-of-check-to-time-of-use bug: two concurrent transfers would
    # each see enough funds and both proceed, overdrawing the account.
    _assert_can_absorb(source, debit=amount, credit=0)
    _assert_can_absorb(destination, debit=0, credit=amount)

    row = session.execute(
        text(
            """
            INSERT INTO transfers
                (source_account_id, destination_account_id, amount, description)
            VALUES (:source_account_id, :destination_account_id, :amount, :description)
            RETURNING id, source_account_id, destination_account_id,
                      amount, currency, description, created_at
            """
        ),
        {
            "source_account_id": source_account_id,
            "destination_account_id": destination_account_id,
            "amount": amount,
            "description": description,
        },
    ).one()

    session.execute(
        _POST_ENTRIES,
        {
            "transfer_id": row.id,
            "debit_account_id": source.id,
            "credit_account_id": destination.id,
            "amount": amount,
        },
    )

    return PostedTransfer(
        id=row.id,
        source_account_id=row.source_account_id,
        destination_account_id=row.destination_account_id,
        amount=row.amount,
        currency=row.currency,
        description=row.description,
        created_at=row.created_at,
    )
