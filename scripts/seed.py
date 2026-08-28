"""Seed the chart of accounts.

Run from the project root:

    python -m scripts.seed

Re-runnable. Accounts are keyed by their unique `name` and inserted with
ON CONFLICT DO NOTHING, so running this twice does not create duplicates and
does not disturb balances that already exist.

A note on direction, because it is the one thing that is easy to get backwards:

    **The source account is DEBITED. The destination account is CREDITED.**

What that does to a balance depends on the account's normal direction. Debiting
a credit-normal account (a customer wallet) reduces it -- money leaving.
Debiting a debit-normal account (house cash) increases it. That is not a
special case, it is what debit and credit mean; `balance_of` in app/ledger.py
is the only code that needs to know.

The float account exists so that seeded demo money has an honest origin inside
the ledger. Drawing $50 into a wallet debits `house:float`, which drives that
account negative -- correctly, because we have handed out money we did not take
in. Real deposits would arrive against a bank-settlement account instead; see
"What I deliberately left out" in the README.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import session_scope

# (name, account_type, allow_negative_balance, purpose)
CHART_OF_ACCOUNTS: list[tuple[str, str, bool, str]] = [
    (
        "house:float",
        "equity",
        True,
        "Opening float the demo draws down to fund wallets.",
    ),
    (
        "wallet:alice",
        "liability",
        False,
        "Customer wallet. Money we hold on Alice's behalf.",
    ),
    (
        "wallet:bob",
        "liability",
        False,
        "Customer wallet. Money we hold on Bob's behalf.",
    ),
]


def seed_accounts() -> None:
    with session_scope() as session:
        for name, account_type, allow_negative, _purpose in CHART_OF_ACCOUNTS:
            session.execute(
                text(
                    """
                    INSERT INTO accounts (name, account_type, allow_negative_balance)
                    VALUES (:name, :account_type, :allow_negative)
                    ON CONFLICT (name) DO NOTHING
                    """
                ),
                {
                    "name": name,
                    "account_type": account_type,
                    "allow_negative": allow_negative,
                },
            )

            # Give every account a balance row up front so "one balance row per
            # account" is an invariant reconciliation can assert, rather than a
            # thing that happens to be true once money has moved.
            session.execute(
                text(
                    """
                    INSERT INTO account_balances (account_id)
                    SELECT id FROM accounts WHERE name = :name
                    ON CONFLICT (account_id) DO NOTHING
                    """
                ),
                {"name": name},
            )


def print_chart() -> None:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT a.name, a.account_type, a.normal_balance,
                       a.allow_negative_balance, b.posted_debits, b.posted_credits
                FROM accounts a
                LEFT JOIN account_balances b ON b.account_id = a.id
                ORDER BY a.name
                """
            )
        ).all()

    header = f"{'account':16} {'type':10} {'normal':7} {'neg?':5} {'debits':>9} {'credits':>9}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.name:16} {r.account_type:10} {r.normal_balance:7} "
            f"{str(r.allow_negative_balance):5} {r.posted_debits:9} {r.posted_credits:9}"
        )


if __name__ == "__main__":
    seed_accounts()
    print_chart()
