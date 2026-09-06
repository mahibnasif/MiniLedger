"""Stripe card provisioning, against a fake Stripe client.

This is the one path that cannot be run for real without a Stripe account with
Issuing enabled, so it is the one path most likely to rot unnoticed. A fake
client does not prove Stripe accepts these parameters -- only a real call does
that -- but it does prove the call chain exists as spelled, that the response
is unpacked correctly, and that the card-to-account mapping lands in the
database. Those are the failures that would otherwise be discovered live.

The fake mimics the SDK's shape deliberately: client.v1.issuing.cardholders
.create(...) rather than a single stubbed function, so a typo anywhere in that
chain fails here instead of at a demo.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.stripe_client import StripeNotConfigured
from scripts import issue_card


class FakeResource:
    """Stands in for client.v1.issuing.cardholders / .cards."""

    def __init__(self, result: SimpleNamespace) -> None:
        self.result = result
        self.calls: list[dict] = []

    def create(self, params: dict) -> SimpleNamespace:
        self.calls.append(params)
        return self.result


@pytest.fixture()
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cardholders = FakeResource(SimpleNamespace(id="ich_fake_123"))
    cards = FakeResource(SimpleNamespace(id="ic_fake_456", last4="4242"))
    client = SimpleNamespace(
        v1=SimpleNamespace(issuing=SimpleNamespace(cardholders=cardholders, cards=cards))
    )
    monkeypatch.setattr(issue_card, "get_client", lambda: client)
    return SimpleNamespace(client=client, cardholders=cardholders, cards=cards)


@pytest.fixture()
def cli(session: Session, monkeypatch: pytest.MonkeyPatch):
    """Point the script's session_scope at the test database."""

    @contextmanager
    def scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(issue_card, "session_scope", scope)
    return issue_card


def test_issuing_a_card_records_the_account_mapping(
    cli, fake_stripe: SimpleNamespace, funded: dict[str, uuid.UUID], session: Session
) -> None:
    assert cli.main(["--account", "wallet:alice"]) == 0

    row = session.execute(
        text(
            "SELECT stripe_card_id, stripe_cardholder_id, account_id, last4 "
            "FROM cards WHERE stripe_card_id = 'ic_fake_456'"
        )
    ).one()

    assert row.stripe_cardholder_id == "ich_fake_123"
    assert row.account_id == funded["wallet:alice"]
    assert row.last4 == "4242"


def test_the_card_is_virtual_active_and_usd(
    cli, fake_stripe: SimpleNamespace, funded: dict[str, uuid.UUID]
) -> None:
    """Pins the parameters actually sent, since nothing else checks them."""
    cli.main(["--account", "wallet:alice"])

    (card_params,) = fake_stripe.cards.calls
    assert card_params["cardholder"] == "ich_fake_123"
    assert card_params["type"] == "virtual"
    assert card_params["status"] == "active"
    assert card_params["currency"] == "usd"

    (cardholder_params,) = fake_stripe.cardholders.calls
    assert cardholder_params["name"] == "wallet:alice"
    assert cardholder_params["type"] == "individual"
    assert cardholder_params["billing"]["address"]["country"] == "US"


def test_re_running_does_not_issue_a_second_card(
    cli, fake_stripe: SimpleNamespace, funded: dict[str, uuid.UUID], session: Session
) -> None:
    """Cards are not free, and a second one silently spending the same wallet
    is easy to create by accident and annoying to unpick."""
    assert cli.main(["--account", "wallet:alice"]) == 0
    assert cli.main(["--account", "wallet:alice"]) == 0

    assert session.execute(text("SELECT count(*) FROM cards")).scalar_one() == 1
    # Stripe was called exactly once, not twice.
    assert len(fake_stripe.cards.calls) == 1
    assert len(fake_stripe.cardholders.calls) == 1


def test_unknown_account_exits_one_without_calling_stripe(
    cli, fake_stripe: SimpleNamespace, funded: dict[str, uuid.UUID]
) -> None:
    assert cli.main(["--account", "wallet:nobody"]) == 1
    assert fake_stripe.cardholders.calls == []
    assert fake_stripe.cards.calls == []


def test_missing_api_key_exits_two(
    cli, funded: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clear message and a distinct exit code, not a traceback."""

    def unconfigured():
        raise StripeNotConfigured("STRIPE_API_KEY is not set.")

    monkeypatch.setattr(issue_card, "get_client", unconfigured)
    assert cli.main(["--account", "wallet:alice"]) == 2


def test_two_accounts_get_two_cards(
    cli, fake_stripe: SimpleNamespace, funded: dict[str, uuid.UUID], session: Session
) -> None:
    """The "already has a card" check is per account, not global."""
    cli.main(["--account", "wallet:alice"])

    fake_stripe.cards.result = SimpleNamespace(id="ic_fake_789", last4="1881")
    cli.main(["--account", "wallet:bob"])

    names = dict(
        session.execute(
            text(
                "SELECT a.name, c.stripe_card_id FROM cards c "
                "JOIN accounts a ON a.id = c.account_id"
            )
        ).all()  # type: ignore[arg-type]
    )
    assert names == {"wallet:alice": "ic_fake_456", "wallet:bob": "ic_fake_789"}
