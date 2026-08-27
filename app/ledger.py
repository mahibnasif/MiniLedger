"""Balance derivation.

The one rule this module exists to enforce: **a balance is a query over
`ledger_entries`, never a number somebody remembered to keep up to date.**

`account_balances` does hold maintained totals, and the read path in Phase 2
will use them, because summing a log that grows forever gets slower forever.
But that cache is derived state with an independent auditor (Phase 3), not an
authority. Every function here that reads the cache is named `cached_*` so the
distinction is visible at the call site rather than buried in a docstring.

Sign convention, stated once so it never has to be guessed again:

    posted_debits and posted_credits are both non-negative running totals.
    A balance is expressed in the account's own normal direction:

        credit-normal (liability, equity, revenue):  credits - debits
        debit-normal  (asset, expense):              debits - credits

    So a customer wallet holding $50 reports 5000, and the house cash account
    that funded it also reports its own position as a positive number. Nobody
    has to remember which way round an asset account reads -- `balance_of` is
    the only place that knows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Account, AccountBalance, LedgerEntry

CREDIT = "credit"
DEBIT = "debit"


@dataclass(frozen=True)
class AccountTotals:
    """Posted debit and credit totals for one account, in minor units."""

    posted_debits: int = 0
    posted_credits: int = 0
    entry_count: int = 0
    last_entry_id: int | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    """One account's identity plus its totals, however they were obtained.

    Deliberately the same shape whether the totals were derived from the entry
    log or read from the cache, so Phase 3 can compare the two without any
    field-by-field translation in between.
    """

    account_id: uuid.UUID
    name: str
    normal_balance: str
    totals: AccountTotals

    @property
    def balance(self) -> int:
        return balance_of(self.normal_balance, self.totals)


def balance_of(normal_balance: str, totals: AccountTotals) -> int:
    """Turn raw debit/credit totals into a balance in the account's direction.

    The single place in the codebase that knows the debit/credit sign rule.
    """
    if normal_balance == CREDIT:
        return totals.posted_credits - totals.posted_debits
    if normal_balance == DEBIT:
        return totals.posted_debits - totals.posted_credits
    raise ValueError(f"unknown normal_balance {normal_balance!r}")


def format_minor_units(amount: int, currency: str = "USD") -> str:
    """Render cents for humans. Display only -- never feed this back into maths."""
    sign = "-" if amount < 0 else ""
    whole, cents = divmod(abs(amount), 100)
    return f"{sign}{whole}.{cents:02d} {currency}"


# --- Derivation from the entry log (the source of truth) ---------------------

# Both totals come from one pass over the entries: SUM(CASE ...) rather than two
# separate queries, so the debit and credit figures are guaranteed to be read
# from the same snapshot of the table.
_DEBIT_TOTAL = func.coalesce(
    func.sum(case((LedgerEntry.direction == DEBIT, LedgerEntry.amount), else_=0)), 0
)
_CREDIT_TOTAL = func.coalesce(
    func.sum(case((LedgerEntry.direction == CREDIT, LedgerEntry.amount), else_=0)), 0
)


def derive_totals(session: Session, account_id: uuid.UUID) -> AccountTotals:
    """Recompute one account's totals from every entry ever written for it.

    O(entries for this account), served by ix_ledger_entries_account_id_id.
    This is the definition of that account's position; everything else is an
    optimisation that has to agree with it.
    """
    row = session.execute(
        select(
            _DEBIT_TOTAL,
            _CREDIT_TOTAL,
            func.count(LedgerEntry.id),
            func.max(LedgerEntry.id),
        ).where(LedgerEntry.account_id == account_id)
    ).one()
    return AccountTotals(
        posted_debits=row[0],
        posted_credits=row[1],
        entry_count=row[2],
        last_entry_id=row[3],
    )


def derive_balance(session: Session, account_id: uuid.UUID) -> int:
    """One account's balance, computed from the log and nothing else."""
    normal_balance = session.execute(
        select(Account.normal_balance).where(Account.id == account_id)
    ).scalar_one()
    return balance_of(normal_balance, derive_totals(session, account_id))


def derive_all_snapshots(session: Session) -> list[AccountSnapshot]:
    """Recompute every account's totals from the log in a single query.

    LEFT JOIN, not INNER: an account with no entries yet must still appear, with
    zero totals. Dropping it would let reconciliation silently skip exactly the
    accounts most likely to be misconfigured.
    """
    rows = session.execute(
        select(
            Account.id,
            Account.name,
            Account.normal_balance,
            _DEBIT_TOTAL.label("posted_debits"),
            _CREDIT_TOTAL.label("posted_credits"),
            func.count(LedgerEntry.id).label("entry_count"),
            func.max(LedgerEntry.id).label("last_entry_id"),
        )
        .select_from(Account)
        .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
        .group_by(Account.id, Account.name, Account.normal_balance)
        .order_by(Account.name)
    ).all()

    return [
        AccountSnapshot(
            account_id=r.id,
            name=r.name,
            normal_balance=r.normal_balance,
            totals=AccountTotals(
                posted_debits=r.posted_debits,
                posted_credits=r.posted_credits,
                entry_count=r.entry_count,
                last_entry_id=r.last_entry_id,
            ),
        )
        for r in rows
    ]


# --- Reads of the cache (fast, and never trusted without reconciliation) -----


def cached_totals(session: Session, account_id: uuid.UUID) -> AccountTotals:
    """Read one account's cached totals. Absent row means "no entries yet"."""
    row = session.execute(
        select(
            AccountBalance.posted_debits,
            AccountBalance.posted_credits,
            AccountBalance.entry_count,
            AccountBalance.last_entry_id,
        ).where(AccountBalance.account_id == account_id)
    ).one_or_none()

    if row is None:
        return AccountTotals()
    return AccountTotals(
        posted_debits=row[0],
        posted_credits=row[1],
        entry_count=row[2],
        last_entry_id=row[3],
    )


def cached_balance(session: Session, account_id: uuid.UUID) -> int:
    """One account's balance as the system currently reports it."""
    normal_balance = session.execute(
        select(Account.normal_balance).where(Account.id == account_id)
    ).scalar_one()
    return balance_of(normal_balance, cached_totals(session, account_id))


def cached_all_snapshots(session: Session) -> list[AccountSnapshot]:
    """Every account's cached totals, shaped identically to derive_all_snapshots.

    LEFT JOIN again: an account that never received a posting has no row in
    account_balances at all, and must read as zero rather than vanishing.
    """
    rows = session.execute(
        select(
            Account.id,
            Account.name,
            Account.normal_balance,
            func.coalesce(AccountBalance.posted_debits, 0).label("posted_debits"),
            func.coalesce(AccountBalance.posted_credits, 0).label("posted_credits"),
            func.coalesce(AccountBalance.entry_count, 0).label("entry_count"),
            AccountBalance.last_entry_id.label("last_entry_id"),
        )
        .select_from(Account)
        .outerjoin(AccountBalance, AccountBalance.account_id == Account.id)
        .order_by(Account.name)
    ).all()

    return [
        AccountSnapshot(
            account_id=r.id,
            name=r.name,
            normal_balance=r.normal_balance,
            totals=AccountTotals(
                posted_debits=r.posted_debits,
                posted_credits=r.posted_credits,
                entry_count=r.entry_count,
                last_entry_id=r.last_entry_id,
            ),
        )
        for r in rows
    ]
