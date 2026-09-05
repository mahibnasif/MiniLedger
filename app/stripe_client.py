"""Stripe client construction.

Everything Stripe-facing goes through here so there is exactly one place where
credentials are read and exactly one place to look when asking "could this
possibly touch live money?".

Keys come from .env via app.config, never from a literal in the source. The
Settings validator already refuses anything that is not sk_test_, so a live key
cannot reach this module at all -- the process will not boot.
"""

from __future__ import annotations

import stripe

from app.config import get_settings

PLACEHOLDER_KEY = "sk_test_replace_me"


class StripeNotConfigured(RuntimeError):
    """Raised when Stripe is needed but no usable key is present.

    A distinct type rather than a bare RuntimeError so the webhook route can
    return a clear 503 instead of a 500. "You have not finished setting this
    up" and "this is broken" are different messages for whoever is on call.
    """


def get_client() -> stripe.StripeClient:
    """Build a Stripe client from the configured test key.

    Constructed per call rather than at import time. Module-level construction
    would make importing anything in this package fail when Stripe is not
    configured, which would take the ledger -- the part that has nothing to do
    with Stripe -- down with it.
    """
    settings = get_settings()
    key = settings.stripe_api_key

    if not key or key == PLACEHOLDER_KEY:
        raise StripeNotConfigured(
            "STRIPE_API_KEY is not set. Copy .env.example to .env and put your "
            "Stripe test-mode secret key in it (see the README, Phase 4 setup)."
        )

    # StripeClient rather than the module-level stripe.api_key global: the
    # global is process-wide mutable state, which in a test suite means one
    # test can silently reconfigure another.
    return stripe.StripeClient(key)


def get_webhook_secret() -> str:
    """The signing secret for verifying inbound webhooks.

    Separate from the API key and separately checked, because they fail in
    different ways: a missing API key means we cannot call Stripe, a missing
    webhook secret means we cannot trust anything Stripe appears to send us.
    The second is the dangerous one.
    """
    secret = get_settings().stripe_webhook_secret

    if not secret or secret == "whsec_replace_me":
        raise StripeNotConfigured(
            "STRIPE_WEBHOOK_SECRET is not set. `stripe listen` prints it when "
            "you start forwarding (see the README, Phase 4 setup)."
        )
    return secret
