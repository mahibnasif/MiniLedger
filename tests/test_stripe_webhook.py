"""The Stripe webhook endpoint, including signature verification.

Signatures are generated here BY HAND rather than with a Stripe helper, for two
reasons. It keeps the test honest -- a helper that builds the header with the
same code that verifies it can agree with itself while both are wrong. And it
makes the scheme explicit: HMAC-SHA256 over "<timestamp>.<raw body>", keyed on
the endpoint signing secret.

The forgery and tampering tests are the point of this file. This endpoint is
reachable by anyone who learns its URL and it moves money; the signature is the
only thing standing between a stranger's curl command and a $10,000 debit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from pydantic import ValidationError

from app import webhooks
from app.stripe_client import StripeNotConfigured

TEST_SECRET = "whsec_test_only_never_a_real_secret"


def sign(payload: bytes, secret: str = TEST_SECRET, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header the way Stripe does."""
    ts = int(time.time()) if timestamp is None else timestamp
    signed_payload = f"{ts}.".encode() + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def authorization_event(
    card_id: str = "ic_test_alice",
    amount: int = 2_500,
    auth_id: str = "iauth_test_1",
    event_type: str = "issuing_authorization.request",
) -> bytes:
    """A realistic `issuing_authorization.request` payload, as raw bytes."""
    return json.dumps(
        {
            "id": "evt_test_1",
            "object": "event",
            "type": event_type,
            "data": {
                "object": {
                    "id": auth_id,
                    "object": "issuing.authorization",
                    "card": {"id": card_id, "object": "issuing.card", "last4": "4242"},
                    "amount": amount,
                    "currency": "usd",
                    "pending_request": {"amount": amount, "currency": "usd"},
                    "merchant_data": {"name": "Test Coffee", "category": "coffee_shop"},
                }
            },
        }
    ).encode()


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the endpoint a known signing secret."""
    monkeypatch.setattr(webhooks, "get_webhook_secret", lambda: TEST_SECRET)


def post(client: TestClient, payload: bytes, signature: str | None) -> object:
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["Stripe-Signature"] = signature
    return client.post("/webhooks/stripe", content=payload, headers=headers)


# --- Signature verification: the security boundary ---------------------------


def test_unsigned_request_is_rejected_and_moves_no_money(
    client: TestClient, configured: None, card: str, session: Session
) -> None:
    payload = authorization_event(amount=1_000_000)
    response = post(client, payload, signature=None)

    assert response.status_code == 400
    assert response.json()["error"] == "SignatureVerificationError"
    assert session.execute(text("SELECT count(*) FROM card_authorizations")).scalar_one() == 0


def test_forged_signature_is_rejected_and_moves_no_money(
    client: TestClient, configured: None, card: str, session: Session
) -> None:
    """The scenario that matters: a stranger POSTing a $10,000 authorisation.

    Without the secret they cannot produce a matching HMAC, so the handler
    never runs and no money moves.
    """
    payload = authorization_event(amount=1_000_000)
    forged = sign(payload, secret="whsec_attacker_guess")

    response = post(client, payload, forged)

    assert response.status_code == 400
    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 2
    assert session.execute(text("SELECT count(*) FROM card_authorizations")).scalar_one() == 0


def test_tampered_body_invalidates_a_genuine_signature(
    client: TestClient, configured: None, card: str, session: Session
) -> None:
    """Proves the HMAC covers the body, not just the timestamp.

    Sign a legitimate $25 authorisation, then send a $10,000 one with that same
    signature. Intercepting a real webhook and editing the amount must not work.
    """
    genuine = authorization_event(amount=2_500)
    signature = sign(genuine)

    tampered = authorization_event(amount=1_000_000)
    response = post(client, tampered, signature)

    assert response.status_code == 400
    assert session.execute(text("SELECT count(*) FROM card_authorizations")).scalar_one() == 0


def test_stale_signature_is_rejected(
    client: TestClient, configured: None, card: str
) -> None:
    """Replay protection.

    Without a tolerance window a captured signature stays valid forever, and
    anyone who records one real request can replay it indefinitely -- each
    replay being a genuinely Stripe-signed money movement.
    """
    payload = authorization_event()
    stale = sign(payload, timestamp=int(time.time()) - 3600)

    response = post(client, payload, stale)

    assert response.status_code == 400
    assert response.json()["error"] == "SignatureVerificationError"


def test_malformed_json_with_a_valid_signature_is_rejected(
    client: TestClient, configured: None
) -> None:
    payload = b"{not json at all"
    response = post(client, payload, sign(payload))

    assert response.status_code == 400
    assert response.json()["error"] == "InvalidPayload"


def test_missing_configuration_returns_503_not_500(
    client: TestClient, card: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No signing secret configured. "Not set up" is not the same as "broken".

    The unconfigured state is injected rather than relying on .env happening to
    be empty. It was, once, and this test passed for that reason alone -- then
    a local secret was added for a demo and it started failing. A test whose
    result depends on a gitignored file is worse than no test.
    """

    def unconfigured() -> str:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")

    monkeypatch.setattr(webhooks, "get_webhook_secret", unconfigured)

    payload = authorization_event()
    response = post(client, payload, sign(payload))

    assert response.status_code == 503
    assert response.json()["error"] == "StripeNotConfigured"


@pytest.mark.parametrize("value", [None, "", "whsec_replace_me"])
def test_get_webhook_secret_rejects_unset_values(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """The placeholder from .env.example counts as unset.

    Otherwise a fresh clone would appear configured and then fail every
    signature check with a confusing error instead of a clear one.
    """
    from app import stripe_client

    settings = stripe_client.get_settings()
    monkeypatch.setattr(
        stripe_client,
        "get_settings",
        lambda: settings.model_copy(update={"stripe_webhook_secret": value}),
    )

    with pytest.raises(StripeNotConfigured, match="STRIPE_WEBHOOK_SECRET"):
        stripe_client.get_webhook_secret()


@pytest.mark.parametrize("value", [None, "", "sk_test_replace_me"])
def test_get_client_rejects_unset_api_keys(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    from app import stripe_client

    settings = stripe_client.get_settings()
    monkeypatch.setattr(
        stripe_client,
        "get_settings",
        lambda: settings.model_copy(update={"stripe_api_key": value}),
    )

    with pytest.raises(StripeNotConfigured, match="STRIPE_API_KEY"):
        stripe_client.get_client()


def test_live_stripe_keys_are_refused_at_config_time() -> None:
    """The guard that stops this project ever moving real money.

    There is no authorisation layer, no fraud control, and a webhook handler
    that approves card spend. A live key is treated as a configuration bug and
    killed at boot rather than trusted to be caught in review.
    """
    from app.config import Settings

    with pytest.raises(ValidationError, match="test-mode key"):
        Settings(
            database_url="postgresql+psycopg://x:y@localhost/z",
            stripe_api_key="sk_live_this_would_move_real_money",
        )


# --- Decisions ---------------------------------------------------------------


def test_signed_authorization_within_balance_is_approved(
    client: TestClient, configured: None, card: str, session: Session
) -> None:
    payload = authorization_event(amount=2_500)
    response = post(client, payload, sign(payload))

    assert response.status_code == 200
    assert response.json() == {"approve": True}

    balance = session.execute(
        text(
            "SELECT posted_credits - posted_debits FROM account_balances b "
            "JOIN accounts a ON a.id = b.account_id WHERE a.name = 'wallet:alice'"
        )
    ).scalar_one()
    assert balance == 47_500


def test_signed_authorization_over_balance_is_declined(
    client: TestClient, configured: None, card: str, session: Session
) -> None:
    payload = authorization_event(amount=1_000_000)
    response = post(client, payload, sign(payload))

    assert response.status_code == 200
    assert response.json() == {
        "approve": False,
        "decline_reason": "insufficient_funds",
    }

    balance = session.execute(
        text(
            "SELECT posted_credits - posted_debits FROM account_balances b "
            "JOIN accounts a ON a.id = b.account_id WHERE a.name = 'wallet:alice'"
        )
    ).scalar_one()
    assert balance == 50_000


def test_authorization_for_an_unknown_card_is_declined(
    client: TestClient, configured: None, settlement: dict[str, uuid.UUID]
) -> None:
    payload = authorization_event(card_id="ic_never_issued")
    response = post(client, payload, sign(payload))

    assert response.status_code == 200
    assert response.json() == {"approve": False, "decline_reason": "card_inactive"}


def test_redelivered_webhook_does_not_charge_twice(
    client: TestClient, configured: None, card: str, session: Session
) -> None:
    """Stripe retries. Five deliveries of one authorisation, one debit."""
    payload = authorization_event(amount=2_500, auth_id="iauth_retry")

    responses = [post(client, payload, sign(payload)) for _ in range(5)]

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == {"approve": True} for r in responses)

    assert (
        session.execute(
            text(
                "SELECT count(*) FROM card_authorizations "
                "WHERE stripe_authorization_id = 'iauth_retry'"
            )
        ).scalar_one()
        == 1
    )

    balance = session.execute(
        text(
            "SELECT posted_credits - posted_debits FROM account_balances b "
            "JOIN accounts a ON a.id = b.account_id WHERE a.name = 'wallet:alice'"
        )
    ).scalar_one()
    assert balance == 47_500, "the card was charged more than once"


# --- Events we do not handle -------------------------------------------------


def test_unhandled_event_types_are_acknowledged_not_errored(
    client: TestClient, configured: None, card: str
) -> None:
    """A 4xx would make Stripe retry forever and eventually disable the
    endpoint, taking the authorisation events we DO care about down with it."""
    payload = authorization_event(event_type="issuing_card.created")
    response = post(client, payload, sign(payload))

    assert response.status_code == 200
    assert response.json() == {
        "received": True,
        "handled": False,
        "type": "issuing_card.created",
    }


def test_any_internal_failure_declines_rather_than_erroring(
    client: TestClient, configured: None, card: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed.

    Stripe does not read a non-decision as a decline -- it falls back to the
    account's configured default, which may be approve-all. So an error here
    must not propagate into a 4xx/5xx; it has to answer the question explicitly.
    """

    def exploding(*_args, **_kwargs):
        raise RuntimeError("settlement account vanished")

    monkeypatch.setattr(webhooks, "decide", exploding)

    payload = authorization_event(amount=2_500)
    response = post(client, payload, sign(payload))

    assert response.status_code == 200
    assert response.json() == {"approve": False, "decline_reason": "webhook_error"}
