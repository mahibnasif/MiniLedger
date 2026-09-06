"""The HTTP layer.

Thin on purpose. Every rule lives in app/transfers.py and the database; this
module's only jobs are parsing, choosing status codes, and owning the
transaction boundary.

Transactions are committed and rolled back explicitly inside the route rather
than in the `get_session` dependency's teardown. Teardown for a yield
dependency runs after the response has been produced, which makes "was this
committed before or after the error handler ran?" a question you have to think
about. For a money endpoint it should not be a question at all.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import (
    AccountNotFound,
    IdempotencyKeyConflict,
    InsufficientFunds,
    InvalidTransfer,
    LedgerError,
)
from app.ledger import AccountSnapshot, cached_all_snapshots, cached_snapshot
from app.schemas import AccountResponse, TransferRequest, TransferResponse
from app.transfers import execute_transfer
from app.webhooks import router as webhooks_router

app = FastAPI(
    title="MiniLedger",
    version="1.0.0",
    description=(
        "A double-entry ledger with idempotent money movement. "
        "Balances are derived from an append-only entry log."
    ),
)

# Domain error -> HTTP status. The service layer knows nothing about HTTP, so
# this mapping is the only place the two vocabularies meet.
STATUS_BY_ERROR: dict[type[LedgerError], int] = {
    AccountNotFound: 404,
    InsufficientFunds: 422,
    InvalidTransfer: 422,
    IdempotencyKeyConflict: 409,
}


app.include_router(webhooks_router)


@app.exception_handler(LedgerError)
async def handle_ledger_error(_request, exc: LedgerError) -> JSONResponse:
    status = STATUS_BY_ERROR.get(type(exc), 400)
    return JSONResponse(
        status_code=status,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


def _account_response(snapshot: AccountSnapshot) -> AccountResponse:
    return AccountResponse(
        id=snapshot.account_id,
        name=snapshot.name,
        normal_balance=snapshot.normal_balance,
        balance=snapshot.balance,
        posted_debits=snapshot.totals.posted_debits,
        posted_credits=snapshot.totals.posted_credits,
        entry_count=snapshot.totals.entry_count,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/accounts", response_model=list[AccountResponse])
def list_accounts(session: Annotated[Session, Depends(get_session)]):
    """Every account with the balance the system currently reports.

    Exists so a transfer can be verified end to end, and so the account UUIDs
    the transfer endpoint needs are discoverable without opening psql.
    """
    return [_account_response(s) for s in cached_all_snapshots(session)]


@app.post("/transfers", status_code=201, response_model=TransferResponse)
def create_transfer(
    payload: TransferRequest,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            description="Required. Retries carrying the same key return the "
            "original transfer instead of creating a second one.",
        ),
    ],
):
    """Move money between two accounts, at most once per idempotency key.

    The header is required rather than optional: making it optional would mean
    shipping a money endpoint whose default behaviour is unsafe, and every
    caller would have to remember to opt in.

    A retry carrying the same key and the same payload returns the original
    response with `Idempotent-Replay: true` set. The difference is reported in a
    header rather than by changing what the caller is handed: the body carries
    the same transfer id and the same values as the first response.

    The replay is byte-identical, and that is worth one sentence because it is
    not free. The stored body round-trips through a jsonb column, which
    normalises key order, so returning it raw would hand back the same values in
    a different sequence. Serialising both the original and the replay through
    this route's response_model puts them through one serialiser, so the bytes
    match -- and the OpenAPI schema documents the shape as a side effect.
    jsonb is still the right storage type: an operator debugging a duplicate
    charge at 3am can query these bodies in SQL, which raw text would not allow.
    """
    try:
        body, was_replay = execute_transfer(
            session,
            idempotency_key=idempotency_key,
            source_account_id=payload.source_account_id,
            destination_account_id=payload.destination_account_id,
            amount=payload.amount,
            description=payload.description,
        )
        session.commit()
    except LedgerError:
        # Rolls back the idempotency claim along with any postings, so a key
        # burned on a failed request is free to be retried.
        session.rollback()
        raise

    response.headers["Idempotent-Replay"] = "true" if was_replay else "false"
    return body


@app.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]):
    snapshot = cached_snapshot(session, account_id)
    if snapshot is None:
        raise AccountNotFound(account_id)
    return _account_response(snapshot)
