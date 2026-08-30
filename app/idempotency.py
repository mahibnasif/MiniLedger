"""Claiming idempotency keys in the database.

The failure mode this prevents, concretely: a client POSTs a $50 transfer, the
response is lost to a timeout, and the client retries. Without idempotency the
customer is charged twice. The retry may arrive *while the first attempt is
still running*, and it may land on a different worker process, so anything held
in application memory is useless. The claim has to be made somewhere both
requests can contend, which means the database.

Everything here runs inside the caller's transaction and nothing commits.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Claim:
    """The outcome of trying to claim a key."""

    is_new: bool
    stored_request_hash: str
    transfer_id: uuid.UUID | None
    response_status: int | None
    response_body: dict[str, Any] | None


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 over a canonical rendering of the request.

    sort_keys and fixed separators mean two semantically identical payloads
    hash identically regardless of how the client ordered or spaced its JSON --
    otherwise a retry that serialised its fields in a different order would
    look like a different request and be rejected as a conflict.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# The single statement the entire scheme rests on.
#
# WHY "DO UPDATE" AND NOT "DO NOTHING":
#
#   DO NOTHING returns zero rows on conflict. That tells you a row exists but
#   nothing about it, and takes no lock on it, so you would need a second
#   SELECT ... FOR UPDATE and then have to handle the case where the other
#   transaction rolled back in between. Two statements, and a race between them.
#
#   DO UPDATE always returns the row and always holds a row lock on it. If a
#   concurrent transaction holds an uncommitted row for this key, DO UPDATE
#   BLOCKS until that transaction commits or rolls back, then re-evaluates --
#   inserting if it rolled back, returning the committed row if it did not.
#   That block is not a cost, it is the serialisation: the duplicate request
#   waits for the original to finish and then reads its result.
#
# WHY THE UPDATE IS A DELIBERATE NO-OP:
#
#   SET request_hash = idempotency_keys.request_hash writes the value already
#   there. Using EXCLUDED.request_hash would overwrite the ORIGINAL request's
#   hash with the retry's, destroying the only evidence that the two requests
#   differed. The update exists purely to take the lock and make RETURNING
#   produce the existing row.
_CLAIM_KEY = text(
    """
    INSERT INTO idempotency_keys (endpoint, idempotency_key, request_hash)
    VALUES (:endpoint, :idempotency_key, :request_hash)
    ON CONFLICT (endpoint, idempotency_key) DO UPDATE
        SET request_hash = idempotency_keys.request_hash
    RETURNING request_hash, transfer_id, response_status, response_body
    """
)


def claim(
    session: Session, *, endpoint: str, idempotency_key: str, request_hash: str
) -> Claim:
    """Claim `idempotency_key`, or return the completed row that already holds it.

    `is_new` is derived from `transfer_id IS NULL` rather than from a status
    column, and that is sound because of how the transaction is scoped: the
    claim and the transfer commit together, so a *committed* row always has
    transfer_id set. A row we can see with transfer_id still NULL can only be
    the one we just inserted ourselves.

    The pay-off is that there is no committed "in progress" state, so there is
    no possibility of a key stuck half-claimed by a process that died, and no
    need for the background expiry sweeper that a two-transaction design would
    require.
    """
    row = session.execute(
        _CLAIM_KEY,
        {
            "endpoint": endpoint,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
        },
    ).one()

    return Claim(
        is_new=row.transfer_id is None,
        stored_request_hash=row.request_hash,
        transfer_id=row.transfer_id,
        response_status=row.response_status,
        response_body=row.response_body,
    )


def complete(
    session: Session,
    *,
    endpoint: str,
    idempotency_key: str,
    transfer_id: uuid.UUID,
    response_status: int,
    response_body: Mapping[str, Any],
) -> None:
    """Record the result against the claimed key.

    All four completion columns are written together; a CHECK constraint
    rejects any half-filled row. Still uncommitted when this returns -- the
    caller decides whether the whole thing lands.
    """
    session.execute(
        text(
            """
            UPDATE idempotency_keys
               SET transfer_id     = :transfer_id,
                   response_status = :response_status,
                   response_body   = CAST(:response_body AS jsonb),
                   completed_at    = now()
             WHERE endpoint = :endpoint
               AND idempotency_key = :idempotency_key
            """
        ),
        {
            "endpoint": endpoint,
            "idempotency_key": idempotency_key,
            "transfer_id": transfer_id,
            "response_status": response_status,
            "response_body": json.dumps(response_body, separators=(",", ":")),
        },
    )
