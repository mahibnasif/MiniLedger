"""Deliberately corrupt the ledger, so reconciliation can be seen catching it.

    python -m scripts.break_ledger --scenario tampered-entry
    python -m scripts.break_ledger --scenario cache-drift
    python -m scripts.break_ledger --scenario double-transfer

Then run `python -m scripts.reconcile` and watch it fail.

Three scenarios, because they break the ledger in genuinely different ways and
are caught by genuinely different checks. A single scenario would make the
reconciliation job look like it only does one thing.

    tampered-entry    Someone edited the entry log itself. Caught three ways
                      at once: the whole ledger stops summing to zero, that
                      transfer stops balancing, and the account drifts.

    cache-drift       Someone ran a bad UPDATE against account_balances. The
                      log is perfect. Caught only by comparing the two.

    double-transfer   A duplicate payment written straight to the database,
                      bypassing idempotency. Balanced entries, correct cache,
                      ZERO drift. No balance comparison will ever find it --
                      it is caught purely as a provenance gap.

That last one is the interesting case, and the reason the reconciliation job
checks more than balances.

RESETTING: this script only ever makes things worse. To get back to a clean
ledger, rebuild it:

    docker compose down -v && docker compose up -d
    alembic upgrade head && python -m scripts.seed
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import session_scope

TAMPER_AMOUNT = 5_000


def tampered_entry(session: Session) -> str:
    """Edit a committed ledger entry, inflating a customer's credit.

    The append-only trigger from migration 0002 refuses UPDATE on
    ledger_entries, so this has to disable it first -- which is the point worth
    noticing. Corrupting the log is not a stray UPDATE somebody fat-fingers; it
    requires table-owner rights and a deliberate act to switch the protection
    off. In production the application role would not be able to do this at
    all.

    ALTER TABLE ... DISABLE TRIGGER USER turns off both user triggers on the
    table, including the deferred balancing check, which is why an unbalanced
    row can be written at all.
    """
    row = session.execute(
        text(
            """
            SELECT e.id, e.amount, a.name
            FROM ledger_entries e
            JOIN accounts a ON a.id = e.account_id
            WHERE e.direction = 'credit' AND a.name LIKE 'wallet:%'
            ORDER BY e.id
            LIMIT 1
            """
        )
    ).one_or_none()

    if row is None:
        raise SystemExit("no wallet credit entries found -- run scripts.seed first")

    session.execute(text("ALTER TABLE ledger_entries DISABLE TRIGGER USER"))
    try:
        session.execute(
            text("UPDATE ledger_entries SET amount = amount + :bump WHERE id = :id"),
            {"bump": TAMPER_AMOUNT, "id": row.id},
        )
    finally:
        # Re-enabled even if the UPDATE fails, so a half-run of this script
        # cannot leave the ledger permanently unprotected.
        session.execute(text("ALTER TABLE ledger_entries ENABLE TRIGGER USER"))

    return (
        f"inflated entry #{row.id} ({row.name}) from {row.amount} to "
        f"{row.amount + TAMPER_AMOUNT} with the append-only trigger disabled"
    )


def cache_drift(session: Session) -> str:
    """Corrupt account_balances directly. The entry log stays perfect.

    No trigger to defeat: the cache is ordinary mutable state, which is exactly
    why it cannot be trusted without an auditor.
    """
    row = session.execute(
        text(
            """
            SELECT b.account_id, a.name, b.posted_credits
            FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.name LIKE 'wallet:%'
            ORDER BY a.name
            LIMIT 1
            """
        )
    ).one_or_none()

    if row is None:
        raise SystemExit("no wallet balance rows found -- run scripts.seed first")

    session.execute(
        text(
            "UPDATE account_balances SET posted_credits = posted_credits + :bump "
            "WHERE account_id = :account_id"
        ),
        {"bump": TAMPER_AMOUNT, "account_id": row.account_id},
    )

    return (
        f"inflated {row.name} cached posted_credits from {row.posted_credits} "
        f"to {row.posted_credits + TAMPER_AMOUNT}; the entry log is untouched"
    )


def double_transfer(session: Session) -> str:
    """Duplicate a payment, bypassing the idempotency layer entirely.

    Writes a complete, correct, balanced transfer straight to the database --
    entries that sum to zero, cache folded in properly. The resulting ledger is
    internally flawless. Every balance check passes.

    The customer has still been charged twice.

    This is why reconciliation cannot only compare balances. The only trace
    left is that the transfer has no idempotency key, because it never went
    through the path that would have claimed one.
    """
    original = session.execute(
        text(
            """
            SELECT t.id, t.source_account_id, t.destination_account_id,
                   t.amount, t.description
            FROM transfers t
            JOIN accounts dst ON dst.id = t.destination_account_id
            WHERE dst.name LIKE 'wallet:%'
            ORDER BY t.created_at
            LIMIT 1
            """
        )
    ).one_or_none()

    if original is None:
        raise SystemExit("no transfers found -- run scripts.seed first")

    duplicate_id = session.execute(
        text(
            """
            INSERT INTO transfers
                (source_account_id, destination_account_id, amount, description)
            VALUES (:src, :dst, :amount, :description)
            RETURNING id
            """
        ),
        {
            "src": original.source_account_id,
            "dst": original.destination_account_id,
            "amount": original.amount,
            "description": original.description,
        },
    ).scalar_one()

    # Post it properly, cache and all, so that nothing looks wrong afterwards.
    session.execute(
        text(
            """
            WITH new_entries AS (
                INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
                VALUES (:tid, :src, 'debit', :amount),
                       (:tid, :dst, 'credit', :amount)
                RETURNING id, account_id, direction, amount
            ),
            deltas AS (
                SELECT account_id,
                       COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0)  AS d,
                       COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0) AS c,
                       COUNT(*) AS n,
                       MAX(id)  AS last_id
                FROM new_entries GROUP BY account_id
            )
            INSERT INTO account_balances AS b
                (account_id, posted_debits, posted_credits, entry_count,
                 last_entry_id, updated_at)
            SELECT account_id, d, c, n, last_id, now() FROM deltas
            ON CONFLICT (account_id) DO UPDATE SET
                posted_debits  = b.posted_debits  + EXCLUDED.posted_debits,
                posted_credits = b.posted_credits + EXCLUDED.posted_credits,
                entry_count    = b.entry_count    + EXCLUDED.entry_count,
                last_entry_id  = GREATEST(COALESCE(b.last_entry_id, 0),
                                          EXCLUDED.last_entry_id),
                updated_at     = now()
            """
        ),
        {
            "tid": duplicate_id,
            "src": original.source_account_id,
            "dst": original.destination_account_id,
            "amount": original.amount,
        },
    )

    return (
        f"duplicated transfer {original.id} as {duplicate_id} "
        f"({original.amount} minor units) with no idempotency key; "
        f"the ledger remains internally consistent"
    )


SCENARIOS = {
    "tampered-entry": tampered_entry,
    "cache-drift": cache_drift,
    "double-transfer": double_transfer,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.break_ledger",
        description="Corrupt the ledger on purpose, to demonstrate reconciliation.",
    )
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    args = parser.parse_args(argv)

    with session_scope() as session:
        summary = SCENARIOS[args.scenario](session)

    print(f"BROKE THE LEDGER [{args.scenario}]")
    print(f"  {summary}")
    print()
    print("Now run:  python -m scripts.reconcile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
