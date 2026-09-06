"""Stripe webhook handling.

The single most important line in this file is the signature check, so it is
worth saying plainly why.

This endpoint is reachable by anyone who learns its URL, and it moves money.
Without verification, a forged POST claiming
`issuing_authorization.request` for $10,000 would be honoured -- the handler
cannot tell a real Stripe delivery from a curl command. The signature is the
ONLY thing that establishes the request came from Stripe.

`stripe.Webhook.construct_event` does three things, and all three matter:

  1. Recomputes HMAC-SHA256 over "<timestamp>.<raw body>" using the endpoint's
     signing secret, and compares it to the Stripe-Signature header in constant
     time. An attacker without the secret cannot produce a matching signature.

  2. Rejects signatures older than a tolerance (300s default). Without that
     check, a signature stays valid forever, and anyone who captures one real
     request can replay it indefinitely -- each replay being a genuinely
     Stripe-signed money movement.

  3. Refuses to parse at all until the above pass, so a malformed or hostile
     body never reaches the handler.

Its return value is deliberately discarded. It is called for verification, and
the payload is then decoded with a plain `json.loads`, because construct_event
hands back StripeObjects rather than dicts -- `.get()` on one raises
AttributeError, which a test caught. Parsing the bytes ourselves keeps this
handler coupled to Stripe's wire format, which is versioned and stable, rather
than to the SDK's object model, which is neither.

It must be given the RAW request body. Re-serialising the parsed JSON changes
whitespace and key order, the HMAC no longer matches, and every legitimate
webhook fails -- which is why the route reads `await request.body()` rather
than taking a Pydantic model.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.issuing import (
    DECLINE_WEBHOOK_ERROR,
    AuthorizationDecision,
    AuthorizationRequest,
    decide,
)
from app.stripe_client import StripeNotConfigured, get_webhook_secret

logger = logging.getLogger("miniledger.webhooks")

router = APIRouter()

AUTHORIZATION_REQUEST = "issuing_authorization.request"


def _authorization_request(event_object: dict) -> AuthorizationRequest:
    """Pull the decision inputs out of a Stripe Issuing authorization object."""
    merchant = event_object.get("merchant_data") or {}
    return AuthorizationRequest(
        stripe_authorization_id=event_object["id"],
        stripe_card_id=(event_object.get("card") or {}).get("id")
        if isinstance(event_object.get("card"), dict)
        else event_object.get("card"),
        # pending_request carries the amount still being asked for; on a fresh
        # authorisation request `amount` matches it, but pending_request is the
        # field Stripe documents for this event, so prefer it when present.
        amount=int(
            (event_object.get("pending_request") or {}).get("amount")
            or event_object.get("amount")
            or 0
        ),
        merchant_name=merchant.get("name"),
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    """Verify, then handle, an inbound Stripe event.

    Returns the approve/decline decision in the response body, because that is
    how Stripe's real-time issuing authorisations work: this endpoint is not
    acknowledging something that already happened, it is answering a question
    while a cardholder is standing at a terminal. Stripe allows roughly two
    seconds, which is why the decision is a single indexed balance read and a
    transfer rather than anything cleverer.
    """
    # RAW bytes. Anything that re-serialises the body breaks the HMAC.
    payload = await request.body()

    try:
        secret = get_webhook_secret()
    except StripeNotConfigured as exc:
        # 503, not 500: "not finished setting up" and "broken" need different
        # responses from whoever is looking at this.
        logger.warning("stripe webhook received but not configured: %s", exc)
        return JSONResponse(
            status_code=503, content={"error": "StripeNotConfigured", "detail": str(exc)}
        )

    try:
        # Used for VERIFICATION only. Hand-rolling HMAC comparison is exactly
        # the kind of security code that should be borrowed, not written.
        stripe.Webhook.construct_event(payload, stripe_signature, secret)
    except ValueError:
        # Body was not valid JSON.
        logger.warning("stripe webhook rejected: malformed payload")
        return JSONResponse(
            status_code=400,
            content={"error": "InvalidPayload", "detail": "body is not valid JSON"},
        )
    except stripe.SignatureVerificationError:
        # Either a forgery or a replay of a captured request past the tolerance
        # window. Deliberately not distinguished in the response -- telling an
        # attacker which of the two failed is free information.
        logger.warning("stripe webhook rejected: bad signature")
        return JSONResponse(
            status_code=400,
            content={
                "error": "SignatureVerificationError",
                "detail": "signature missing, invalid, or outside the tolerance window",
            },
        )

    # Parsed separately as plain JSON rather than reading the typed objects
    # construct_event returns. Those are StripeObjects, not dicts -- .get() on
    # one raises AttributeError, which a test caught. Decoding the raw bytes
    # ourselves keeps the handler dependent on Stripe's wire format (stable,
    # versioned) instead of on the SDK's object model (neither).
    event = json.loads(payload)
    event_type = event.get("type")

    if event_type != AUTHORIZATION_REQUEST:
        # Acknowledge everything else with a 200. Returning an error for an
        # event we simply do not handle would make Stripe retry it forever and
        # eventually disable the endpoint -- taking the authorisation events we
        # DO care about down with it.
        logger.info("ignoring unhandled stripe event %s", event_type)
        return {"received": True, "handled": False, "type": event_type}

    authorization = _authorization_request(event.get("data", {}).get("object", {}))

    if not authorization.stripe_card_id or authorization.amount <= 0:
        logger.warning(
            "authorization %s missing card id or amount",
            authorization.stripe_authorization_id,
        )
        return AuthorizationDecision(
            approved=False, reason=DECLINE_WEBHOOK_ERROR
        ).as_stripe_response()

    try:
        decision = decide(session, authorization)
        session.commit()
    except Exception:
        session.rollback()
        # EVERY failure declines. No exception type gets to escape into a 4xx
        # here, and that uniformity is deliberate.
        #
        # Stripe does not treat a non-decision as a decline -- it falls back to
        # whatever default the account has configured, which may well be
        # approve-all. So letting an error propagate would hand the decision to
        # a setting in a dashboard instead of to this code. An explicit decline
        # fails closed no matter what broke.
        #
        # The cost is that a misconfiguration (say, a missing settlement
        # account) declines quietly and Stripe never retries, so this logs at
        # exception level with the authorisation id to make it findable.
        logger.exception(
            "authorization %s failed; declining",
            authorization.stripe_authorization_id,
        )
        return AuthorizationDecision(
            approved=False, reason=DECLINE_WEBHOOK_ERROR
        ).as_stripe_response()

    logger.info(
        "authorization %s %s (%s minor units, balance %s)%s",
        authorization.stripe_authorization_id,
        "approved" if decision.approved else f"declined: {decision.reason}",
        authorization.amount,
        decision.balance_at_decision,
        " [replayed]" if decision.replayed else "",
    )
    return decision.as_stripe_response()
