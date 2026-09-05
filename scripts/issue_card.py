"""Provision a Stripe Issuing cardholder and virtual test card.

    python -m scripts.issue_card --account wallet:alice

Requires STRIPE_API_KEY in .env and Issuing enabled on the Stripe test account
(Dashboard -> Issuing -> Get started, test mode). See the README.

TEST MODE ONLY. app/config.py refuses to start with anything that is not an
sk_test_ key, so this cannot be pointed at live credentials by accident.

Re-runnable per account: if a card already exists for the account, it is
printed rather than a second one being issued. Cards are not free to create and
an accidental second card silently spending the same wallet is exactly the kind
of thing that is easy to do and annoying to unpick.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.db import session_scope
from app.stripe_client import StripeNotConfigured, get_client

# Stripe requires a billing address on a cardholder. Test mode does not verify
# it, and no real person is involved here, so this is an obvious placeholder
# rather than something that could be mistaken for real KYC data.
PLACEHOLDER_BILLING = {
    "address": {
        "line1": "123 Test Street",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94111",
        "country": "US",
    }
}


def issue(account_name: str) -> int:
    client = get_client()

    with session_scope() as session:
        account = session.execute(
            text("SELECT id, name FROM accounts WHERE name = :name"),
            {"name": account_name},
        ).one_or_none()

        if account is None:
            print(
                f"no account named {account_name!r}. Run python -m scripts.seed, "
                f"or pick one from python -m scripts.seed output.",
                file=sys.stderr,
            )
            return 1

        existing = session.execute(
            text(
                "SELECT stripe_card_id, stripe_cardholder_id, last4 FROM cards "
                "WHERE account_id = :a"
            ),
            {"a": account.id},
        ).one_or_none()

        if existing is not None:
            print(f"{account_name} already has a card:")
            print(f"  card       {existing.stripe_card_id}")
            print(f"  cardholder {existing.stripe_cardholder_id}")
            print(f"  last4      {existing.last4}")
            return 0

        cardholder = client.v1.issuing.cardholders.create(
            {
                "name": account_name,
                "email": "cardholder@example.com",
                "phone_number": "+15555550123",
                "status": "active",
                "type": "individual",
                "billing": PLACEHOLDER_BILLING,
            }
        )

        card = client.v1.issuing.cards.create(
            {
                "cardholder": cardholder.id,
                "currency": "usd",
                "type": "virtual",
                # Virtual cards can be activated immediately; a physical card
                # would ship inactive and need activating on receipt.
                "status": "active",
            }
        )

        session.execute(
            text(
                """
                INSERT INTO cards
                    (stripe_card_id, stripe_cardholder_id, account_id, last4)
                VALUES (:card_id, :cardholder_id, :account_id, :last4)
                """
            ),
            {
                "card_id": card.id,
                "cardholder_id": cardholder.id,
                "account_id": account.id,
                "last4": card.last4,
            },
        )

    print(f"issued a virtual test card for {account_name}")
    print(f"  cardholder {cardholder.id}")
    print(f"  card       {card.id}")
    print(f"  last4      {card.last4}")
    print()
    print("Simulate an authorisation against it with:")
    print(f"  stripe testhelpers issuing authorizations create \\")
    print(f"      --card {card.id} --amount 2500")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.issue_card",
        description="Create a Stripe Issuing cardholder and virtual test card.",
    )
    parser.add_argument(
        "--account",
        default="wallet:alice",
        help="ledger account the card spends from (default: wallet:alice)",
    )
    args = parser.parse_args(argv)

    try:
        return issue(args.account)
    except StripeNotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
