"""Send a correctly-signed authorisation webhook at a locally running server.

    python -m scripts.simulate_authorization --card ic_demo_alice --amount 2500

This is a LOCAL TEST HARNESS, not a substitute for the real thing. It proves
the decision path and the signature verification, because it builds a genuine
Stripe-format signature and the server verifies it with Stripe's own code. It
does NOT prove anything about Stripe actually calling us -- for that, use the
Stripe CLI, as described in the README.

It exists because Stripe Issuing has to be enabled on the account before test
cards can be created, and that should not stand between someone cloning this
repo and seeing the authorisation flow work.

Use --secret-mismatch to send a signature computed with the wrong key and watch
the endpoint refuse it.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request

from app.config import get_settings


def build_event(card_id: str, amount: int, authorization_id: str, merchant: str) -> bytes:
    """An `issuing_authorization.request` payload shaped like Stripe's."""
    return json.dumps(
        {
            "id": f"evt_sim_{authorization_id}",
            "object": "event",
            "type": "issuing_authorization.request",
            "data": {
                "object": {
                    "id": authorization_id,
                    "object": "issuing.authorization",
                    "card": {"id": card_id, "object": "issuing.card"},
                    "amount": amount,
                    "currency": "usd",
                    "pending_request": {"amount": amount, "currency": "usd"},
                    "merchant_data": {"name": merchant},
                }
            },
        }
    ).encode()


def sign(payload: bytes, secret: str) -> str:
    """Stripe's scheme: HMAC-SHA256 over "<timestamp>.<raw body>".

    Written out rather than imported so the signature this sends is genuinely
    independent of the code that verifies it. A helper shared by both sides can
    agree with itself while both are wrong.
    """
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.simulate_authorization",
        description="POST a signed issuing authorisation to a local MiniLedger.",
    )
    parser.add_argument("--card", default="ic_demo_alice", help="Stripe card id")
    parser.add_argument("--amount", type=int, default=2_500, help="minor units")
    parser.add_argument(
        "--authorization-id",
        default=f"iauth_sim_{int(time.time())}",
        help="reuse one to simulate a Stripe redelivery",
    )
    parser.add_argument("--merchant", default="Blue Bottle Coffee")
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/stripe")
    parser.add_argument(
        "--secret-mismatch",
        action="store_true",
        help="sign with the wrong key, to watch verification reject it",
    )
    args = parser.parse_args(argv)

    secret = get_settings().stripe_webhook_secret
    if not secret or secret == "whsec_replace_me":
        print(
            "STRIPE_WEBHOOK_SECRET is not set in .env.\n"
            "For a purely local run, any value works -- set it to something "
            "like whsec_local_demo_secret and restart the server.",
            file=sys.stderr,
        )
        return 2

    payload = build_event(args.card, args.amount, args.authorization_id, args.merchant)
    signature = sign(payload, "whsec_deliberately_wrong" if args.secret_mismatch else secret)

    request = urllib.request.Request(
        args.url,
        data=payload,
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )

    print(f"-> {args.amount} minor units on {args.card} ({args.authorization_id})")
    if args.secret_mismatch:
        print("   signed with the WRONG secret on purpose")

    try:
        with urllib.request.urlopen(request) as response:
            print(f"<- {response.status} {response.read().decode()}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"<- {exc.code} {exc.read().decode()}")
        # A rejected forgery is the expected outcome, not a script failure.
        return 0 if args.secret_mismatch else 1
    except urllib.error.URLError as exc:
        print(
            f"could not reach {args.url}: {exc.reason}\n"
            f"Start the server first:  uvicorn app.main:app --reload",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
