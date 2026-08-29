"""Domain errors.

Separate from the HTTP layer on purpose: the ledger rules are true whether they
are reached from FastAPI, a script, or a test. app/main.py is the only place
that decides which status code each of these becomes.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for every rule this ledger enforces."""


class AccountNotFound(LedgerError):
    def __init__(self, account_id: object) -> None:
        super().__init__(f"account {account_id} does not exist")
        self.account_id = account_id


class InsufficientFunds(LedgerError):
    """A posting would drive an account negative that is not allowed to be.

    Carries the numbers rather than just a message, so the API can tell the
    caller how short they were without re-querying.
    """

    def __init__(
        self, account_name: str, balance: int, requested: int, shortfall: int
    ) -> None:
        super().__init__(
            f"{account_name} holds {balance} but {requested} was requested "
            f"({shortfall} short)"
        )
        self.account_name = account_name
        self.balance = balance
        self.requested = requested
        self.shortfall = shortfall


class InvalidTransfer(LedgerError):
    """The request is not a coherent movement of money."""


class IdempotencyKeyConflict(LedgerError):
    """Same key, different request.

    Replaying the first response here would silently hide a client bug -- the
    caller believes they sent request B and would be handed the result of
    request A. Reported instead.
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            f"idempotency key {idempotency_key!r} was already used with a "
            f"different request payload"
        )
        self.idempotency_key = idempotency_key
