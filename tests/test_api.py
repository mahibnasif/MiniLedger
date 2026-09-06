"""The HTTP surface: status codes, headers, and validation at the edge."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session


def _body(
    accounts: dict[str, uuid.UUID],
    *,
    source: str = "house:float",
    destination: str = "wallet:alice",
    amount: int = 5000,
    **extra: object,
) -> dict:
    return {
        "source_account_id": str(accounts[source]),
        "destination_account_id": str(accounts[destination]),
        "amount": amount,
        **extra,
    }


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_create_transfer(
    client: TestClient, accounts: dict[str, uuid.UUID]
) -> None:
    response = client.post(
        "/transfers",
        json=_body(accounts, description="opening"),
        headers={"Idempotency-Key": "k-1"},
    )

    assert response.status_code == 201
    assert response.headers["Idempotent-Replay"] == "false"

    payload = response.json()
    assert payload["amount"] == 5000
    assert payload["currency"] == "USD"
    assert payload["description"] == "opening"
    uuid.UUID(payload["id"])  # parses


def test_retry_replays_the_original_response(
    client: TestClient, accounts: dict[str, uuid.UUID]
) -> None:
    body = _body(accounts, description="opening")
    headers = {"Idempotency-Key": "k-1"}

    first = client.post("/transfers", json=body, headers=headers)
    second = client.post("/transfers", json=body, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.headers["Idempotent-Replay"] == "false"
    assert second.headers["Idempotent-Replay"] == "true"

    # Same transfer, same values -- and the same BYTES. The stored body
    # round-trips through jsonb, which normalises key order, so returning it
    # raw would reorder the keys on a replay. Both responses go through this
    # route's response_model, which puts them through one serialiser.
    assert first.json() == second.json()
    assert first.content == second.content


def test_retry_does_not_move_money_twice(
    client: TestClient, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    body = _body(accounts, amount=5000)
    for _ in range(5):
        client.post("/transfers", json=body, headers={"Idempotency-Key": "k-1"})

    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 1

    alice = client.get(f"/accounts/{accounts['wallet:alice']}").json()
    assert alice["balance"] == 5000
    assert alice["entry_count"] == 1


def test_same_key_different_payload_is_409(
    client: TestClient, accounts: dict[str, uuid.UUID]
) -> None:
    headers = {"Idempotency-Key": "k-1"}
    client.post("/transfers", json=_body(accounts, amount=5000), headers=headers)
    response = client.post(
        "/transfers", json=_body(accounts, amount=6000), headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"] == "IdempotencyKeyConflict"


def test_insufficient_funds_is_422(
    client: TestClient, accounts: dict[str, uuid.UUID]
) -> None:
    response = client.post(
        "/transfers",
        json=_body(accounts, source="wallet:alice", destination="wallet:bob"),
        headers={"Idempotency-Key": "k-1"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "InsufficientFunds"


def test_unknown_account_is_404(
    client: TestClient, accounts: dict[str, uuid.UUID]
) -> None:
    response = client.post(
        "/transfers",
        json={
            "source_account_id": str(accounts["house:float"]),
            "destination_account_id": str(uuid.uuid4()),
            "amount": 100,
        },
        headers={"Idempotency-Key": "k-1"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "AccountNotFound"


def test_missing_idempotency_key_is_rejected(
    client: TestClient, accounts: dict[str, uuid.UUID], session: Session
) -> None:
    """The header is required, so the unsafe path is not reachable by accident."""
    response = client.post("/transfers", json=_body(accounts))
    assert response.status_code == 422
    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 0


@pytest.mark.parametrize(
    ("label", "patch"),
    [
        ("zero amount", {"amount": 0}),
        ("negative amount", {"amount": -100}),
        ("unsupported currency", {"currency": "EUR"}),
        ("unknown field", {"reference": "oops"}),
    ],
)
def test_malformed_requests_are_rejected_at_the_edge(
    client: TestClient,
    accounts: dict[str, uuid.UUID],
    session: Session,
    label: str,
    patch: dict,
) -> None:
    response = client.post(
        "/transfers",
        json={**_body(accounts), **patch},
        headers={"Idempotency-Key": "k-1"},
    )
    assert response.status_code == 422, label
    # Nothing reached the ledger, and the key was never claimed.
    assert session.execute(text("SELECT count(*) FROM transfers")).scalar_one() == 0
    assert (
        session.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 0
    )


def test_currency_is_optional_and_does_not_affect_the_hash(
    client: TestClient, accounts: dict[str, uuid.UUID]
) -> None:
    """Omitting "USD" and sending it explicitly are the same request.

    If currency leaked into the idempotency hash, a retry that dropped the
    default would be rejected as a conflict.
    """
    headers = {"Idempotency-Key": "k-1"}
    first = client.post("/transfers", json=_body(accounts), headers=headers)
    second = client.post(
        "/transfers", json=_body(accounts, currency="USD"), headers=headers
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.headers["Idempotent-Replay"] == "true"


# --- Account reads -----------------------------------------------------------


def test_list_accounts(client: TestClient, accounts: dict[str, uuid.UUID]) -> None:
    client.post(
        "/transfers", json=_body(accounts), headers={"Idempotency-Key": "k-1"}
    )
    listed = {a["name"]: a for a in client.get("/accounts").json()}

    assert set(listed) == {"house:float", "wallet:alice", "wallet:bob"}
    assert listed["wallet:alice"]["balance"] == 5000
    assert listed["house:float"]["balance"] == -5000
    # Never touched, still present with a zero balance.
    assert listed["wallet:bob"]["balance"] == 0


def test_get_unknown_account_is_404(client: TestClient) -> None:
    response = client.get(f"/accounts/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"] == "AccountNotFound"
